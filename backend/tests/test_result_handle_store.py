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
import structlog

import meho_backplane.connectors.result_handle_store as store_module
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    ResultRecordKindError,
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

    ok = (
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
    ).stored_rows
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

    ok = (
        await store.spill(
            tenant_id=tenant,
            operator_sub="op-a",
            handle_id=handle,
            op_id=None,
            rows=_rows(500),
            total_rows=500,
            ttl_seconds=3600,
            max_rows=100,  # cap below the row count
        )
    ).stored_rows
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
    ok = (
        await store.spill(
            tenant_id=uuid4(),
            operator_sub="op-a",
            handle_id=uuid4(),
            op_id=None,
            rows=rows,
            total_rows=len(rows),
            ttl_seconds=ttl,
            max_rows=max_rows,
        )
    ).stored_rows
    assert ok == 0


async def test_store_fails_open_on_unreachable_client() -> None:
    """An unreachable Valkey makes spill return zero and fetch return None."""
    store = ResultHandleStore(_BrokenRedis())
    ok = (
        await store.spill(
            tenant_id=uuid4(),
            operator_sub="op-a",
            handle_id=uuid4(),
            op_id=None,
            rows=_rows(60),
            total_rows=60,
            ttl_seconds=3600,
            max_rows=10000,
        )
    ).stored_rows
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


async def test_fetch_record_and_decoders_split_the_read() -> None:
    """The raw ``GET`` and the decode are separate steps (#3640).

    ``fetch_record`` returns the stored bytes with no operator check; the
    sync decoders apply the owner check and the record-kind rules, so the
    read core can run them on its worker pool.
    """
    fake = _FakeRedis()
    store = ResultHandleStore(fake)
    tenant, handle = uuid4(), uuid4()
    await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.list",
        rows=_rows(3),
        total_rows=3,
        ttl_seconds=60,
        max_rows=100,
    )

    raw = await store.fetch_record(tenant_id=tenant, handle_id=handle)
    assert raw is not None
    assert await store.fetch_record(tenant_id=tenant, handle_id=uuid4()) is None

    rows = store.decode_rows(raw, operator_sub="op-a", handle_id=handle)
    assert rows is not None and [r["i"] for r in rows.rows] == [0, 1, 2]
    window = store.decode_window(raw, operator_sub="op-a", handle_id=handle, offset=1, limit=1)
    assert window is not None and [r["i"] for r in window.rows] == [1]
    # Another operator and a corrupt value are the same miss.
    assert store.decode_rows(raw, operator_sub="op-b", handle_id=handle) is None
    assert store.decode_rows(b"{", operator_sub="op-a", handle_id=handle) is None
    # A rows record with nothing beside its rows has no document to read.
    with pytest.raises(ResultRecordKindError):
        store.decode_document(raw, operator_sub="op-a", handle_id=handle)


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
    stored = (
        await store.spill(
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
    ).stored_rows
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
    ).stored_rows == 0


