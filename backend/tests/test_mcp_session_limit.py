# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant MCP session-start cap unit tests (#3500).

Drives :mod:`meho_backplane.mcp.session_limit` against an in-memory fake
Valkey, mirroring ``test_events_ingest_rate_limit.py``. Acceptance-criteria
coverage:

* the ``cap+1``-th ``initialize`` in one window is rejected with a
  retry-after; a fresh window refills the budget
* the per-tenant override wins over the global default
* a cap of ``0`` disables the control with no Valkey round-trip
* a second tenant is unaffected (per-tenant counter)
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.mcp.session_limit import (
    check_mcp_session_cap,
    resolve_mcp_session_cap,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
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


@pytest.fixture
def fake_valkey(monkeypatch: pytest.MonkeyPatch) -> _FakeValkey:
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.mcp.session_limit.get_broadcast_client",
        lambda: fake,
    )
    monkeypatch.setattr(
        "meho_backplane.mcp.session_limit.time.time",
        lambda: _FIXED_NOW,
    )
    return fake


def test_resolve_session_cap_prefers_override() -> None:
    settings = SimpleNamespace(
        mcp_session_start_limit_per_minute=50,
        mcp_session_start_limit_per_minute_overrides=f"{_TENANT}=3",
    )
    assert resolve_mcp_session_cap(settings, _TENANT) == 3
    assert resolve_mcp_session_cap(settings, _OTHER_TENANT) == 50


@pytest.mark.asyncio
async def test_within_cap_passes(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        assert await check_mcp_session_cap(_TENANT, 3) is None


@pytest.mark.asyncio
async def test_over_cap_rejected_with_retry_after(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        assert await check_mcp_session_cap(_TENANT, 3) is None
    assert await check_mcp_session_cap(_TENANT, 3) == 10


@pytest.mark.asyncio
async def test_window_rollover_refills(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.mcp.session_limit.get_broadcast_client",
        lambda: fake,
    )
    clock = {"now": _FIXED_NOW}
    monkeypatch.setattr(
        "meho_backplane.mcp.session_limit.time.time",
        lambda: clock["now"],
    )
    for _ in range(2):
        assert await check_mcp_session_cap(_TENANT, 2) is None
    assert await check_mcp_session_cap(_TENANT, 2) == 10
    clock["now"] = _FIXED_NOW + 60
    assert await check_mcp_session_cap(_TENANT, 2) is None


@pytest.mark.asyncio
async def test_per_tenant_isolation(fake_valkey: _FakeValkey) -> None:
    for _ in range(2):
        await check_mcp_session_cap(_TENANT, 2)
    assert await check_mcp_session_cap(_TENANT, 2) == 10
    # A different tenant has its own budget.
    assert await check_mcp_session_cap(_OTHER_TENANT, 2) is None


@pytest.mark.asyncio
async def test_disabled_no_round_trip(fake_valkey: _FakeValkey) -> None:
    assert await check_mcp_session_cap(_TENANT, 0) is None
    assert fake_valkey.pipeline_calls == 0
