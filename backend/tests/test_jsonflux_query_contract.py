# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract-compiler tests for the ``result_query`` query surface (#3366).

The compiler (:mod:`meho_backplane.jsonflux.query.contract`) turns a small
typed argument grammar into exactly one parameterized, read-only ``SELECT``
over the single registered handle table. These tests pin the safety
properties that make the surface stronger than sanitizing a caller SQL
string: the unsafe forms are simply not expressible.

* No raw-SQL argument exists on the spec at all.
* Every referenced field is checked against the handle's result field
  catalog; an unknown field is rejected (Kubernetes field-selector style),
  and a field the catalog cannot query is rejected in predicates (#3635).
* Caller field names never reach the SQL text: each resolves to its
  catalog-generated internal column, and output columns use generated
  aliases mapped back to public names by position (#3635).
* Operators and aggregate functions are fixed allow-lists (a value outside
  them fails at model construction).
* The predicate / group / order caps are enforced at construction.
* Caller values are bound as parameters, never interpolated — so a SQL
  fragment passed as a value cannot alter the statement structure or smuggle
  a second statement / a write / DDL.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.jsonflux.query.contract import (
    RESULT_TABLE,
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.jsonflux.query.engine import QueryEngine
from meho_backplane.jsonflux.query.json_pointer import (
    JsonPointerError,
    parse_json_pointer,
    resolve_json_pointer,
)
from meho_backplane.jsonflux.query.result_catalog import (
    AdmissionGuard,
    ResultFieldCatalog,
    build_catalog,
)
from meho_backplane.operations.result_query import _project_rows


def _catalog(rows: list[dict[str, Any]]) -> ResultFieldCatalog:
    return build_catalog(rows, guard=AdmissionGuard())


# Catalog columns are generated in sorted field order:
# host=_rr_v_0000, id=_rr_v_0001, memoryMB=_rr_v_0002, project=_rr_v_0003,
# severity=_rr_v_0004.
_CATALOG = _catalog([{"id": 1, "severity": "high", "project": "p0", "memoryMB": 512, "host": "h1"}])

_MAX = 500


def _compile(spec: ResultQuerySpec) -> tuple[str, list[object]]:
    compiled = compile_query(spec, _CATALOG, max_limit=_MAX)
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


def test_empty_spec_compiles_to_a_bounded_ordinal_select() -> None:
    """A full read-back selects only the hidden row ordinal, in stored order."""
    compiled = compile_query(ResultQuerySpec(), _CATALOG, max_limit=_MAX)
    assert compiled.sql == (
        f'SELECT "_rr_ord" FROM "{RESULT_TABLE}" ORDER BY "_rr_ord" LIMIT {_MAX + 1}'
    )
    assert compiled.params == []
    assert compiled.output_plan.ordinal
    assert compiled.output_plan.select is None


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
    sql, params = _compile(ResultQuerySpec(filter=[{"field": "host", "op": "=", "value": payload}]))
    assert sql == (
        f'SELECT "_rr_ord" FROM "{RESULT_TABLE}" WHERE "_rr_v_0000" = ? '
        f'ORDER BY "_rr_ord" LIMIT {_MAX + 1}'
    )
    assert params == [payload]
    assert "DROP" not in sql


def test_in_operator_expands_to_one_placeholder_per_value() -> None:
    sql, params = _compile(
        ResultQuerySpec(filter=[{"field": "severity", "op": "IN", "value": ["high", "crit"]}])
    )
    assert '"_rr_v_0004" IN (?, ?)' in sql
    assert params == ["high", "crit"]


def test_is_null_takes_no_value_and_binds_no_param() -> None:
    sql, params = _compile(ResultQuerySpec(filter=[{"field": "host", "op": "IS NULL"}]))
    assert '"_rr_v_0000" IS NULL' in sql
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
    """A field not in the handle's catalog fails compilation."""
    with pytest.raises(QueryContractError, match="unknown field 'nope'"):
        compile_query(spec, _CATALOG, max_limit=_MAX)


def test_injection_shaped_field_name_is_an_unknown_field() -> None:
    """A field carrying SQL is not a known column → rejected, never emitted."""
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(select=['id"; DROP TABLE result; --']),
            _CATALOG,
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


def test_more_than_1000_in_values_is_rejected() -> None:
    """An `IN` value list past the cap fails at construction (bounds expansion)."""
    with pytest.raises(ValidationError):
        ResultQuerySpec(filter=[{"field": "id", "op": "IN", "value": list(range(1001))}])


def test_in_value_list_at_cap_compiles() -> None:
    """A 1000-element `IN` list constructs and compiles to one placeholder each."""
    values = list(range(1000))
    sql, params = _compile(ResultQuerySpec(filter=[{"field": "id", "op": "IN", "value": values}]))
    assert sql.count("?") == 1000
    assert params == values


def test_more_than_64_select_columns_is_rejected() -> None:
    """A `select` projection past the cap fails at construction (bounds expansion)."""
    with pytest.raises(ValidationError):
        ResultQuerySpec(select=["id"] * 65)


def test_select_at_cap_compiles() -> None:
    """A 64-column `select` constructs and compiles into the output plan."""
    compiled = compile_query(ResultQuerySpec(select=["id"] * 64), _CATALOG, max_limit=_MAX)
    assert compiled.sql.startswith('SELECT "_rr_ord" ')
    assert compiled.output_plan.select == ("id",) * 64


def test_more_than_eight_aggregates_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(aggregate=[{"func": "COUNT"}] * 9)


def test_eight_aggregates_are_accepted_and_exposed_in_schema() -> None:
    spec = ResultQuerySpec(aggregate=[{"func": "COUNT"}] * 8)
    assert len(spec.aggregate) == 8
    assert ResultQuerySpec.model_json_schema()["properties"]["aggregate"]["maxItems"] == 8


def test_extra_top_level_argument_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(bogus=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Aggregate / group projection shaping
# ---------------------------------------------------------------------------


def test_group_by_with_aggregate_projects_keys_then_aggregates() -> None:
    compiled = compile_query(
        ResultQuerySpec(
            group_by=["project"],
            aggregate=[{"func": "COUNT"}, {"func": "SUM", "field": "memoryMB"}],
        ),
        _CATALOG,
        max_limit=_MAX,
    )
    assert (
        '"_rr_v_0003" AS "_rr_o_0", COUNT(*) AS "_rr_o_1", SUM("_rr_v_0002") AS "_rr_o_2"'
        in compiled.sql
    )
    assert 'GROUP BY "_rr_v_0003"' in compiled.sql
    assert not compiled.output_plan.ordinal
    assert compiled.output_plan.columns == ("project", "count", "sum_memoryMB")


def test_duplicate_group_key_is_rejected() -> None:
    with pytest.raises(QueryContractError, match="duplicate output column"):
        _compile(ResultQuerySpec(group_by=["project", "project"], aggregate=[{"func": "COUNT"}]))


def test_duplicate_group_key_without_aggregate_is_rejected() -> None:
    with pytest.raises(QueryContractError, match="duplicate output column"):
        _compile(ResultQuerySpec(group_by=["project", "project"]))


def test_group_key_equal_to_aggregate_alias_does_not_collapse() -> None:
    """Equal public names are rejected; case-distinct ones stay separate."""
    catalog = _catalog([{"count": 1, "Count": 2, "id": 1}])
    with pytest.raises(QueryContractError, match="duplicate aggregate output 'count'"):
        compile_query(
            ResultQuerySpec(group_by=["count"], aggregate=[{"func": "COUNT"}]),
            catalog,
            max_limit=_MAX,
        )
    compiled = compile_query(
        ResultQuerySpec(group_by=["Count"], aggregate=[{"func": "COUNT"}]),
        catalog,
        max_limit=_MAX,
    )
    assert compiled.output_plan.columns == ("Count", "count")
    assert compiled.output_plan.rehydrate([(2, 1)], raw_rows=[]) == [{"Count": 2, "count": 1}]


# ---------------------------------------------------------------------------
# Catalog queryability: non-queryable fields only come back raw via `select`
# ---------------------------------------------------------------------------


_MIXED_CATALOG = _catalog([{"mixed": "text", "id": 1}, {"mixed": 7, "id": 2}])


@pytest.mark.parametrize(
    ("spec", "role"),
    [
        (ResultQuerySpec(filter=[{"field": "mixed", "op": "IS NULL"}]), "`filter`"),
        (ResultQuerySpec(order_by=[{"field": "mixed"}]), "`order_by`"),
        (ResultQuerySpec(group_by=["mixed"]), "`group_by`"),
        (ResultQuerySpec(aggregate=[{"func": "MAX", "field": "mixed"}]), "`aggregate`"),
    ],
)
def test_mixed_field_rejected_in_predicate_returnable_in_select(
    spec: ResultQuerySpec, role: str
) -> None:
    with pytest.raises(QueryContractError) as exc:
        compile_query(spec, _MIXED_CATALOG, max_limit=_MAX)
    assert isinstance(exc.value, QueryContractError)
    message = str(exc.value)
    assert "'mixed'" in message and role in message
    assert "mixed_non_null_kinds" in message
    assert "`select`" in message and "paging" in message

    compiled = compile_query(
        ResultQuerySpec(filter=[{"field": "id", "op": "=", "value": 2}], select=["mixed"]),
        _MIXED_CATALOG,
        max_limit=_MAX,
    )
    assert compiled.output_plan.select == ("mixed",)
    assert compiled.output_plan.rehydrate(
        [(1,)], raw_rows=[{"mixed": "text", "id": 1}, {"mixed": 7, "id": 2}]
    ) == [{"mixed": 7}]


def test_container_field_is_rejected_in_filter() -> None:
    catalog = _catalog([{"tags": [True]}])
    with pytest.raises(QueryContractError, match="container_value"):
        compile_query(
            ResultQuerySpec(filter=[{"field": "tags", "op": "=", "value": "x"}]),
            catalog,
            max_limit=_MAX,
        )


# ---------------------------------------------------------------------------
# Typed filter literals, checked against the catalog scalar family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "value"),
    [(True, True), (1, 1), (1.5, 1.5), (1.5, 2), ("ok", "ok")],
)
def test_native_typed_filter_literal_compiles(sample: object, value: object) -> None:
    compiled = compile_query(
        ResultQuerySpec(filter=[{"field": "value", "op": "=", "value": value}]),
        _catalog([{"value": sample}]),
        max_limit=_MAX,
    )
    assert compiled.params == [value]


@pytest.mark.parametrize(
    ("sample", "value"),
    [(False, "false"), (1, "1"), (1.5, True), ("ok", 1)],
)
def test_wrong_typed_filter_literal_is_rejected(sample: object, value: object) -> None:
    with pytest.raises(QueryContractError, match="column type"):
        compile_query(
            ResultQuerySpec(filter=[{"field": "value", "op": "=", "value": value}]),
            _catalog([{"value": sample}]),
            max_limit=_MAX,
        )


def test_every_in_literal_is_validated_against_column_type() -> None:
    with pytest.raises(QueryContractError, match="got str"):
        compile_query(
            ResultQuerySpec(filter=[{"field": "value", "op": "IN", "value": [1, "2"]}]),
            _catalog([{"value": 1}]),
            max_limit=_MAX,
        )


def test_unknown_field_suggestions_are_bounded_and_sorted() -> None:
    known = _catalog([{f"field_{index:02d}": index for index in range(21)}])
    with pytest.raises(QueryContractError) as exc:
        compile_query(ResultQuerySpec(select=["missing"]), known, max_limit=_MAX)
    message = str(exc.value)
    assert "field_00" in message and "field_19" in message
    assert "field_20" not in message
    assert "(and 1 more)" in message


def test_unknown_field_suggestions_include_all_at_boundary() -> None:
    known = _catalog([{f"field_{index:02d}": index for index in range(20)}])
    with pytest.raises(QueryContractError) as exc:
        compile_query(ResultQuerySpec(select=["missing"]), known, max_limit=_MAX)
    assert "field_19" in str(exc.value)
    assert "more)" not in str(exc.value)


def test_select_alongside_aggregate_is_rejected() -> None:
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(select=["id"], aggregate=[{"func": "COUNT"}]),
            _CATALOG,
            max_limit=_MAX,
        )


def test_non_count_aggregate_requires_a_field() -> None:
    with pytest.raises(QueryContractError):
        compile_query(
            ResultQuerySpec(aggregate=[{"func": "SUM"}]),
            _CATALOG,
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
            _CATALOG,
            max_limit=_MAX,
        )


# ---------------------------------------------------------------------------
# Limit clamping
# ---------------------------------------------------------------------------


def test_limit_clamps_to_max_and_fetches_one_extra() -> None:
    compiled = compile_query(ResultQuerySpec(limit=100000), _CATALOG, max_limit=_MAX)
    assert compiled.effective_limit == _MAX
    assert compiled.sql.endswith(f"LIMIT {_MAX + 1}")


def test_caller_limit_below_max_is_honoured() -> None:
    compiled = compile_query(ResultQuerySpec(limit=10), _CATALOG, max_limit=_MAX)
    assert compiled.effective_limit == 10
    assert compiled.sql.endswith("LIMIT 11")


# ---------------------------------------------------------------------------
# End-to-end inertness: a DROP payload as a filter value executes as data
# ---------------------------------------------------------------------------


def test_drop_payload_executes_inert() -> None:
    """A `DROP TABLE` payload bound as a filter value cannot mutate the store.

    This closes the loop that ``test_filter_value_is_bound_not_interpolated``
    only proves at the SQL-string level: register real rows in a hardened
    :class:`QueryEngine`, compile a spec whose filter value is a multi-statement
    SQL payload, actually run the compiled statement with its bound params, and
    assert the ``result`` table survives with an unchanged row count — the
    payload lands as a parameter (matching nothing), never as executed SQL.
    """
    payload = "1; DROP TABLE result; --"
    rows = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
        {"id": 3, "name": "gamma"},
    ]
    engine = QueryEngine()
    try:
        # Mirror the core: catalog from the rows, explicit projection registered.
        catalog = _catalog(rows)
        engine.conn.register(RESULT_TABLE, _project_rows(rows, catalog))
        before = engine.conn.execute(f"SELECT COUNT(*) FROM {RESULT_TABLE}").fetchone()[0]
        assert before == len(rows)

        compiled = compile_query(
            ResultQuerySpec(filter=[{"field": "name", "op": "=", "value": payload}]),
            catalog,
            max_limit=_MAX,
        )
        assert "DROP" not in compiled.sql
        assert compiled.params == [payload]

        # Execute the compiled SELECT exactly as the core does: bound params.
        matched = engine.conn.execute(compiled.sql, compiled.params).fetchall()
        assert matched == []  # the payload string matches no row

        # The table still exists and its row count is untouched.
        after = engine.conn.execute(f"SELECT COUNT(*) FROM {RESULT_TABLE}").fetchone()[0]
        assert after == before
        # DESCRIBE would raise CatalogException if the table had been dropped.
        assert engine.conn.execute(f"DESCRIBE {RESULT_TABLE}").fetchall()
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# RFC 6901 JSON Pointer (#3633)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "tokens"),
    [
        ("", []),
        ("/", [""]),
        ("/foo/0", ["foo", "0"]),
        ("/a~1b", ["a/b"]),
        ("/m~0n", ["m~n"]),
        # Decode order matters: "~01" is "~" + "1", never "/".
        ("/~01", ["~1"]),
    ],
)
def test_parse_json_pointer_decodes_tokens(pointer: str, tokens: list[str]) -> None:

    assert parse_json_pointer(pointer) == tokens


