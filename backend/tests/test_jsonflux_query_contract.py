# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract-compiler tests for the ``result_query`` query surface (#3366).

The compiler (:mod:`meho_backplane.jsonflux.query.contract`) turns a small
typed argument grammar into exactly one parameterized, read-only ``SELECT``
over the single registered handle table. These tests pin the safety
properties that make the surface stronger than sanitizing a caller SQL
string: the unsafe forms are simply not expressible.

* No raw-SQL argument exists on the spec at all.
* Every referenced field is checked against the handle's known columns; an
  unknown field is rejected (Kubernetes field-selector style).
* Operators and aggregate functions are fixed allow-lists (a value outside
  them fails at model construction).
* The predicate / group / order caps are enforced at construction.
* Caller values are bound as parameters, never interpolated — so a SQL
  fragment passed as a value cannot alter the statement structure or smuggle
  a second statement / a write / DDL.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_backplane.jsonflux.query.contract import (
    RESULT_TABLE,
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)

_COLUMNS = ["id", "severity", "project", "memoryMB", "host"]

_MAX = 500


def _compile(spec: ResultQuerySpec) -> tuple[str, list[object]]:
    compiled = compile_query(spec, _COLUMNS, max_limit=_MAX)
    return compiled.sql, compiled.params


# ---------------------------------------------------------------------------
# The statement is always a single read-only SELECT
# ---------------------------------------------------------------------------


def test_no_raw_sql_argument_is_expressible() -> None:
    """The spec has no ``sql``/``query``/``expr`` field, and forbids extras."""
    fields = set(ResultQuerySpec.model_fields)
    assert "sql" not in fields
    assert not fields & {"query", "expr", "statement", "raw"}
    with pytest.raises(ValidationError):
        ResultQuerySpec(sql="SELECT 1")  # type: ignore[call-arg]


def test_empty_spec_compiles_to_a_bounded_select_star() -> None:
    sql, params = _compile(ResultQuerySpec())
    assert sql == f'SELECT * FROM "{RESULT_TABLE}" LIMIT {_MAX + 1}'
    assert params == []


def test_compiled_sql_is_exactly_one_select_statement() -> None:
    """Every compiled statement starts with SELECT and has no separator."""
    specs = [
        ResultQuerySpec(),
        ResultQuerySpec(filter=[{"field": "id", "op": "=", "value": 1}]),
        ResultQuerySpec(group_by=["severity"], aggregate=[{"func": "COUNT"}]),
        ResultQuerySpec(select=["id", "host"], order_by=[{"field": "id", "direction": "desc"}]),
    ]
    for spec in specs:
        sql, _ = _compile(spec)
        assert sql.startswith("SELECT ")
        assert ";" not in sql
        # No write/DDL/PRAGMA verbs are ever emitted.
        upper = sql.upper()
        for forbidden in (" INSERT", " UPDATE", " DELETE", " DROP", " CREATE", "PRAGMA", "ATTACH"):
            assert forbidden not in upper


# ---------------------------------------------------------------------------
# Values bind as parameters — writes/DDL/multi-statement not smuggle-able
# ---------------------------------------------------------------------------


def test_filter_value_is_bound_not_interpolated() -> None:
    """A SQL-injection payload in a value lands in params, never in the SQL."""
    payload = "1; DROP TABLE result; --"
    sql, params = _compile(ResultQuerySpec(filter=[{"field": "id", "op": "=", "value": payload}]))
    assert sql == f'SELECT * FROM "{RESULT_TABLE}" WHERE "id" = ? LIMIT {_MAX + 1}'
    assert params == [payload]
    assert "DROP" not in sql


def test_in_operator_expands_to_one_placeholder_per_value() -> None:
    sql, params = _compile(
        ResultQuerySpec(filter=[{"field": "severity", "op": "IN", "value": ["high", "crit"]}])
    )
    assert '"severity" IN (?, ?)' in sql
    assert params == ["high", "crit"]


