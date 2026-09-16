# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.20-T7 (#1507) tests for :class:`ResultHandleStore`.

The store is a thin wrapper over an async Valkey client. These tests
drive it against a fake in-memory client (no container) to pin the
contract that matters for the read-back surface:

* spill → fetch round-trips the full rows;
* the operator-scoped key isolation (#304's contract) holds — a
  different operator gets a miss, not another operator's rows;
* tenant isolation holds — the same handle id in two tenants is two
  distinct keys;
* the spill is capped at ``max_rows`` and the window metadata reports
  the truncation;
* the store fails open — an unreachable client makes ``spill`` return
  ``0`` and ``fetch_window`` return ``None`` rather than raising.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import msgspec
import pytest
import redis.exceptions

import meho_backplane.connectors.result_handle_store as store_module
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    SpilledRowSet,
    SpilledWindow,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()


class _FakeRedis:
    """In-memory stand-in for the async Valkey client.

    Records the ``ex`` TTL passed to ``set`` so a test can assert the
    handle's ``ttl_seconds`` is threaded through; ignores actual expiry
    (the tests cover the not-found path via a missing key, and the TTL
    is enforced by real Valkey server-side).
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.last_ex: int | None = None

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.last_ex = ex
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


class _BrokenRedis:
    """Client whose every call raises — exercises the fail-open path."""

    async def set(self, *_a: Any, **_k: Any) -> None:
        raise redis.exceptions.ConnectionError("valkey down")

    async def get(self, *_a: Any, **_k: Any) -> None:
        raise redis.exceptions.ConnectionError("valkey down")


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"i": i, "name": f"row-{i}"} for i in range(n)]


async def test_spill_then_fetch_round_trips_full_rows() -> None:
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant = uuid4()
    handle = uuid4()
    rows = _rows(120)

    ok = await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.list",
        rows=rows,
        total_rows=120,
        ttl_seconds=3600,
        max_rows=10000,
    )
    assert ok == 120
    assert fake.last_ex == 3600

    # A window past the inline sample returns the real rows.
    window = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        offset=5,
        limit=50,
    )
    assert isinstance(window, SpilledWindow)
    assert window.total_rows == 120
    assert window.stored_rows == 120
    assert window.truncated is False
    assert [r["i"] for r in window.rows] == list(range(5, 55))

    # The full set is retrievable by paging to the end.
    tail = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        offset=100,
        limit=50,
    )
    assert tail is not None
    assert [r["i"] for r in tail.rows] == list(range(100, 120))


async def test_cross_operator_access_is_a_miss() -> None:
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant = uuid4()
    handle = uuid4()
    await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id=None,
        rows=_rows(60),
        total_rows=60,
        ttl_seconds=3600,
        max_rows=10000,
    )

    miss = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-b",  # different operator, same tenant
        handle_id=handle,
        offset=0,
        limit=10,
    )
    assert miss is None


async def test_cross_tenant_handle_is_distinct() -> None:
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    handle = uuid4()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.spill(
        tenant_id=tenant_a,
        operator_sub="op-a",
        handle_id=handle,
        op_id=None,
        rows=_rows(60),
        total_rows=60,
        ttl_seconds=3600,
        max_rows=10000,
    )

    # Same handle id, different tenant → no such key.
    miss = await store.fetch_window(
        tenant_id=tenant_b,
        operator_sub="op-a",
        handle_id=handle,
        offset=0,
        limit=10,
    )
    assert miss is None


async def test_unknown_handle_is_none() -> None:
    store = ResultHandleStore(_FakeRedis())
    window = await store.fetch_window(
        tenant_id=uuid4(),
        operator_sub="op-a",
        handle_id=uuid4(),
        offset=0,
        limit=10,
    )
    assert window is None


async def test_spill_caps_at_max_rows_and_reports_truncation() -> None:
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant, handle = uuid4(), uuid4()

    ok = await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id=None,
        rows=_rows(500),
        total_rows=500,
        ttl_seconds=3600,
        max_rows=100,  # cap below the row count
    )
    assert ok == 100

    window = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        offset=0,
        limit=500,
    )
    assert window is not None
    assert window.total_rows == 500
    assert window.stored_rows == 100
    assert window.truncated is True
    assert len(window.rows) == 100  # only the first 100 were stored

    # Past the stored tail: empty rows, metadata still populated.
    past = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        offset=200,
        limit=50,
    )
    assert past is not None
    assert past.rows == []
    assert past.truncated is True


@pytest.mark.parametrize(
    ("rows", "ttl", "max_rows"),
    [([], 3600, 10000), (_rows(5), 0, 10000), (_rows(5), 3600, 0)],
)
async def test_spill_skips_degenerate_inputs(
    rows: list[dict[str, Any]], ttl: int, max_rows: int
) -> None:
    """Empty rows, non-positive TTL, or zero cap → no spill (returns 0)."""
    store = ResultHandleStore(_FakeRedis())
    ok = await store.spill(
        tenant_id=uuid4(),
        operator_sub="op-a",
        handle_id=uuid4(),
        op_id=None,
        rows=rows,
        total_rows=len(rows),
        ttl_seconds=ttl,
        max_rows=max_rows,
    )
    assert ok == 0


async def test_store_fails_open_on_unreachable_client() -> None:
    """An unreachable Valkey makes spill return zero and fetch return None."""
    store = ResultHandleStore(_BrokenRedis())
    ok = await store.spill(
        tenant_id=uuid4(),
        operator_sub="op-a",
        handle_id=uuid4(),
        op_id=None,
        rows=_rows(60),
        total_rows=60,
        ttl_seconds=3600,
        max_rows=10000,
    )
    assert ok == 0

    window = await store.fetch_window(
        tenant_id=uuid4(),
        operator_sub="op-a",
        handle_id=uuid4(),
        offset=0,
        limit=10,
    )
    assert window is None


async def test_corrupt_payload_is_a_miss() -> None:
    """A non-decodable value under the key surfaces as not-found, not a raise."""
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant, handle = uuid4(), uuid4()
    # Write a value that is not the expected JSON payload shape.
    from meho_backplane.connectors.result_handle_store import _key

    fake.store[_key(tenant, handle)] = json.dumps({"unexpected": True}).encode()

    window = await store.fetch_window(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        offset=0,
        limit=10,
    )
    assert window is None


def test_key_shape_is_tenant_scoped() -> None:
    from meho_backplane.connectors.result_handle_store import _key

    tenant = UUID("00000000-0000-0000-0000-0000000000aa")
    handle = UUID("00000000-0000-0000-0000-0000000000bb")
    assert _key(tenant, handle) == (
        "meho:reshandle:00000000-0000-0000-0000-0000000000aa:00000000-0000-0000-0000-0000000000bb"
    )


# ---------------------------------------------------------------------------
# #3366 — fetch_rows: the full-set read the query surface uses
# ---------------------------------------------------------------------------


async def test_fetch_rows_returns_the_full_authorized_set() -> None:
    """The full-set read returns every stored row + the true total."""
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant = uuid4()
    handle = uuid4()
    rows = _rows(120)
    await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.list",
        rows=rows,
        total_rows=120,
        ttl_seconds=3600,
        max_rows=10000,
    )
    row_set = await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
    assert isinstance(row_set, SpilledRowSet)
    assert row_set.total_rows == 120
    assert row_set.stored_rows == 120
    assert [r["i"] for r in row_set.rows] == list(range(120))


async def test_fetch_rows_reports_capped_spill() -> None:
    """When the spill was capped, stored_rows < total_rows is preserved."""
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant = uuid4()
    handle = uuid4()
    await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="k8s.pods.list",
        rows=_rows(500),
        total_rows=500,
        ttl_seconds=3600,
        max_rows=100,
    )
    row_set = await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
    assert row_set is not None
    assert row_set.stored_rows == 100
    assert row_set.total_rows == 500
    assert len(row_set.rows) == 100


async def test_fetch_rows_cross_operator_is_a_miss() -> None:
    """A different operator gets a miss, not another operator's rows (#304)."""
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant = uuid4()
    handle = uuid4()
    await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="op",
        rows=_rows(10),
        total_rows=10,
        ttl_seconds=3600,
        max_rows=10000,
    )
    assert await store.fetch_rows(tenant_id=tenant, operator_sub="op-b", handle_id=handle) is None


