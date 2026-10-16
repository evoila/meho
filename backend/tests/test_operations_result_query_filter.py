# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Core tests for the ``result_query`` bounded query surface (#3366).

Drives :func:`meho_backplane.operations.result_query.run_result_query`
against a fake store, pinning the four behaviours the acceptance criteria
name: the bounds (row cap + ``truncated`` flag, byte cap, wall-time budget
via a worker-thread ``conn.interrupt()``), coverage labelling when the spill
was capped, the isolation/not-found semantics reused from the paging read,
and end-to-end filter / aggregate correctness through the hardened engine.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import duckdb
import pytest

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.jsonflux.query.contract import QueryContractError, ResultQuerySpec
from tests._result_store_helpers import MemoryResultStore

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


def _operator(sub: str = "op-a", tenant_id: uuid.UUID | None = _TENANT) -> Operator:
    fields = {
        "sub": sub,
        "name": "Test Operator",
        "email": None,
        "raw_jwt": "<test-raw-jwt>",
        "tenant_id": tenant_id,
        "tenant_role": TenantRole.OPERATOR,
    }
    if tenant_id is None:
        # ``Operator.tenant_id`` is a required UUID; bypass validation to
        # exercise the core's defensive no-tenant-context branch directly.
        return Operator.model_construct(**fields)
    return Operator(**fields)


def _store(
    rows: list[dict[str, Any]],
    *,
    total_rows: int | None = None,
    owner_sub: str = "op-a",
) -> MemoryResultStore:
    """In-memory store serving one fixed row set, for any handle, to one operator."""
    store = MemoryResultStore()
    store.seed_any_handle(operator_sub=owner_sub, rows=rows, total_rows=total_rows)
    return store


def _stub_settings(*, rows: int = 500, out_bytes: int = 262144, timeout: int = 5) -> Any:
    return types.SimpleNamespace(
        result_query_max_output_rows=rows,
        result_query_max_output_bytes=out_bytes,
        result_query_timeout_seconds=timeout,
    )


def _install(monkeypatch: pytest.MonkeyPatch, store: MemoryResultStore, settings: Any) -> None:
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(rq, "get_settings", lambda: settings)


def _sample_rows(n: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "severity": "high" if i % 2 else "low", "project": f"p{i % 3}"} for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Filter / aggregate correctness through the hardened engine
# ---------------------------------------------------------------------------


async def test_filter_returns_only_matching_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(20)), _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(filter=[{"field": "severity", "op": "=", "value": "high"}]),
    )
    assert result["returned_rows"] == 10
    assert {row["severity"] for row in result["rows"]} == {"high"}
    assert result["coverage"] == "complete"


async def test_group_by_aggregate_returns_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(30)), _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            group_by=["project"],
            aggregate=[{"func": "COUNT"}],
            order_by=[{"field": "project"}],
        ),
    )
    counts = {row["project"]: row["count"] for row in result["rows"]}
    assert counts == {"p0": 10, "p1": 10, "p2": 10}


def _cpu_rows() -> list[dict[str, Any]]:
    """A number field that mixes whole and decimal values, as JSON often does."""
    return [{"name": "a", "cpu": 0}, {"name": "b", "cpu": 12.5}, {"name": "c", "cpu": 3}]


async def test_mixed_whole_and_decimal_numbers_filter_and_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _store(_cpu_rows()), _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            filter=[{"field": "cpu", "op": ">", "value": 1}],
            order_by=[{"field": "cpu", "direction": "desc"}],
        ),
    )
    assert [row["name"] for row in result["rows"]] == ["b", "c"]
    # Flat output is the raw stored row: the whole number stays an integer.
    assert result["rows"][1]["cpu"] == 3
    assert type(result["rows"][1]["cpu"]) is int


async def test_mixed_whole_and_decimal_numbers_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_cpu_rows()), _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(aggregate=[{"func": "SUM", "field": "cpu"}]),
    )
    assert result["rows"] == [{"sum_cpu": 15.5}]


async def test_mixed_whole_and_decimal_numbers_flat_query_keeps_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _store(_cpu_rows()), _stub_settings())
    result = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert result["rows"] == _cpu_rows()
    assert [type(row["cpu"]) for row in result["rows"]] == [int, float, int]


# ---------------------------------------------------------------------------
# Bounds: row cap + truncated flag
# ---------------------------------------------------------------------------