def test_is_null_takes_no_value_and_binds_no_param() -> None:
    sql, params = _compile(ResultQuerySpec(filter=[{"field": "host", "op": "IS NULL"}]))
    assert '"host" IS NULL' in sql
    assert params == []


# ---------------------------------------------------------------------------
# Field-vs-schema validation (unknown field rejected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        ResultQuerySpec(filter=[{"field": "nope", "op": "=", "value": 1}]),
        ResultQuerySpec(select=["nope"]),
        ResultQuerySpec(group_by=["nope"], aggregate=[{"func": "COUNT"}]),
        ResultQuerySpec(aggregate=[{"func": "SUM", "field": "nope"}]),
        ResultQuerySpec(order_by=[{"field": "nope"}]),
    ],
)
def test_unknown_field_is_rejected(spec: ResultQuerySpec) -> None:
    """A field not in the handle's known columns fails compilation."""
    with pytest.raises(QueryContractError):
        compile_query(spec, _COLUMNS, max_limit=_MAX)


def test_injection_shaped_field_name_is_an_unknown_field() -> None:
    """A field carrying SQL is not a known column → rejected, never emitted."""
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(select=['id"; DROP TABLE result; --']),
            _COLUMNS,
            max_limit=_MAX,
        )


# ---------------------------------------------------------------------------
# Operator / aggregate allow-lists (fixed vocabularies)
# ---------------------------------------------------------------------------


def test_operator_outside_allow_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(filter=[{"field": "id", "op": "LIKE", "value": "x%"}])


def test_aggregate_function_outside_allow_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(aggregate=[{"func": "MEDIAN", "field": "memoryMB"}])


# ---------------------------------------------------------------------------
# Caps (predicate ≤ 10, group_by ≤ 4, order_by ≤ 4)
# ---------------------------------------------------------------------------


def test_more_than_ten_predicates_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(filter=[{"field": "id", "op": "=", "value": i} for i in range(11)])


def test_more_than_four_group_by_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(group_by=["id", "severity", "project", "host", "memoryMB"])


def test_more_than_four_order_by_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(
            order_by=[
                {"field": "id"},
                {"field": "severity"},
                {"field": "project"},
                {"field": "host"},
                {"field": "memoryMB"},
            ]
        )


def test_extra_top_level_argument_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(bogus=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Aggregate / group projection shaping
# ---------------------------------------------------------------------------


def test_group_by_with_aggregate_projects_keys_then_aggregates() -> None:
    sql, _ = _compile(
        ResultQuerySpec(
            group_by=["project"],
            aggregate=[{"func": "COUNT"}, {"func": "SUM", "field": "memoryMB"}],
        )
    )
    assert '"project", COUNT(*) AS "count", SUM("memoryMB") AS "sum_memoryMB"' in sql
    assert 'GROUP BY "project"' in sql


def test_select_alongside_aggregate_is_rejected() -> None:
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(select=["id"], aggregate=[{"func": "COUNT"}]),
            _COLUMNS,
            max_limit=_MAX,
        )


def test_non_count_aggregate_requires_a_field() -> None:
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(aggregate=[{"func": "SUM"}]),
            _COLUMNS,
            max_limit=_MAX,
        )


def test_order_by_non_group_key_in_grouped_query_is_rejected() -> None:
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(
                group_by=["project"],
                aggregate=[{"func": "COUNT"}],
                order_by=[{"field": "id"}],
            ),
            _COLUMNS,
            max_limit=_MAX,
        )


# ---------------------------------------------------------------------------
# Limit clamping
# ---------------------------------------------------------------------------


def test_limit_clamps_to_max_and_fetches_one_extra() -> None:
    compiled = compile_query(ResultQuerySpec(limit=100000), _COLUMNS, max_limit=_MAX)
    assert compiled.effective_limit == _MAX
    assert compiled.sql.endswith(f"LIMIT {_MAX + 1}")


def test_caller_limit_below_max_is_honoured() -> None:
    compiled = compile_query(ResultQuerySpec(limit=10), _COLUMNS, max_limit=_MAX)
    assert compiled.effective_limit == 10
    assert compiled.sql.endswith("LIMIT 11")
