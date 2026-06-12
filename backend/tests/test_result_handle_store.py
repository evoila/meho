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
  ``False`` and ``fetch_window`` return ``None`` rather than raising.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis.exceptions

from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    SpilledWindow,
)


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
    assert ok is True
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
    assert ok is True

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
    """Empty rows, non-positive TTL, or zero cap → no spill (returns False)."""
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
    assert ok is False


async def test_store_fails_open_on_unreachable_client() -> None:
    """An unreachable Valkey makes spill return False and fetch return None."""
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
    assert ok is False

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