async def test_fetch_rows_unknown_handle_is_none() -> None:
    store = ResultHandleStore(_FakeRedis())
    assert await store.fetch_rows(tenant_id=uuid4(), operator_sub="op-a", handle_id=uuid4()) is None


async def test_fetch_rows_fails_open_on_unreachable_store() -> None:
    """An unreachable client yields None, never an exception (fail-open)."""
    store = ResultHandleStore(_BrokenRedis())
    assert await store.fetch_rows(tenant_id=uuid4(), operator_sub="op-a", handle_id=uuid4()) is None


async def test_spill_byte_cap_accepts_exact_prefix_and_rejects_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from meho_backplane.connectors.result_handle_store import _StoredPayload

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(
        store_module, "datetime", type("Clock", (), {"now": staticmethod(lambda _tz: fixed)})
    )
    rows = [{"value": "é" * 10}, {"value": "b" * 100}, {"value": "later"}]
    created = fixed.isoformat()
    prefix = _StoredPayload(
        "op", "x", [rows[0]], 3, 1, created, 3, "partial", "unknown", {"note": "μ"}
    )
    cap = len(msgspec.json.encode(prefix))
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    stored = await store.spill(
        tenant_id=uuid4(),
        operator_sub="op",
        handle_id=uuid4(),
        op_id="x",
        rows=rows,
        total_rows=3,
        ttl_seconds=60,
        max_rows=3,
        max_record_bytes=cap,
        metadata={"note": "μ"},
    )
    assert stored == 1
    assert len(next(iter(fake.store.values()))) == cap
    assert (
        await store.spill(
            tenant_id=uuid4(),
            operator_sub="op",
            handle_id=uuid4(),
            op_id="x",
            rows=rows,
            total_rows=3,
            ttl_seconds=60,
            max_rows=3,
            max_record_bytes=cap - 1,
            metadata={"note": "μ"},
        )
        == 0
    )


