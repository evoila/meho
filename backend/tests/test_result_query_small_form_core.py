# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Core tests for the small form of ``result_query`` (#4174).

The query contract (``contains`` and the place of an error), the
``no_match`` block, and the core that adds it only when the MCP tool asks
for the small form. The MCP side is in
``test_mcp_result_query_small_form.py``.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.jsonflux.query.contract import (
    QueryContractError,
    ResultQuerySpec,
    SmallFormQuerySpec,
    compile_query,
)
from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard, build_catalog
from meho_backplane.operations.result_query_no_match import (
    MAX_VALUE_CHARS,
    MAX_VALUES_PER_FIELD,
    no_match_hint,
)
from tests._result_store_helpers import MemoryResultStore
from tests.mcp_test_fixtures import required_settings_env  # noqa: F401 — fixture

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000fe")

_ROWS: list[dict[str, Any]] = [
    {"name": "web-01", "power_state": "POWERED_ON", "cpus": 2, "tags": ["a"]},
    {"name": "Web-02", "power_state": "POWERED_OFF", "cpus": 4, "tags": []},
    {"name": "db-01", "power_state": "POWERED_ON", "cpus": 8, "tags": None},
    {"name": "50%_off", "power_state": "SUSPENDED", "cpus": 1, "tags": ["b"]},
]


def _operator() -> Operator:
    return Operator(
        sub="op-a",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _install(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    store = MemoryResultStore()
    store.seed_any_handle(operator_sub="op-a", rows=rows)
    settings = types.SimpleNamespace(
        result_query_max_output_rows=500,
        result_query_max_output_bytes=262144,
        result_query_timeout_seconds=5,
        result_query_response_wire_max_bytes=2097152,
    )
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(rq, "get_settings", lambda: settings)


def _compile(spec: ResultQuerySpec) -> Any:
    return compile_query(spec, build_catalog(_ROWS, guard=AdmissionGuard()), max_limit=500)


# ---------------------------------------------------------------------------
# Contract: ``contains`` only in the small-form spec
# ---------------------------------------------------------------------------


def test_plain_spec_still_rejects_contains() -> None:
    """The REST body and the tool with the switch off keep today's operators."""
    with pytest.raises(ValidationError):
        ResultQuerySpec.model_validate(
            {"filter": [{"field": "name", "op": "contains", "value": "web"}]}
        )


def test_plain_spec_json_schema_has_no_contains() -> None:
    schema = ResultQuerySpec.model_json_schema()
    assert "contains" not in str(schema)


def test_small_form_spec_compiles_contains_as_a_bound_case_free_match() -> None:
    spec = SmallFormQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "contains", "value": "WEB"}]}
    )
    compiled = _compile(spec)
    assert "contains(lower(" in compiled.sql
    assert "WEB" not in compiled.sql
    assert compiled.params == ["WEB"]


def test_small_form_spec_keeps_the_other_rules() -> None:
    """``path`` still cannot be combined with a filter in the small form."""
    with pytest.raises(ValidationError):
        SmallFormQuerySpec.model_validate(
            {"path": "/a", "filter": [{"field": "name", "op": "contains", "value": "x"}]}
        )


def test_contains_on_a_number_column_names_the_field() -> None:
    spec = SmallFormQuerySpec.model_validate(
        {"filter": [{"field": "cpus", "op": "contains", "value": "2"}]}
    )
    with pytest.raises(QueryContractError, match="needs a text column") as caught:
        _compile(spec)
    assert caught.value.loc == "filter[0].field"


@pytest.mark.parametrize("value", [None, "", 5, ["web"]])
def test_contains_needs_a_non_empty_text_value(value: Any) -> None:
    spec = SmallFormQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "contains", "value": value}]}
    )
    with pytest.raises(QueryContractError, match="non-empty text value") as caught:
        _compile(spec)
    assert caught.value.loc == "filter[0].value"


