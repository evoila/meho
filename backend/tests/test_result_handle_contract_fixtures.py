# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Self-flipping regression fixtures for the result-handle fidelity work (#3630).

Each strict xfail describes the corrected contract that its owning follow-up
must make true.  The unmarked v1 codec probe protects the established spill
wire format while those changes land.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import msgspec
import pytest

from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    SpilledRowSet,
    _StoredPayload,
)
from meho_backplane.jsonflux.query.contract import (
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import _run_compiled_query
from meho_backplane.settings import get_settings


class _MemoryClient:
    """Minimal async Valkey shape for reducer-to-store lifecycle fixtures."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture(autouse=True)
def _settings_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the normal reducer settings before any marked assertion runs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _stored_result(rows: list[dict[str, Any]]) -> tuple[Any, SpilledRowSet]:
    """Run the reducer and read its stored rows through the production store API."""
    tenant_id = uuid4()
    store = ResultHandleStore(_MemoryClient())
    reduced, handle = await JsonFluxReducer(row_threshold=0, store=store).reduce(
        {"results": rows},
        None,
        context={"tenant_id": str(tenant_id), "operator_sub": "fixture-operator"},
    )
    assert isinstance(reduced, dict)
    assert handle is not None
    row_set = await store.fetch_rows(
        tenant_id=tenant_id,
        operator_sub="fixture-operator",
        handle_id=handle.handle_id,
    )
    assert row_set is not None
    return handle, row_set


async def _stored_rows(rows: list[dict[str, Any]]) -> SpilledRowSet:
    """Persist raw rows through the store for raw-only overflow coverage."""
    tenant_id, handle_id = uuid4(), uuid4()
    store = ResultHandleStore(_MemoryClient())
    assert await store.spill(
        tenant_id=tenant_id,
        operator_sub="fixture-operator",
        handle_id=handle_id,
        op_id="example.read",
        rows=rows,
        total_rows=len(rows),
        ttl_seconds=3600,
        max_rows=1000,
    )
    row_set = await store.fetch_rows(
        tenant_id=tenant_id,
        operator_sub="fixture-operator",
        handle_id=handle_id,
    )
    assert row_set is not None
    return row_set


async def test_late_and_final_fields_are_advertised_by_the_handle_schema() -> None:
    """Fields first present after the analyzer sample remain discoverable."""
    rows = [{"stable": index} for index in range(200)]
    rows.extend([{"stable": 200, "late": "row-201"}, {"stable": 201, "final": "last"}])

    handle, row_set = await _stored_result(rows)

    properties = handle.schema_["items"]["properties"]
    assert row_set.rows[-2:] == rows[-2:]
    assert {"stable", "late", "final"} <= set(properties)


@pytest.mark.parametrize(
    ("rows", "value"),
    [([{"enabled": False}], "false"), ([{"count": 1}], "1")],
    ids=["string-false-against-boolean", "string-one-against-integer"],
)
@pytest.mark.xfail(
    reason="A5/A6 literal-family validation", strict=True, raises=pytest.fail.Exception
)
async def test_string_literals_are_rejected_for_non_string_columns(
    rows: list[dict[str, Any]], value: str
) -> None:
    """DuckDB binding must not silently coerce text into bools or integers."""
    field = next(iter(rows[0]))
    with pytest.raises(QueryContractError):
        await _run_compiled_query(
            rows,
            ResultQuerySpec(filter=[{"field": field, "op": "=", "value": value}]),
            uuid4(),
            max_output_rows=10,
            timeout_seconds=5,
        )


@pytest.mark.xfail(reason="A2/A6 mixed-kind rehydration", strict=True, raises=AssertionError)
async def test_kind_switches_are_not_replaced_during_projection() -> None:
    """A value that changes JSON kind survives the projection unchanged."""
    values = [{"nested": "object"}, ["list"], "scalar"]
    _, row_set = await _stored_result([{"value": value} for value in values] * 17)
    projected, _, _ = await _run_compiled_query(
        row_set.rows,
        ResultQuerySpec(select=["value"]),
        uuid4(),
        max_output_rows=100,
        timeout_seconds=5,
    )
    assert [row["value"] for row in projected] == values * 17


@pytest.mark.xfail(reason="A2 empty-kind catalog", strict=True, raises=AssertionError)
async def test_empty_object_remains_distinguishable() -> None:
    """An empty object must remain an object through query projection."""
    _, row_set = await _stored_result([{"value": {}}] * 51)
    projected, _, _ = await _run_compiled_query(
        row_set.rows,
        ResultQuerySpec(select=["value"]),
        uuid4(),
        max_output_rows=100,
        timeout_seconds=5,
    )
    assert projected[0]["value"] == {}


async def test_empty_list_remains_distinguishable() -> None:
    """An empty list is already retained through the reducer, store, and query path."""
    _, row_set = await _stored_result([{"value": []}] * 51)
    projected, _, _ = await _run_compiled_query(
        row_set.rows,
        ResultQuerySpec(select=["value"]),
        uuid4(),
        max_output_rows=100,
        timeout_seconds=5,
    )
    assert projected[0]["value"] == []


async def test_int64_max_round_trips_through_reducer_store_and_query() -> None:
    """The supported signed-int64 maximum is already preserved and queryable."""
    number = 2**63 - 1
    _, row_set = await _stored_result([{"number": number}] * 51)
    projected, _, _ = await _run_compiled_query(
        row_set.rows,
        ResultQuerySpec(filter=[{"field": "number", "op": "=", "value": number}]),
        uuid4(),
        max_output_rows=100,
        timeout_seconds=5,
    )
    assert projected[0]["number"] == number


@pytest.mark.parametrize("number", [2**63, 2**64], ids=["int64-overflow", "uint64-overflow"])
@pytest.mark.xfail(reason="A2 raw-only overflow metadata", strict=True, raises=OverflowError)
async def test_integer_overflow_is_preserved_and_explicitly_raw_only(number: int) -> None:
    """Overflow values stay in the spill and receive a recoverable query denial."""
    row_set = await _stored_rows([{"number": number}] * 51)
    assert row_set.rows[0]["number"] == number
    with pytest.raises(QueryContractError):
        await _run_compiled_query(
            row_set.rows,
            ResultQuerySpec(select=["number"]),
            uuid4(),
            max_output_rows=100,
            timeout_seconds=5,
        )


@pytest.mark.xfail(reason="A2/A6 mixed numeric rehydration", strict=True, raises=AssertionError)
async def test_large_integer_mixed_with_float_keeps_its_exact_value() -> None:
    """A number above JavaScript-safe precision must not be rounded through float."""
    large = 2**53 + 1
    result, _, _ = await _run_compiled_query(
        [{"number": large}, {"number": 1.5}],
        ResultQuerySpec(order_by=[{"field": "number"}]),
        uuid4(),
        max_output_rows=10,
        timeout_seconds=5,
    )
    assert large in [row["number"] for row in result]


@pytest.mark.xfail(reason="A2/A6 case-preserving rehydration", strict=True, raises=AssertionError)
async def test_case_distinct_keys_remain_independently_queryable() -> None:
    """A DuckDB registration must not rename one of two case-distinct JSON keys."""
    result, _, _ = await _run_compiled_query(
        [{"Foo": "upper", "foo": "lower"}],
        ResultQuerySpec(),
        uuid4(),
        max_output_rows=10,
        timeout_seconds=5,
    )
    assert set(result[0]) == {"Foo", "foo"}


@pytest.mark.xfail(reason="A5 group output collision", strict=True, raises=pytest.fail.Exception)
def test_group_key_named_count_is_rejected_before_query_execution() -> None:
    """A group identity cannot be overwritten by COUNT(*)'s public alias."""
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(group_by=["count"], aggregate=[{"func": "COUNT"}]),
            ["count"],
            max_limit=10,
        )


def test_legacy_v1_codec_spill_round_trips() -> None:
    """The original stored payload remains readable through additive store changes."""
    raw = msgspec.json.encode(
        {
            "operator_sub": "operator",
            "op_id": "example.read",
            "rows": [{"a": 1}],
            "total_rows": 1,
            "stored_rows": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert msgspec.json.decode(raw, type=_StoredPayload).rows == [{"a": 1}]