@pytest.mark.parametrize("pointer", ["foo", "/a~", "/a~2", "/~x/b"])
def test_parse_json_pointer_rejects_bad_syntax(pointer: str) -> None:

    with pytest.raises(JsonPointerError):
        parse_json_pointer(pointer)


def test_resolve_json_pointer_follows_the_rfc_examples() -> None:
    """The RFC 6901 section 5 example document."""

    doc = {
        "foo": ["bar", "baz"],
        "": 0,
        "a/b": 1,
        "c%d": 2,
        "e^f": 3,
        "g|h": 4,
        "i\\j": 5,
        'k"l': 6,
        " ": 7,
        "m~n": 8,
    }
    cases = {
        "": doc,
        "/foo": ["bar", "baz"],
        "/foo/0": "bar",
        "/": 0,
        "/a~1b": 1,
        "/c%d": 2,
        "/e^f": 3,
        "/g|h": 4,
        "/i\\j": 5,
        '/k"l': 6,
        "/ ": 7,
        "/m~0n": 8,
    }
    for pointer, expected in cases.items():
        assert resolve_json_pointer(doc, parse_json_pointer(pointer)) == expected, pointer


@pytest.mark.parametrize("pointer", ["/foo/-", "/foo/01", "/foo/2", "/foo/+1", "/nope", "/foo/0/x"])
def test_resolve_json_pointer_rejects_unresolvable_steps(pointer: str) -> None:

    with pytest.raises(JsonPointerError):
        resolve_json_pointer({"foo": ["bar", "baz"]}, parse_json_pointer(pointer))


def test_compile_query_refuses_a_path_spec() -> None:
    """A ``path`` read is never compiled to SQL (#3633)."""
    with pytest.raises(QueryContractError, match="path"):
        compile_query(ResultQuerySpec(path="/a"), ["a"], max_limit=10)
