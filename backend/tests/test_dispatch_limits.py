# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-principal dispatch rate limit + concurrent-op cap unit tests (#3500).

Drives :mod:`meho_backplane.operations.dispatch_limits` against an in-memory
fake Valkey (no socket), mirroring ``test_events_ingest_rate_limit.py``.
Acceptance-criteria coverage:

* the ``limit+1``-th dispatch in one window is rejected with a retry-after
  (criterion 1 / rate-limit) and a fresh window refills the budget
* a second principal in the same tenant is unaffected (per-``(tenant,
  principal)`` counter)
* the per-tenant override wins over the global default (criterion 3)
* the concurrent-op cap admits up to ``cap`` in-flight dispatches, rejects
  the next, and a release frees a slot (criterion 2)
* a limit / cap of ``0`` disables the control with no Valkey round-trip
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations import dispatch_limits
from meho_backplane.operations.dispatch_limits import (
    acquire_dispatch_slot,
    check_dispatch_rate_limit,
    rate_limited_read_envelope,
    release_dispatch_slot,
    resolve_dispatch_concurrency_cap,
    resolve_dispatch_rate_limit,
    resolve_int_override,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_FIXED_NOW = 1_700_000_030.0  # int(now) % 60 == 50, so retry_after == 10


class _FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str, amount: int = 1) -> None:
        self._ops.append(("incr", name, amount))

    def expire(self, name: str, time: int, *args: object, **kwargs: object) -> None:
        self._ops.append(("expire", name, time))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, name, arg in self._ops:
            if op == "incr":
                self._store[name] = self._store.get(name, 0) + arg
                results.append(self._store[name])
            else:
                results.append(True)
        return results


class _FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.pipeline_calls = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        self.pipeline_calls += 1
        return _FakePipeline(self.store)

    async def decr(self, name: str, amount: int = 1) -> int:
        self.store[name] = self.store.get(name, 0) - amount
        return self.store[name]

    async def delete(self, name: str) -> int:
        return 1 if self.store.pop(name, None) is not None else 0


@pytest.fixture
def fake_valkey(monkeypatch: pytest.MonkeyPatch) -> _FakeValkey:
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.get_broadcast_client",
        lambda: fake,
    )
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.time.time",
        lambda: _FIXED_NOW,
    )
    return fake


# --- resolver (pure, no Valkey) ------------------------------------------


def test_resolve_int_override_empty_uses_default() -> None:
    assert resolve_int_override("", _TENANT, 42) == 42


def test_resolve_int_override_matches_tenant_case_insensitive() -> None:
    csv = f"{str(_TENANT).upper()}=5, {_OTHER_TENANT}=9"
    assert resolve_int_override(csv, _TENANT, 100) == 5


def test_resolve_int_override_absent_tenant_falls_back() -> None:
    assert resolve_int_override(f"{_OTHER_TENANT}=9", _TENANT, 100) == 100


def test_resolve_int_override_skips_malformed_entries() -> None:
    # A bad-uuid entry and a bad-int entry are skipped; the valid one wins.
    csv = f"not-a-uuid=3, {_TENANT}=nope, {_TENANT}=7"
    assert resolve_int_override(csv, _TENANT, 100) == 7


def test_resolve_dispatch_rate_limit_prefers_override() -> None:
    settings = SimpleNamespace(
        dispatch_rate_limit_per_minute=100,
        dispatch_rate_limit_per_minute_overrides=f"{_TENANT}=5",
    )
    assert resolve_dispatch_rate_limit(settings, _TENANT) == 5
    assert resolve_dispatch_rate_limit(settings, _OTHER_TENANT) == 100


def test_resolve_dispatch_concurrency_cap_prefers_override() -> None:
    settings = SimpleNamespace(
        dispatch_max_concurrent_ops=20,
        dispatch_max_concurrent_ops_overrides=f"{_TENANT}=2",
    )
    assert resolve_dispatch_concurrency_cap(settings, _TENANT) == 2
    assert resolve_dispatch_concurrency_cap(settings, _OTHER_TENANT) == 20


# --- rate limit ----------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_within_limit_passes(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        assert await check_dispatch_rate_limit(_TENANT, "p1", 3) is None


@pytest.mark.asyncio
async def test_rate_over_limit_rejected_with_retry_after(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        assert await check_dispatch_rate_limit(_TENANT, "p1", 3) is None
    retry = await check_dispatch_rate_limit(_TENANT, "p1", 3)
    # int(_FIXED_NOW) % 60 == 50, so the window rolls over in 10 s.
    assert retry == 10


@pytest.mark.asyncio
async def test_rate_window_rollover_refills(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.get_broadcast_client",
        lambda: fake,
    )
    clock = {"now": _FIXED_NOW}
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.time.time",
        lambda: clock["now"],
    )
    for _ in range(3):
        assert await check_dispatch_rate_limit(_TENANT, "p1", 3) is None
    assert await check_dispatch_rate_limit(_TENANT, "p1", 3) == 10
    # Advance into the next window: the budget refills (a new bucket key).
    clock["now"] = _FIXED_NOW + 60
    assert await check_dispatch_rate_limit(_TENANT, "p1", 3) is None


