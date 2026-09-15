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
import pyarrow as pa
import pytest

from meho_backplane.connectors.result_handle_store import _StoredPayload
from meho_backplane.jsonflux.core.converter import normalize_data
from meho_backplane.jsonflux.query.contract import (
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import _run_compiled_query


async def _reduced_handle(rows: list[dict[str, Any]]) -> Any:
    """Materialize rows through the production reducer and return its handle."""
    reduced, handle = await JsonFluxReducer(row_threshold=0).reduce({"results": rows}, None)
    assert isinstance(reduced, dict)
    assert handle is not None
    return handle


@pytest.mark.xfail(reason="A2 all-row catalog", strict=True)
async def test_late_and_final_fields_are_advertised_by_the_handle_schema() -> None:
    """Fields first present after the analyzer sample remain discoverable."""
    rows = [{"stable": index} for index in range(200)]
    rows.extend([{"stable": 200, "late": "row-201"}, {"stable": 201, "final": "last"}])

    handle = await _reduced_handle(rows)

    properties = handle.schema_["items"]["properties"]
    assert {"stable", "late", "final"} <= set(properties)


@pytest.mark.parametrize(
    ("rows", "value"),
    [([{"enabled": False}], "false"), ([{"count": 1}], "1")],
    ids=["string-false-against-boolean", "string-one-against-integer"],
)
@pytest.mark.xfail(reason="A5/A6 literal-family validation", strict=True)
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


@pytest.mark.xfail(reason="A2/A6 mixed-kind rehydration", strict=True)
def test_kind_switches_are_not_replaced_during_projection() -> None:
    """A value that changes JSON kind survives the projection unchanged."""
    schema = pa.schema([pa.field("value", pa.struct([pa.field("nested", pa.string())]))])
    values = [{"nested": "object"}, ["list"], "scalar"]
    projected = [normalize_data({"value": value}, schema)["value"] for value in values]
    assert projected == values


@pytest.mark.parametrize("value", [{}, []], ids=["empty-object", "empty-list"])
@pytest.mark.xfail(reason="A2 empty-kind catalog", strict=True)
def test_empty_structures_remain_distinguishable(value: Any) -> None:
    """Empty objects and arrays are data, not missing values or empty coercions."""
    schema = pa.schema([pa.field("value", pa.struct([pa.field("known", pa.string())]))])
    assert normalize_data({"value": value}, schema)["value"] == value


@pytest.mark.parametrize(
    "number",
    [2**63 - 1, 2**63, 2**64],
    ids=["int64-max", "int64-overflow", "uint64-overflow"],
)
@pytest.mark.xfail(reason="A2 raw-only overflow metadata", strict=True)
async def test_integer_boundaries_remain_queryable_or_explicitly_raw_only(number: int) -> None:
    """Integer values do not disappear because projection assumes signed int64."""
    handle = await _reduced_handle([{"number": number}] * 51)
    assert "number" in handle.schema_["items"]["properties"]


@pytest.mark.xfail(reason="A2/A6 mixed numeric rehydration", strict=True)
async def test_large_integer_mixed_with_float_keeps_its_exact_value() -> None:
    """A number above JavaScript-safe precision must not be rounded through float."""
    large = 2**53 + 1
    result, _, _ = await _run_compiled_query(
        [{"number": large}, {"number": 1.5}],
        ResultQuerySpec(order_by=[{"field": "number"}]),
        __import__("uuid").uuid4(),
        max_output_rows=10,
        timeout_seconds=5,
    )
    assert large in [row["number"] for row in result]


@pytest.mark.xfail(reason="A2/A6 case-preserving rehydration", strict=True)
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


@pytest.mark.xfail(reason="A5 group output collision", strict=True)
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
        _StoredPayload(
            operator_sub="operator",
            op_id="example.read",
            rows=[{"a": 1}],
            total_rows=1,
            stored_rows=1,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )

    assert msgspec.json.decode(raw, type=_StoredPayload).rows == [{"a": 1}]