async def test_spill_rejects_wide_first_row_without_writing() -> None:
    fake = _FakeRedis()
    stored = await ResultHandleStore(fake).spill(
        tenant_id=uuid4(),
        operator_sub="op",
        handle_id=uuid4(),
        op_id=None,
        rows=[{"value": "x" * 1000}, {"value": "later"}],
        total_rows=2,
        ttl_seconds=60,
        max_rows=2,
        max_record_bytes=20,
    )
    assert stored == 0
    assert fake.store == {}


def test_legacy_payload_defaults_are_unknown_and_independent() -> None:
    from meho_backplane.connectors.result_handle_store import _StoredPayload

    raw = msgspec.json.encode(
        {
            "operator_sub": "op",
            "op_id": None,
            "rows": [],
            "total_rows": 1,
            "stored_rows": 1,
            "created_at": "x",
        }
    )
    one = msgspec.json.decode(raw, type=_StoredPayload)
    two = msgspec.json.decode(raw, type=_StoredPayload)
    assert (one.captured_rows, one.storage_coverage, one.source_coverage) == (
        0,
        "unknown",
        "unknown",
    )
    one.metadata["x"] = 1
    assert two.metadata == {}


async def test_spill_accounts_for_count_digit_width_and_complete_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte selection includes stored-count width and the final complete label."""
    from datetime import UTC, datetime

    from meho_backplane.connectors.result_handle_store import _StoredPayload

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(
        store_module, "datetime", type("Clock", (), {"now": staticmethod(lambda _tz: fixed)})
    )

    async def spill_with_cap(rows: list[dict[str, Any]], cap: int) -> tuple[int, bytes]:
        fake = _FakeRedis()
        stored = await ResultHandleStore(fake).spill(
            tenant_id=uuid4(),
            operator_sub="op",
            handle_id=uuid4(),
            op_id=None,
            rows=rows,
            total_rows=len(rows),
            ttl_seconds=60,
            max_rows=len(rows),
            max_record_bytes=cap,
        )
        return stored, next(iter(fake.store.values()), b"")

    created = fixed.isoformat()
    ten_rows = [{"i": i} for i in range(10)]
    cap_at_nine = len(
        msgspec.json.encode(_StoredPayload("op", None, ten_rows[:9], 10, 9, created, 10, "partial"))
    )
    stored, encoded = await spill_with_cap(ten_rows, cap_at_nine)
    assert stored == 9
    assert len(encoded) == cap_at_nine

    hundred_rows = [{"i": i} for i in range(100)]
    cap_at_ninety_nine = len(
        msgspec.json.encode(
            _StoredPayload("op", None, hundred_rows[:99], 100, 99, created, 100, "partial")
        )
    )
    stored, encoded = await spill_with_cap(hundred_rows, cap_at_ninety_nine)
    assert stored == 99
    assert len(encoded) == cap_at_ninety_nine

    one_row = [{"i": 1}]
    complete_size = len(
        msgspec.json.encode(_StoredPayload("op", None, one_row, 1, 1, created, 1, "complete"))
    )
    assert (await spill_with_cap(one_row, complete_size - 1))[0] == 0
    stored, encoded = await spill_with_cap(one_row, complete_size)
    assert stored == 1
    assert len(encoded) == complete_size


async def test_spill_reports_storage_coverage_for_complete_and_capped_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored fidelity is measured against captured rows, not source total_rows."""
    from datetime import UTC, datetime

    from meho_backplane.connectors.result_handle_store import _StoredPayload

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(
        store_module, "datetime", type("Clock", (), {"now": staticmethod(lambda _tz: fixed)})
    )
    rows = [{"i": i, "text": "x" * 20} for i in range(3)]

    async def payload_for(*, max_rows: int, max_record_bytes: int) -> _StoredPayload:
        fake = _FakeRedis()
        stored = await ResultHandleStore(fake).spill(
            tenant_id=uuid4(),
            operator_sub="op",
            handle_id=uuid4(),
            op_id=None,
            rows=rows,
            total_rows=99,
            ttl_seconds=60,
            max_rows=max_rows,
            max_record_bytes=max_record_bytes,
        )
        assert stored > 0
        return msgspec.json.decode(next(iter(fake.store.values())), type=_StoredPayload)

    complete = await payload_for(max_rows=3, max_record_bytes=10_000)
    assert (complete.captured_rows, complete.stored_rows, complete.storage_coverage) == (
        3,
        3,
        "complete",
    )
    assert complete.source_coverage == "unknown"

    row_capped = await payload_for(max_rows=2, max_record_bytes=10_000)
    assert (row_capped.captured_rows, row_capped.stored_rows, row_capped.storage_coverage) == (
        3,
        2,
        "partial",
    )

    one_row_size = len(
        msgspec.json.encode(
            _StoredPayload("op", None, rows[:1], 99, 1, fixed.isoformat(), 3, "partial")
        )
    )
    byte_capped = await payload_for(max_rows=3, max_record_bytes=one_row_size)
    assert (byte_capped.captured_rows, byte_capped.stored_rows, byte_capped.storage_coverage) == (
        3,
        1,
        "partial",
    )


@pytest.mark.parametrize("cap", [0, -1])
async def test_spill_explicit_nonpositive_byte_cap_skips_without_writing(cap: int) -> None:
    """An explicit disabled/invalid cap never resolves settings or writes a key."""
    fake = _FakeRedis()
    stored = await ResultHandleStore(fake).spill(
        tenant_id=uuid4(),
        operator_sub="op",
        handle_id=uuid4(),
        op_id=None,
        rows=[{"i": 1}],
        total_rows=1,
        ttl_seconds=60,
        max_rows=1,
        max_record_bytes=cap,
    )
    assert stored == 0
    assert fake.store == {}


async def test_spill_fails_open_for_an_unencodable_row_without_writing() -> None:
    """A real serialization TypeError preserves the no-spill fail-open contract."""
    fake = _FakeRedis()
    stored = await ResultHandleStore(fake).spill(
        tenant_id=uuid4(),
        operator_sub="op",
        handle_id=uuid4(),
        op_id=None,
        rows=[{"unsupported": object()}],
        total_rows=1,
        ttl_seconds=60,
        max_rows=1,
        max_record_bytes=10_000,
    )
    assert stored == 0
    assert fake.store == {}