# ---------------------------------------------------------------------------
# Contract: the place of an error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "loc"),
    [
        (
            {
                "filter": [
                    {"field": "name", "op": "=", "value": "x"},
                    {"field": "state", "op": "IS NULL"},
                ]
            },
            "filter[1].field",
        ),
        ({"filter": [{"field": "cpus", "op": "=", "value": "two"}]}, "filter[0].value"),
        ({"filter": [{"field": "name", "op": "IN", "value": []}]}, "filter[0].value"),
        ({"filter": [{"field": "name", "op": "IS NULL", "value": "x"}]}, "filter[0].value"),
        ({"filter": [{"field": "name", "op": "=", "value": ["x"]}]}, "filter[0].value"),
        ({"filter": [{"field": "tags", "op": "=", "value": "a"}]}, "filter[0].field"),
        ({"select": ["name", "state"]}, "select[1]"),
        ({"group_by": ["state"]}, "group_by[0]"),
        ({"group_by": ["name", "name"]}, "group_by[1]"),
        (
            {"aggregate": [{"func": "COUNT"}, {"func": "SUM", "field": "state"}]},
            "aggregate[1].field",
        ),
        ({"aggregate": [{"func": "SUM"}]}, "aggregate[0].field"),
        ({"aggregate": [{"func": "COUNT"}, {"func": "COUNT"}]}, "aggregate[1]"),
        ({"aggregate": [{"func": "COUNT"}], "select": ["name"]}, "select"),
        ({"order_by": [{"field": "state"}]}, "order_by[0].field"),
        ({"group_by": ["name"], "order_by": [{"field": "cpus"}]}, "order_by[0].field"),
    ],
)
def test_contract_error_names_where_the_query_is_wrong(query: dict[str, Any], loc: str) -> None:
    with pytest.raises(QueryContractError) as caught:
        _compile(ResultQuerySpec.model_validate(query))
    assert caught.value.loc == loc


def test_contract_error_text_does_not_change() -> None:
    """The place is kept beside the text, so REST and the switch-off text stay as they were."""
    with pytest.raises(QueryContractError) as caught:
        _compile(ResultQuerySpec(select=["state"]))
    assert str(caught.value).startswith(
        "unknown field 'state': not a column on this result handle."
    )


# ---------------------------------------------------------------------------
# The ``no_match`` block
# ---------------------------------------------------------------------------


def test_no_match_lists_values_per_filter_field_in_stored_order() -> None:
    spec = ResultQuerySpec.model_validate(
        {
            "filter": [
                {"field": "power_state", "op": "=", "value": "poweredOff"},
                {"field": "cpus", "op": ">", "value": 100},
                {"field": "power_state", "op": "!=", "value": "x"},
            ]
        }
    )
    hint = no_match_hint(_ROWS, spec)
    assert hint["values"] == {
        "power_state": ["POWERED_ON", "POWERED_OFF", "SUSPENDED"],
        "cpus": [2, 4, 8, 1],
    }
    assert hint["note"].startswith("No row matched the filter.")
    assert "Only the first" not in hint["note"]


def test_no_match_skips_null_object_and_list_values() -> None:
    rows: list[dict[str, Any]] = [{"v": None}, {"v": {"a": 1}}, {"v": [1]}, {}, {"v": "x"}]
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "v", "op": "=", "value": "y"}]})
    assert no_match_hint(rows, spec)["values"] == {"v": ["x"]}


def test_no_match_keeps_true_and_one_apart() -> None:
    rows: list[dict[str, Any]] = [{"v": True}, {"v": 1}, {"v": True}, {"v": 1.0}]
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "v", "op": "=", "value": 2}]})
    assert no_match_hint(rows, spec)["values"] == {"v": [True, 1, 1.0]}


def test_no_match_with_no_rows_gives_empty_lists() -> None:
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "v", "op": "IS NULL"}]})
    assert no_match_hint([], spec)["values"] == {"v": []}


def test_no_match_shows_a_few_values_and_says_when_there_are_more() -> None:
    rows = [{"name": f"vm-{i}"} for i in range(MAX_VALUES_PER_FIELD + 5)]
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "name", "op": "=", "value": "x"}]})
    hint = no_match_hint(rows, spec)
    assert hint["values"]["name"] == [f"vm-{i}" for i in range(MAX_VALUES_PER_FIELD)]
    assert f"Only the first {MAX_VALUES_PER_FIELD} values are shown for: name." in hint["note"]