async def test_row_cap_truncates_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(60)), _stub_settings(rows=10))
    result = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert result["returned_rows"] == 10
    assert result["limit"] == 10
    assert result["truncated"] is True


async def test_under_cap_is_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(5)), _stub_settings(rows=10))
    result = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert result["returned_rows"] == 5
    assert result["truncated"] is False


async def test_caller_limit_below_cap_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(60)), _stub_settings(rows=500))
    result = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec(limit=3))
    assert result["returned_rows"] == 3
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Bounds: serialized-output byte cap (tested on wide rows)
# ---------------------------------------------------------------------------


async def test_output_byte_cap_rejects_wide_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    wide = [{"id": i, "blob": "x" * 5000} for i in range(50)]
    _install(monkeypatch, _store(wide), _stub_settings(out_bytes=2048))
    with pytest.raises(rq.ResultQueryOutputTooLargeError) as exc:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    # The error is actionable: it names the remedy (narrow the result).
    assert "select" in str(exc.value).lower()


async def test_output_byte_cap_lets_narrow_projection_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wide = [{"id": i, "blob": "x" * 5000} for i in range(50)]
    _install(monkeypatch, _store(wide), _stub_settings(out_bytes=2048))
    result = await rq.run_result_query(
        _operator(), uuid.uuid4(), ResultQuerySpec(select=["id"], limit=20)
    )
    assert result["returned_rows"] == 20
    assert all(set(row) == {"id"} for row in result["rows"])


# ---------------------------------------------------------------------------
# Catalog-aware compile + raw-row rehydration (#3635)
# ---------------------------------------------------------------------------


async def test_field_after_sample_window_is_queryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A field first seen deep in the row set is a valid filter/select target."""
    rows: list[dict[str, Any]] = [{"stable": i} for i in range(300)]
    rows.append({"stable": 300, "late": "row-301"})
    _install(monkeypatch, _store(rows), _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            filter=[{"field": "late", "op": "=", "value": "row-301"}],
            select=["stable", "late"],
        ),
    )
    assert result["rows"] == [{"stable": 300, "late": "row-301"}]


async def test_case_distinct_keys_coexist(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Foo`` and ``foo`` stay distinct keys in flat and grouped output."""
    rows = [{"Foo": "upper", "foo": "lower"}, {"Foo": "upper", "foo": "other"}]
    _install(monkeypatch, _store(rows), _stub_settings())

    flat = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            filter=[{"field": "foo", "op": "=", "value": "lower"}], select=["Foo", "foo"]
        ),
    )
    assert flat["rows"] == [{"Foo": "upper", "foo": "lower"}]

    whole = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert whole["rows"] == rows

    grouped = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            group_by=["Foo", "foo"],
            aggregate=[{"func": "COUNT"}],
            order_by=[{"field": "foo"}],
        ),
    )
    assert grouped["rows"] == [
        {"Foo": "upper", "foo": "lower", "count": 1},
        {"Foo": "upper", "foo": "other", "count": 1},
    ]
    for result in (flat, whole, grouped):
        assert all("foo_1" not in row for row in result["rows"])


async def test_flat_query_rehydrates_raw_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flat output is the raw stored value: no coercion, absent stays absent."""
    rows: list[dict[str, Any]] = [
        {"id": 1, "enabled": "false", "spec": {"Nested": [1, None]}, "maybe": None},
        {"id": 2, "enabled": True, "spec": "flat"},
    ]
    _install(monkeypatch, _store(rows), _stub_settings())

    whole = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert whole["rows"] == rows
    assert whole["rows"][0]["enabled"] == "false"

    selected = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(
            filter=[{"field": "id", "op": ">=", "value": 1}],
            select=["enabled", "spec", "maybe"],
            order_by=[{"field": "id", "direction": "desc"}],
        ),
    )
    assert selected["rows"] == [
        {"enabled": True, "spec": "flat"},
        {"enabled": "false", "spec": {"Nested": [1, None]}, "maybe": None},
    ]


# ---------------------------------------------------------------------------
# Bounds: wall-time budget via worker-thread conn.interrupt()
# ---------------------------------------------------------------------------


async def test_wall_time_budget_interrupts_a_slow_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query over the budget is aborted via ``conn.interrupt()``.

    The compiler only emits bounded single-table SELECTs, so to exercise the
    real interrupt path deterministically ``_execute_sync`` is swapped for a
    genuinely slow cross-join over the registered table — the same connection
    the core interrupts from the event loop on timeout. The worker's
    ``execute`` then raises ``InterruptException`` and the core surfaces the
    recoverable :class:`ResultQueryTimeoutError`.
    """
    _install(monkeypatch, _store(_sample_rows(400)), _stub_settings(timeout=1))

    def _slow(engine: Any, _compiled: Any) -> tuple[list[str], list[Any]]:
        cur = engine.conn.execute(
            "SELECT count(*) AS n FROM result a, result b, result c, result d"
        )
        return [d[0] for d in cur.description], cur.fetchall()

    monkeypatch.setattr(rq, "_execute_sync", _slow)
    with pytest.raises(rq.ResultQueryTimeoutError):
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())