async def test_spill_rejects_wide_first_row_without_writing() -> None:
    fake = _FakeRedis()
    stored = (
        await ResultHandleStore(fake).spill(
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
    ).stored_rows
    assert stored == 0
    assert fake.store == {}


def test_legacy_payload_defaults_are_unknown_and_independent() -> None:
    from meho_backplane.connectors.result_handle_store import _StoredPayload

    raw = msgspec.json.encode(
        {
            "operator_sub": "op",
            "op_id": None,
            "rows": [{"i": 1}],
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
        stored = (
            await ResultHandleStore(fake).spill(
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
        ).stored_rows
        return stored, next(iter(fake.store.values()), b"")

    created = fixed.isoformat()
    for target_prefix in (9, 10, 99, 100):
        rows = [{"i": i} for i in range(target_prefix + 1)]
        exact_cap = len(
            msgspec.json.encode(
                _StoredPayload(
                    "op",
                    None,
                    rows[:target_prefix],
                    len(rows),
                    target_prefix,
                    created,
                    len(rows),
                    "partial",
                )
            )
        )
        stored, encoded = await spill_with_cap(rows, exact_cap)
        assert stored == target_prefix
        assert len(encoded) == exact_cap
        stored, encoded = await spill_with_cap(rows, exact_cap - 1)
        assert stored == target_prefix - 1
        assert len(encoded) <= exact_cap - 1

    complete_rows = [{"i": 1}, {"i": 2}]
    complete_size = len(
        msgspec.json.encode(_StoredPayload("op", None, complete_rows, 2, 2, created, 2, "complete"))
    )
    stored, encoded = await spill_with_cap(complete_rows, complete_size - 1)
    assert stored == 1
    assert len(encoded) <= complete_size - 1
    stored, encoded = await spill_with_cap(complete_rows, complete_size)
    assert stored == 2
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
        stored = (
            await ResultHandleStore(fake).spill(
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
        ).stored_rows
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
    stored = (
        await ResultHandleStore(fake).spill(
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
    ).stored_rows
    assert stored == 0
    assert fake.store == {}


async def test_spill_fails_open_for_an_unencodable_row_without_writing() -> None:
    """A real serialization TypeError preserves the no-spill fail-open contract."""
    fake = _FakeRedis()
    stored = (
        await ResultHandleStore(fake).spill(
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
    ).stored_rows
    assert stored == 0
    assert fake.store == {}


# ---------------------------------------------------------------------------
# #3639 — dedicated result-cache store: reader-first reads + writer gate
# ---------------------------------------------------------------------------


class _InfoRedis(_FakeRedis):
    """Fake dedicated client that also answers ``INFO memory``."""

    async def info(self, section: str | None = None) -> dict[str, Any]:
        assert section == "memory"
        return {"used_memory": 4096, "maxmemory": 805306368}


def _payload(operator_sub: str, rows: list[dict[str, Any]]) -> bytes:
    return msgspec.json.encode(
        store_module._StoredPayload(
            operator_sub=operator_sub,
            op_id="vault.kv.list",
            rows=rows,
            total_rows=len(rows),
            stored_rows=len(rows),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )


def _metric(name: str, labels: dict[str, str]) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


async def test_reader_first_returns_v2_record_from_dedicated_client() -> None:
    shared, dedicated = _FakeRedis(), _FakeRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated)
    tenant, handle = uuid4(), uuid4()
    dedicated.store[store_module._v2_key(tenant, handle)] = _payload("op-a", _rows(3))

    rows = await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
    window = await store.fetch_window(
        tenant_id=tenant, operator_sub="op-a", handle_id=handle, offset=0, limit=10
    )

    assert rows is not None and [r["i"] for r in rows.rows] == [0, 1, 2]
    assert window is not None and len(window.rows) == 3
    assert store_module._v2_key(tenant, handle).startswith("meho:reshandle:v2:")


async def test_reader_falls_back_to_legacy_key_for_same_tenant_only() -> None:
    shared, dedicated = _FakeRedis(), _FakeRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated)
    tenant, other_tenant, handle = uuid4(), uuid4(), uuid4()
    shared.store[store_module._key(tenant, handle)] = _payload("op-a", _rows(2))
    before = _metric("result_cache_reads_total", {"source": "legacy"})

    same = await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
    other = await store.fetch_rows(tenant_id=other_tenant, operator_sub="op-a", handle_id=handle)

    assert same is not None and same.stored_rows == 2
    assert other is None
    assert _metric("result_cache_reads_total", {"source": "legacy"}) == before + 1


async def test_reader_falls_back_to_legacy_when_dedicated_client_fails() -> None:
    shared = _FakeRedis()
    store = ResultHandleStore(shared, dedicated_client=_BrokenRedis())  # type: ignore[arg-type]
    tenant, handle = uuid4(), uuid4()
    shared.store[store_module._key(tenant, handle)] = _payload("op-a", _rows(1))

    window = await store.fetch_window(
        tenant_id=tenant, operator_sub="op-a", handle_id=handle, offset=0, limit=5
    )

    assert window is not None and window.stored_rows == 1


async def test_writer_gate_off_spills_legacy_key_on_shared_client() -> None:
    shared, dedicated = _FakeRedis(), _FakeRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated, writer_enabled=False)
    tenant, handle = uuid4(), uuid4()

    stored = await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.list",
        rows=_rows(4),
        total_rows=4,
        ttl_seconds=60,
        max_rows=100,
    )

    assert stored.stored_rows == 4
    assert list(shared.store) == [store_module._key(tenant, handle)]
    assert dedicated.store == {}
    assert not any(":v2:" in key for key in shared.store)


async def test_writer_gate_on_spills_v2_key_on_dedicated_client() -> None:
    shared, dedicated = _FakeRedis(), _InfoRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated, writer_enabled=True)
    tenant, handle = uuid4(), uuid4()

    stored = await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.list",
        rows=_rows(4),
        total_rows=4,
        ttl_seconds=60,
        max_rows=100,
    )

    assert stored.stored_rows == 4
    assert list(dedicated.store) == [store_module._v2_key(tenant, handle)]
    assert dedicated.last_ex == 60
    assert shared.store == {}
    window = await store.fetch_window(
        tenant_id=tenant, operator_sub="op-a", handle_id=handle, offset=0, limit=10
    )
    assert window is not None and window.stored_rows == 4


async def test_writer_gate_on_spills_document_record_to_dedicated_client() -> None:
    shared, dedicated = _FakeRedis(), _InfoRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated, writer_enabled=True)
    tenant, handle = uuid4(), uuid4()

    stored = await store.spill_document(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=handle,
        op_id="vault.kv.read",
        document={"name": "doc"},
        ttl_seconds=60,
    )

    assert stored is True
    assert list(dedicated.store) == [store_module._v2_key(tenant, handle)]
    assert shared.store == {}
    doc = await store.fetch_document(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
    assert doc is not None and doc.document == {"name": "doc"}


async def test_result_cache_metrics_are_aggregate_and_logs_carry_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    shared, dedicated = _FakeRedis(), _InfoRedis()
    store = ResultHandleStore(shared, dedicated_client=dedicated, writer_enabled=True)
    broken = ResultHandleStore(
        _FakeRedis(),  # type: ignore[arg-type]
        dedicated_client=_BrokenRedis(),  # type: ignore[arg-type]
        writer_enabled=True,
    )
    secret_rows = [{"name": "row-content-must-not-leak", "value": "sensitive-cell"}]
    admitted = {"outcome": "admitted", "namespace": "v2"}
    rejected = {"outcome": "rejected", "namespace": "v2"}
    before_admitted = _metric("result_cache_records_total", admitted)
    before_rejected = _metric("result_cache_records_total", rejected)
    before_v2 = _metric("result_cache_reads_total", {"source": "v2"})
    before_miss = _metric("result_cache_reads_total", {"source": "miss"})

    spill_kwargs: dict[str, Any] = {
        "operator_sub": "op-a",
        "op_id": "vault.kv.list",
        "rows": secret_rows,
        "total_rows": 1,
        "ttl_seconds": 60,
        "max_rows": 100,
    }
    tenant, handle = uuid4(), uuid4()
    # The module logger is cached after its first use. Give the store a
    # fresh logger so capture_logs sees its events in any test order.
    monkeypatch.setattr(store_module, "_log", structlog.get_logger(store_module.__name__))
    with capture_logs() as logs:
        assert (
            await store.spill(tenant_id=tenant, handle_id=handle, **spill_kwargs)
        ).stored_rows == 1
        assert (
            await broken.spill(tenant_id=tenant, handle_id=uuid4(), **spill_kwargs)
        ).stored_rows == 0
        await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=handle)
        await store.fetch_rows(tenant_id=tenant, operator_sub="op-a", handle_id=uuid4())

    assert _metric("result_cache_records_total", admitted) == before_admitted + 1
    assert _metric("result_cache_records_total", rejected) == before_rejected + 1
    assert _metric("result_cache_reads_total", {"source": "v2"}) == before_v2 + 1
    assert _metric("result_cache_reads_total", {"source": "miss"}) == before_miss + 1
    assert _metric("result_cache_memory_bytes", {"kind": "used"}) == 4096
    assert _metric("result_cache_memory_bytes", {"kind": "max"}) == 805306368
    rendered = json.dumps(logs, default=str)
    assert logs
    assert "row-content-must-not-leak" not in rendered
    assert "sensitive-cell" not in rendered


class _OomRedis(_InfoRedis):
    """Dedicated client at its ``maxmemory`` ceiling with ``noeviction``.

    Valkey answers every write with an OOM error but still serves reads
    and ``INFO``.
    """

    async def set(self, *_a: Any, **_k: Any) -> None:
        raise redis.exceptions.OutOfMemoryError(
            "OOM command not allowed when used memory > 'maxmemory'."
        )


class _SharedValkey(_FakeRedis):
    """Working shared client: result-handle keys, rate-limit counters, streams."""

    def __init__(self) -> None:
        super().__init__()
        self.counters: dict[str, int] = {}
        self.streams: dict[str, list[dict[str, Any]]] = {}

    def pipeline(self, transaction: bool = True) -> Any:
        from tests.test_broadcast_announce_rate_limit import _FakePipeline

        return _FakePipeline(self.counters)

    async def xadd(self, name: str, fields: dict[str, Any], **_k: Any) -> str:
        entries = self.streams.setdefault(name, [])
        entries.append(fields)
        return f"1700000000000-{len(entries) - 1}"

    def register_script(self, script: str) -> Any:
        from tests.broadcast_admission_helpers import FakeStreamClient

        return FakeStreamClient(xadd=self.xadd).register_script(script)


async def test_full_result_cache_does_not_block_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full result cache must not stop broadcast publish or the announce limit.

    The dedicated store refuses the write (``noeviction`` at ``maxmemory``).
    The spill fails open, and the shared Valkey still takes the
    rate-limit counter and the announcement.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from meho_backplane.broadcast import AgentAnnouncementEvent
    from meho_backplane.broadcast import publisher as publisher_module
    from meho_backplane.broadcast.publisher import publish_agent_announcement
    from meho_backplane.broadcast.rate_limit import enforce_announce_rate_limit

    shared, full = _SharedValkey(), _OomRedis()
    store = ResultHandleStore(shared, dedicated_client=full, writer_enabled=True)
    rejected = {"outcome": "rejected", "namespace": "v2"}
    before_rejected = _metric("result_cache_records_total", rejected)
    tenant = uuid4()

    stored = await store.spill(
        tenant_id=tenant,
        operator_sub="op-a",
        handle_id=uuid4(),
        op_id="vault.kv.list",
        rows=_rows(4),
        total_rows=4,
        ttl_seconds=60,
        max_rows=100,
    )

    assert stored.stored_rows == 0
    assert _metric("result_cache_records_total", rejected) == before_rejected + 1
    assert full.store == {}
    assert shared.store == {}

    # rate_limit.py and publisher.py import get_broadcast_client by name,
    # so patch each module's own reference.
    monkeypatch.setattr("meho_backplane.broadcast.rate_limit.get_broadcast_client", lambda: shared)
    monkeypatch.setattr(
        "meho_backplane.broadcast.rate_limit.get_settings",
        lambda: SimpleNamespace(broadcast_announce_rate_per_minute=10),
    )
    monkeypatch.setattr(publisher_module, "get_broadcast_client", lambda: shared)

    await enforce_announce_rate_limit(tenant, "op-a")
    result = await publish_agent_announcement(
        AgentAnnouncementEvent(
            tenant_id=tenant,
            principal_sub="op-a",
            activity="investigating",
            phase="start",
            ts=datetime.now(UTC),
            claim_id=uuid4(),
            owner_sub="agent:op-a",
            admission="agent_principal",
        )
    )

    assert list(shared.counters.values()) == [1]
    assert result.stream_entry_id == "1700000000000-0"
    assert [len(entries) for entries in shared.streams.values()] == [1]


async def test_get_result_handle_store_reuses_broadcast_client_when_urls_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``RESULT_CACHE_REDIS_URL`` → one shared client, writer gate off."""
    import meho_backplane.broadcast.client as broadcast_client

    shared = _FakeRedis()
    monkeypatch.delenv("RESULT_CACHE_REDIS_URL", raising=False)
    monkeypatch.delenv("RESULT_CACHE_WRITER_ENABLED", raising=False)
    monkeypatch.setattr(broadcast_client, "get_broadcast_client", lambda: shared)
    get_settings.cache_clear()
    store_module.reset_result_handle_store_for_testing()
    try:
        store = store_module.get_result_handle_store()
        assert store._client is shared
        assert store._dedicated is shared
        assert store._writer_enabled is False
    finally:
        store_module.reset_result_handle_store_for_testing()
        get_settings.cache_clear()


async def test_get_result_handle_store_builds_dedicated_client_and_disposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meho_backplane.broadcast.client as broadcast_client

    shared = _FakeRedis()
    monkeypatch.setenv("RESULT_CACHE_REDIS_URL", "redis://result-cache.test:6379/0")
    monkeypatch.setenv("RESULT_CACHE_WRITER_ENABLED", "true")
    monkeypatch.setattr(broadcast_client, "get_broadcast_client", lambda: shared)
    get_settings.cache_clear()
    store_module.reset_result_handle_store_for_testing()
    try:
        store = store_module.get_result_handle_store()
        assert store._client is shared
        assert store._dedicated is not shared
        assert store._writer_enabled is True
        await store_module.dispose_result_handle_store()
        assert store_module._STORE is None
    finally:
        store_module.reset_result_handle_store_for_testing()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("max_rows", "max_record_bytes", "storage_coverage"),
    [
        pytest.param(3, 10_000, "complete", id="uncapped"),
        pytest.param(2, 10_000, "partial", id="row-capped"),
        pytest.param(3, None, "partial", id="byte-capped"),
    ],
)
async def test_spill_keeps_source_coverage_unknown_on_every_cap_path(
    monkeypatch: pytest.MonkeyPatch,
    max_rows: int,
    max_record_bytes: int | None,
    storage_coverage: str,
) -> None:
    """Storage and source coverage are independent (#3676).

    A cap only says the stored handle dropped captured rows. It never
    says anything about how much of the vendor's data the call captured,
    so source coverage stays ``unknown`` whether or not a cap fired.
    """
    from datetime import UTC, datetime

    from meho_backplane.connectors.result_handle_store import _StoredPayload

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(
        store_module, "datetime", type("Clock", (), {"now": staticmethod(lambda _tz: fixed)})
    )
    rows = [{"i": i, "text": "x" * 20} for i in range(3)]
    if max_record_bytes is None:
        # Room for exactly one row, so the byte cap drops the other two.
        max_record_bytes = len(
            msgspec.json.encode(
                _StoredPayload("op", None, rows[:1], 3, 1, fixed.isoformat(), 3, "partial")
            )
        )
    fake = _FakeRedis()
    stored = (
        await ResultHandleStore(fake).spill(
            tenant_id=uuid4(),
            operator_sub="op",
            handle_id=uuid4(),
            op_id=None,
            rows=rows,
            total_rows=len(rows),
            ttl_seconds=60,
            max_rows=max_rows,
            max_record_bytes=max_record_bytes,
        )
    ).stored_rows
    assert stored > 0
    payload = msgspec.json.decode(next(iter(fake.store.values())), type=_StoredPayload)
    assert payload.storage_coverage == storage_coverage
    assert payload.source_coverage == "unknown"