@pytest.mark.asyncio
async def test_rate_per_principal_isolation(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        await check_dispatch_rate_limit(_TENANT, "p1", 3)
    assert await check_dispatch_rate_limit(_TENANT, "p1", 3) == 10
    # A different principal in the same tenant has its own budget.
    assert await check_dispatch_rate_limit(_TENANT, "p2", 3) is None


@pytest.mark.asyncio
async def test_rate_disabled_no_round_trip(fake_valkey: _FakeValkey) -> None:
    assert await check_dispatch_rate_limit(_TENANT, "p1", 0) is None
    assert fake_valkey.pipeline_calls == 0


# --- concurrency cap -----------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_admits_up_to_cap(fake_valkey: _FakeValkey) -> None:
    keys = []
    for _ in range(2):
        retry, key = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
        assert retry is None
        assert key is not None
        keys.append(key)
    # The 3rd concurrent acquire is over the cap of 2.
    retry, key = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
    assert retry == dispatch_limits.DISPATCH_CONCURRENCY_RETRY_AFTER_SECONDS
    assert key is None


@pytest.mark.asyncio
async def test_concurrency_release_frees_slot(fake_valkey: _FakeValkey) -> None:
    _, key1 = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
    _, _key2 = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
    # At the cap: next is rejected.
    retry, rejected = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
    assert retry is not None and rejected is None
    # Release one slot; a fresh acquire now succeeds.
    await release_dispatch_slot(key1)
    retry, key3 = await acquire_dispatch_slot(_TENANT, "p1", 2, 3600)
    assert retry is None and key3 is not None


@pytest.mark.asyncio
async def test_concurrency_reject_does_not_leak_slot(fake_valkey: _FakeValkey) -> None:
    key = dispatch_limits._concurrency_key(_TENANT, "p1")
    await acquire_dispatch_slot(_TENANT, "p1", 1, 3600)  # count -> 1 (at cap)
    await acquire_dispatch_slot(_TENANT, "p1", 1, 3600)  # rejected, rolled back
    # The rejected attempt rolled its increment back, so the counter still
    # reflects exactly the one held slot.
    assert fake_valkey.store.get(key) == 1


@pytest.mark.asyncio
async def test_concurrency_disabled_no_round_trip(fake_valkey: _FakeValkey) -> None:
    retry, key = await acquire_dispatch_slot(_TENANT, "p1", 0, 3600)
    assert retry is None and key is None
    assert fake_valkey.pipeline_calls == 0


@pytest.mark.asyncio
async def test_release_none_is_noop(fake_valkey: _FakeValkey) -> None:
    await release_dispatch_slot(None)  # no raise, no Valkey call
    assert fake_valkey.store == {}


@pytest.mark.asyncio
async def test_release_reaps_key_at_zero(fake_valkey: _FakeValkey) -> None:
    _, key = await acquire_dispatch_slot(_TENANT, "p1", 5, 3600)
    assert key is not None and fake_valkey.store.get(key) == 1
    await release_dispatch_slot(key)
    # Deleted at zero rather than left at 0 (keeps the counter non-negative).
    assert key not in fake_valkey.store


# --- read-path envelope --------------------------------------------------


def _operator() -> Operator:
    return Operator(
        sub="reader-1",
        raw_jwt="x",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_read_envelope_none_under_limit(
    fake_valkey: _FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.get_settings",
        lambda: SimpleNamespace(
            dispatch_rate_limit_per_minute=2,
            dispatch_rate_limit_per_minute_overrides="",
        ),
    )
    assert await rate_limited_read_envelope(_operator()) is None


@pytest.mark.asyncio
async def test_read_envelope_returned_over_limit(
    fake_valkey: _FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.dispatch_limits.get_settings",
        lambda: SimpleNamespace(
            dispatch_rate_limit_per_minute=1,
            dispatch_rate_limit_per_minute_overrides="",
        ),
    )
    op = _operator()
    assert await rate_limited_read_envelope(op) is None  # 1st within cap of 1
    envelope = await rate_limited_read_envelope(op)  # 2nd over cap
    assert envelope is not None
    assert envelope["status"] == "rate_limited"
    assert envelope["error_code"] == "rate_limited"
    assert envelope["retry_after_seconds"] == 10