# ---------------------------------------------------------------------------
# Coverage semantics: capped spill labelled as covering the stored subset
# ---------------------------------------------------------------------------


async def test_partial_coverage_when_spill_was_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(_sample_rows(60), total_rows=500)
    _install(monkeypatch, store, _stub_settings())
    result = await rq.run_result_query(
        _operator(),
        uuid.uuid4(),
        ResultQuerySpec(aggregate=[{"func": "COUNT"}]),
    )
    assert result["stored_rows"] == 60
    assert result["total_rows"] == 500
    assert result["coverage"] == "partial"
    assert result["coverage_note"] is not None
    assert "60" in result["coverage_note"] and "500" in result["coverage_note"]
    # The aggregate is over the stored subset, not the whole inventory.
    assert result["rows"][0]["count"] == 60


async def test_complete_coverage_when_whole_set_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _store(_sample_rows(30)), _stub_settings())
    result = await rq.run_result_query(
        _operator(), uuid.uuid4(), ResultQuerySpec(aggregate=[{"func": "COUNT"}])
    )
    assert result["coverage"] == "complete"
    assert result["coverage_note"] is None


# ---------------------------------------------------------------------------
# Isolation + error propagation
# ---------------------------------------------------------------------------


async def test_no_tenant_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(5)), _stub_settings())
    with pytest.raises(rq.ResultHandleNotFoundError):
        await rq.run_result_query(_operator(tenant_id=None), uuid.uuid4(), ResultQuerySpec())


async def test_cross_operator_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _store(_sample_rows(5), owner_sub="op-a"), _stub_settings())
    with pytest.raises(rq.ResultHandleNotFoundError):
        await rq.run_result_query(_operator(sub="op-b"), uuid.uuid4(), ResultQuerySpec())


async def test_unknown_field_surfaces_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _store(_sample_rows(5)), _stub_settings())
    with pytest.raises(QueryContractError):
        await rq.run_result_query(
            _operator(), uuid.uuid4(), ResultQuerySpec(select=["does_not_exist"])
        )


# ---------------------------------------------------------------------------
# The engine the core builds is the hardened one (sanity anchor)
# ---------------------------------------------------------------------------


def test_core_reuses_the_hardened_engine() -> None:
    """The core registers rows in a locked, single-threaded engine."""
    engine = rq.QueryEngine()
    try:
        with pytest.raises(duckdb.InvalidInputException):
            engine.conn.execute("SET threads=4")
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Paging read: serialized-output byte cap shared with the query path (#3387)
# ---------------------------------------------------------------------------


async def test_paging_window_over_byte_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window within the row cap but over the byte budget is rejected.

    The paging read applies the same ``result_query_max_output_bytes`` ceiling
    the query path does, so no agent ever receives an over-budget page.
    """
    wide = [{"id": i, "blob": "x" * 5000} for i in range(50)]
    _install(monkeypatch, _store(wide), _stub_settings(out_bytes=2048))
    with pytest.raises(rq.ResultQueryOutputTooLargeError) as exc:
        await rq.read_result_window(_operator(), uuid.uuid4(), offset=0, limit=50)
    # Paging remediation names a smaller `limit` — never `select` / `filter`.
    msg = str(exc.value).lower()
    assert "limit" in msg
    assert "select" not in msg and "filter" not in msg


async def test_paging_window_in_budget_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-budget window is returned verbatim — the byte check is transparent."""
    rows = _sample_rows(10)
    _install(monkeypatch, _store(rows), _stub_settings(out_bytes=262144))
    result = await rq.read_result_window(_operator(), uuid.uuid4(), offset=0, limit=50)
    assert result["returned_rows"] == 10
    assert result["rows"] == rows
    assert result["truncated"] is False