def test_no_match_with_exactly_the_limit_does_not_say_more() -> None:
    rows = [{"name": f"vm-{i}"} for i in range(MAX_VALUES_PER_FIELD)]
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "name", "op": "=", "value": "x"}]})
    assert "Only the first" not in no_match_hint(rows, spec)["note"]


def test_no_match_cuts_long_texts() -> None:
    long_text = "x" * (MAX_VALUE_CHARS + 30)
    spec = ResultQuerySpec.model_validate({"filter": [{"field": "v", "op": "=", "value": "y"}]})
    hint = no_match_hint([{"v": long_text}, {"v": "short"}], spec)
    assert hint["values"]["v"] == ["x" * MAX_VALUE_CHARS + "…", "short"]
    assert "are cut" in hint["note"]


# ---------------------------------------------------------------------------
# Core: ``no_match`` only with the small form, ``contains`` end to end
# ---------------------------------------------------------------------------


async def test_core_adds_no_match_only_for_the_small_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _ROWS)
    spec = ResultQuerySpec.model_validate(
        {"filter": [{"field": "power_state", "op": "=", "value": "poweredOff"}]}
    )
    plain = await rq.run_result_query(_operator(), uuid.uuid4(), spec)
    assert plain["rows"] == []
    assert "no_match" not in plain

    small = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert small["rows"] == []
    assert small["no_match"]["values"] == {
        "power_state": ["POWERED_ON", "POWERED_OFF", "SUSPENDED"]
    }


async def test_core_gives_no_no_match_when_a_row_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ROWS)
    spec = ResultQuerySpec.model_validate(
        {"filter": [{"field": "power_state", "op": "=", "value": "POWERED_OFF"}]}
    )
    result = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert [row["name"] for row in result["rows"]] == ["Web-02"]
    assert "no_match" not in result


async def test_core_gives_no_no_match_without_a_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [])
    result = await rq.run_result_query(
        _operator(), uuid.uuid4(), ResultQuerySpec(), small_form=True
    )
    assert result["rows"] == []
    assert "no_match" not in result


@pytest.mark.parametrize(("value", "has_no_match"), [("poweredOff", True), ("POWERED_ON", False)])
async def test_core_count_without_group_by_still_finds_no_match(
    monkeypatch: pytest.MonkeyPatch, value: str, has_no_match: bool
) -> None:
    """``COUNT(*)`` answers one row even when nothing matched."""
    _install(monkeypatch, _ROWS)
    spec = ResultQuerySpec.model_validate(
        {
            "filter": [{"field": "power_state", "op": "=", "value": value}],
            "aggregate": [{"func": "COUNT"}],
        }
    )
    result = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert len(result["rows"]) == 1
    assert ("no_match" in result) is has_no_match


async def test_core_grouped_query_with_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ROWS)
    spec = ResultQuerySpec.model_validate(
        {
            "filter": [{"field": "cpus", "op": ">", "value": 100}],
            "group_by": ["power_state"],
            "aggregate": [{"func": "COUNT"}],
        }
    )
    result = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert result["rows"] == []
    assert result["no_match"]["values"] == {"cpus": [2, 4, 8, 1]}


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("WEB", ["web-01", "Web-02"]),
        ("01", ["web-01", "db-01"]),
        ("%", ["50%_off"]),
        ("_", ["50%_off"]),
    ],
)
async def test_core_contains_ignores_case_and_has_no_wildcards(
    monkeypatch: pytest.MonkeyPatch, value: str, names: list[str]
) -> None:
    _install(monkeypatch, _ROWS)
    spec = SmallFormQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "contains", "value": value}]}
    )
    result = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert [row["name"] for row in result["rows"]] == names
    assert "no_match" not in result


async def test_core_contains_with_no_match_lists_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ROWS)
    spec = SmallFormQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "contains", "value": "mail"}]}
    )
    result = await rq.run_result_query(_operator(), uuid.uuid4(), spec, small_form=True)
    assert result["rows"] == []
    assert result["no_match"]["values"] == {"name": ["web-01", "Web-02", "db-01", "50%_off"]}
