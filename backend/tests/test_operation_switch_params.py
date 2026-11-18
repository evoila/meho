# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A parameter that waits behind a switch (``x-meho-switch``).

These tests check:

* with the switch off (the default), the parameter is left out of
  ``search_operations`` (ranked and exact-op), ``preview_operation`` and
  the operation read, also when it sits in a nested object;
* a call that gives the parameter is refused like any unknown parameter,
  before it can wait for approval, and no approval request is stored;
* with the switch on, the parameter shows and a call that gives it waits
  for approval as usual;
* a keyword that names an unknown switch stops the operation from
  registering, for a typed and for an ingested operation;
* the stored schema is never changed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.meta_tools import (
    describe_descriptor,
    preview_operation,
    search_operations,
)
from meho_backplane.operations.switch_params import (
    FILE_REF_PARAMS_SWITCH,
    SWITCH_MARKER,
    checked_parameter_schema,
    switch_names,
    visible_parameter_schema,
)
from meho_backplane.operations.switch_registration import check_switch_names
from meho_backplane.settings import get_settings

_SWITCH = "FILE_REF_PARAMS"
_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_CONNECTOR = "widget-1.x"
_OP = "widget.upload"
_HIDDEN = "expected_checksum"
_NESTED_HIDDEN = "file_record"


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            _HIDDEN: {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH},
            "options": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    _NESTED_HIDDEN: {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH},
                },
                "required": ["mode", _NESTED_HIDDEN],
            },
        },
        "required": ["name", _HIDDEN],
        "additionalProperties": False,
    }


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SEARCH_HIT_CALL_HINTS", "true")
    monkeypatch.setenv("PREVIEW_TYPED_WRITES", "true")
    monkeypatch.delenv(_SWITCH, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SWITCH, "true")
    get_settings.cache_clear()


class _WidgetConnector(Connector):
    product = "widget"
    version = "1.x"
    impl_id = "widget"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


async def _register(
    embedding: AsyncMock,
    schema: dict[str, Any] | None = None,
    llm_instructions: dict[str, Any] | None = None,
) -> None:
    register_connector_v2(product="widget", version="1.x", impl_id="widget", cls=_WidgetConnector)
    await register_typed_operation(
        product="widget",
        version="1.x",
        impl_id="widget",
        op_id=_OP,
        handler=_handler,
        summary="Upload a widget file.",
        description="Upload a widget file.",
        parameter_schema=schema if schema is not None else _schema(),
        safety_level="caution",
        when_to_use=None,
        embedding_service=embedding,
        llm_instructions=llm_instructions,
    )


def _operator(kind: PrincipalKind = PrincipalKind.AGENT) -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _no_trace(value: Any, *names: str) -> None:
    text = json.dumps(value, default=str)
    for name in names:
        assert name not in text, name


# ---------------------------------------------------------------------------
# The schema helpers
# ---------------------------------------------------------------------------


def test_switch_names_counts_any_depth() -> None:
    assert switch_names(_schema()) == {FILE_REF_PARAMS_SWITCH}
    assert switch_names({"type": "object"}) == set()
    assert switch_names([{"a": {SWITCH_MARKER: "other"}}]) == {"other"}


def test_visible_schema_off_drops_the_parameter_at_every_depth() -> None:
    stored = _schema()
    snapshot = copy.deepcopy(stored)
    shown = visible_parameter_schema(stored)
    _no_trace(shown, _HIDDEN, _NESTED_HIDDEN)
    assert set(shown["properties"]) == {"name", "options"}
    assert shown["required"] == ["name"]
    assert shown["properties"]["options"]["required"] == ["mode"]
    assert stored == snapshot, "the stored schema is never changed"


def test_visible_schema_off_keeps_the_other_rules() -> None:
    stored = _schema()
    del stored["additionalProperties"]
    shown = visible_parameter_schema(stored)
    assert "additionalProperties" not in shown
    checked = checked_parameter_schema(stored)
    assert "additionalProperties" not in checked
    assert checked["properties"][_HIDDEN] is False
    assert checked["properties"]["options"]["properties"][_NESTED_HIDDEN] is False
    assert checked["required"] == ["name"]
    assert checked_parameter_schema(stored) == checked, "the stored schema is never changed"


def test_visible_schema_off_strips_examples_and_defaults_at_any_depth() -> None:
    stored = _schema()
    sample = {"name": "a", _HIDDEN: "x", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}
    stored["examples"] = [sample, "not an object"]
    stored["example"] = sample
    stored["default"] = sample
    stored["properties"]["options"]["examples"] = [{"mode": "m", _NESTED_HIDDEN: "r"}]
    stored["properties"]["options"]["default"] = {"mode": "m", _NESTED_HIDDEN: "r"}
    stored["dependentRequired"] = {_HIDDEN: ["name"], "name": [_HIDDEN]}
    shown = visible_parameter_schema(stored)
    _no_trace(shown, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)
    assert shown["examples"] == [{"name": "a", "options": {"mode": "m"}}, "not an object"]
    assert shown["example"] == shown["default"] == {"name": "a", "options": {"mode": "m"}}
    assert shown["properties"]["options"]["examples"] == [{"mode": "m"}]
    assert shown["dependentRequired"] == {"name": []}


def test_visible_schema_off_strips_examples_inside_maps_and_lists() -> None:
    marked = _schema()["properties"][_HIDDEN]
    entry = {"type": "object", "properties": {"mode": {"type": "string"}, _HIDDEN: marked}}
    stored = {
        "type": "object",
        "properties": {
            "entries": {"type": "object", "additionalProperties": entry},
            "named": {"type": "object", "patternProperties": {"^n_": entry}},
            "pair": {"type": "array", "prefixItems": [entry], "items": {"type": "string"}},
            "both": {
                "type": "object",
                "properties": {"first": {"type": "object"}},
                "patternProperties": {"^f": entry},
            },
        },
        "example": {
            "entries": {"first": {"mode": "m", _HIDDEN: "x"}},
            "named": {"n_one": {"mode": "m", _HIDDEN: "x"}},
            "pair": [{"mode": "m", _HIDDEN: "x"}, "tail"],
            "both": {"first": {"mode": "m", _HIDDEN: "x"}},
        },
    }
    shown = visible_parameter_schema(stored)
    _no_trace(shown, _HIDDEN, SWITCH_MARKER)
    assert shown["example"] == {
        "entries": {"first": {"mode": "m"}},
        "named": {"n_one": {"mode": "m"}},
        "pair": [{"mode": "m"}, "tail"],
        "both": {"first": {"mode": "m"}},
    }
    checked = Draft202012Validator(checked_parameter_schema(stored))
    assert not checked.is_valid({"both": {"first": {"mode": "m", _HIDDEN: "x"}}})
    assert checked.is_valid(shown["example"])


def test_checked_schema_off_refuses_the_parameter_in_every_branch() -> None:
    marked = _schema()["properties"][_HIDDEN]
    stored = {
        "type": "object",
        "anyOf": [{"properties": {_HIDDEN: marked}}, {"required": ["name"]}],
    }
    checked = checked_parameter_schema(stored)
    assert checked["properties"][_HIDDEN] is False
    assert checked["anyOf"][1]["required"] == ["name"]


def _ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            _HIDDEN: {"$ref": "#/$defs/Checksum"},
            "options": {"$ref": "#/components/schemas/Options"},
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": "a", _HIDDEN: "x", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}],
        "$defs": {"Checksum": {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH}},
        "components": {
            "schemas": {
                "Options": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string"},
                        _NESTED_HIDDEN: {"$ref": "#/components/schemas/FileRecord"},
                    },
                },
                "FileRecord": {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH},
            }
        },
    }


def test_visible_schema_follows_local_refs() -> None:
    shown = visible_parameter_schema(_ref_schema())
    _no_trace(shown, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER, "Checksum", "FileRecord")
    assert shown["examples"] == [{"name": "a", "options": {"mode": "m"}}]
    assert set(shown["components"]["schemas"]) == {"Options"}
    checked = checked_parameter_schema(_ref_schema())
    # The closed root leaves the name out, so it is refused as undeclared.
    assert _HIDDEN not in checked["properties"]
    assert not Draft202012Validator(checked).is_valid({"name": "a", _HIDDEN: "x"})
    options = checked["components"]["schemas"]["Options"]
    assert options["properties"][_NESTED_HIDDEN] is False


def test_visible_schema_on_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    stored = _schema()
    assert visible_parameter_schema(stored) is stored


@pytest.mark.parametrize("schema", [{}, {"type": "object"}, None, [], "text", {"properties": 3}])
def test_visible_schema_without_the_keyword_is_returned_as_it_is(schema: Any) -> None:
    assert visible_parameter_schema(schema) is schema


def test_a_keyword_that_is_not_a_name_hides_nothing_and_does_not_register() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string", SWITCH_MARKER: True}}}
    assert visible_parameter_schema(schema) == schema
    with pytest.raises(ValueError, match="unknown switch"):
        check_switch_names(schema, op_id="widget.x")


# ---------------------------------------------------------------------------
# Switch off: no trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_shows_no_trace(stub_embedding_service: AsyncMock) -> None:
    schema = _schema()
    schema["examples"] = [
        {"name": "a", _HIDDEN: "x", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}
    ]
    schema["properties"]["options"]["example"] = {"mode": "m", _NESTED_HIDDEN: "r"}
    await _register(stub_embedding_service, schema)
    operator = _operator()

    ranked = await search_operations(
        operator, {"connector_id": _CONNECTOR, "query": "upload widget", "limit": 10}
    )
    assert [hit["op_id"] for hit in ranked["hits"]] == [_OP]
    assert ranked["hits"][0]["required_params"] == ["name"]
    _no_trace(ranked, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert exact["operation"]["parameter_schema"]["required"] == ["name"]
    _no_trace(exact, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    preview = await preview_operation(
        operator, {"connector_id": _CONNECTOR, "op_id": _OP, "params": {"name": "a"}}
    )
    _no_trace(preview, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    async with get_sessionmaker()() as session:
        row_id = (
            await session.execute(
                select(EndpointDescriptor.id).where(EndpointDescriptor.op_id == _OP)
            )
        ).scalar_one()
    described = await describe_descriptor(operator, row_id)
    assert described is not None
    _no_trace(described.parameter_schema, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)


@pytest.mark.asyncio
async def test_switch_off_refuses_the_parameter_before_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    for params in (
        {"name": "a", _HIDDEN: "abc"},
        {"name": "a", "options": {"mode": "m", _NESTED_HIDDEN: "r"}},
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR,
            op_id=_OP,
            target=None,
            params=params,
        )
        assert result.status == "error"
        assert result.error is not None
        assert result.error.startswith("invalid_params")
    assert await _approval_count() == 0

    preview = await preview_operation(
        _operator(), {"connector_id": _CONNECTOR, "op_id": _OP, "params": {_HIDDEN: "x"}}
    )
    assert preview["status"] == "error"
    assert "invalid_params" in json.dumps(preview)

    # The same call without the parameter still waits for approval.
    ok = await dispatch(
        operator=_operator(), connector_id=_CONNECTOR, op_id=_OP, target=None, params={"name": "a"}
    )
    assert ok.status == "awaiting_approval"
    assert await _approval_count() == 1


def _linked_required_schemas() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Schemas whose linked schema requires the hidden property declared beside the link."""
    marked = _schema()["properties"][_HIDDEN]
    needs = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name", _HIDDEN],
    }
    beside_ref = {
        "$ref": "#/$defs/Needs",
        "properties": {_HIDDEN: marked},
        "$defs": {"Needs": needs},
    }
    ref_in_all_of = {
        "type": "object",
        "properties": {_HIDDEN: marked},
        "allOf": [{"$ref": "#/$defs/Needs"}],
        "$defs": {"Needs": needs},
    }
    two_levels = {
        "$ref": "#/$defs/Outer",
        "properties": {_HIDDEN: marked},
        "$defs": {"Outer": {"$ref": "#/$defs/Needs"}, "Needs": needs},
    }
    return [
        ("required-beside-ref", beside_ref, {"name": "a"}),
        ("required-ref-in-all-of", ref_in_all_of, {"name": "a"}),
        ("required-two-levels", two_levels, {"name": "a"}),
    ]


def _open_schemas() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Schemas that accept more than their properties, each with one other valid input."""
    no_rule = _schema()
    del no_rule["additionalProperties"]
    open_true = _schema()
    open_true["additionalProperties"] = True
    open_typed = _schema()
    open_typed["additionalProperties"] = {"type": "string"}
    pattern = _schema()
    pattern["patternProperties"] = {"^exp": {"type": "string"}, "^x_": {"type": "string"}}
    branches = {
        "type": "object",
        "allOf": [
            {"properties": {"name": {"type": "string"}, _HIDDEN: _schema()["properties"][_HIDDEN]}},
            {"properties": {"label": {"type": "string"}}},
        ],
        "required": ["name", _HIDDEN],
    }
    marked = _schema()["properties"][_HIDDEN]
    any_of = {
        "type": "object",
        "anyOf": [
            {"properties": {_HIDDEN: marked}, "required": [_HIDDEN]},
            {"required": ["name"]},
        ],
    }
    dependent = {
        "type": "object",
        "properties": {"name": {"type": "string"}, _HIDDEN: marked},
        "dependentRequired": {"name": [_HIDDEN]},
    }
    sibling_required = {
        "type": "object",
        "allOf": [
            {"properties": {"name": {"type": "string"}, _HIDDEN: marked}},
            {"required": ["name", _HIDDEN]},
        ],
    }
    linked_required = {
        "type": "object",
        "allOf": [
            {"properties": {"name": {"type": "string"}, _HIDDEN: marked}},
            {"$ref": "#/$defs/Needs"},
        ],
        "$defs": {"Needs": {"required": ["name", _HIDDEN]}},
    }
    base = {"Base": {"type": "object", "properties": {"name": {"type": "string"}}}}
    ref_sibling = {"$ref": "#/$defs/Base", "properties": {_HIDDEN: marked}, "$defs": base}
    ref_closed = {
        "type": "object",
        "allOf": [
            {
                "$ref": "#/$defs/Base",
                "properties": {_HIDDEN: marked},
                "unevaluatedProperties": False,
            }
        ],
        "$defs": base,
    }
    return [
        *_linked_required_schemas(),
        ("no-rule", no_rule, {"name": "a", "other": 1}),
        ("open", open_true, {"name": "a", "other": 1}),
        ("typed", open_typed, {"name": "a", "other": "x"}),
        ("pattern", pattern, {"name": "a", "x_other": "x"}),
        ("all-of", branches, {"name": "a", "label": "x"}),
        ("any-of", any_of, {"name": "a"}),
        ("dependent-required", dependent, {"name": "a"}),
        ("all-of-required", sibling_required, {"name": "a"}),
        ("linked-required", linked_required, {"name": "a"}),
        ("ref-sibling", ref_sibling, {"name": "a"}),
        ("ref-unevaluated", ref_closed, {"name": "a"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "other"),
    [(schema, other) for _, schema, other in _open_schemas()],
    ids=[name for name, _, _ in _open_schemas()],
)
async def test_switch_off_refuses_only_the_parameter_of_an_open_schema(
    stub_embedding_service: AsyncMock, schema: dict[str, Any], other: dict[str, Any]
) -> None:
    await _register(stub_embedding_service, schema)
    exact = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": _OP})
    _no_trace(exact, _HIDDEN, SWITCH_MARKER)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR,
        op_id=_OP,
        target=None,
        params={"name": "a", _HIDDEN: "abc"},
    )
    assert result.error is not None
    assert result.error.startswith("invalid_params")
    assert await _approval_count() == 0
    preview = await preview_operation(
        _operator(),
        {"connector_id": _CONNECTOR, "op_id": _OP, "params": {"name": "a", _HIDDEN: "abc"}},
    )
    assert preview["status"] == "error"
    assert "invalid_params" in json.dumps(preview)

    # Every other input is accepted as before: the call waits for approval.
    ok = await dispatch(
        operator=_operator(), connector_id=_CONNECTOR, op_id=_OP, target=None, params=other
    )
    assert ok.status == "awaiting_approval", ok.error
    assert await _approval_count() == 1


@pytest.mark.parametrize(
    ("schema", "other"),
    [(schema, other) for _, schema, other in _linked_required_schemas()],
    ids=[name for name, _, _ in _linked_required_schemas()],
)
def test_switch_off_a_linked_schema_does_not_require_the_hidden_property(
    schema: dict[str, Any], other: dict[str, Any]
) -> None:
    stored = copy.deepcopy(schema)
    shown = visible_parameter_schema(schema)
    _no_trace(shown, _HIDDEN, SWITCH_MARKER)
    checked = checked_parameter_schema(schema)
    assert Draft202012Validator(checked).is_valid(other)
    assert not Draft202012Validator(checked).is_valid({**other, _HIDDEN: "abc"})
    assert schema == stored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "other"),
    [(schema, other) for _, schema, other in _linked_required_schemas()],
    ids=[name for name, _, _ in _linked_required_schemas()],
)
async def test_switch_off_preview_accepts_a_call_without_the_linked_required_property(
    stub_embedding_service: AsyncMock, schema: dict[str, Any], other: dict[str, Any]
) -> None:
    await _register(stub_embedding_service, schema)
    preview = await preview_operation(
        _operator(), {"connector_id": _CONNECTOR, "op_id": _OP, "params": other}
    )
    assert preview["status"] != "error", preview
    _no_trace(preview, _HIDDEN, SWITCH_MARKER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [schema for _, schema, _ in _linked_required_schemas()],
    ids=[name for name, _, _ in _linked_required_schemas()],
)
async def test_switch_on_a_linked_schema_requires_the_property(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    _switch_on(monkeypatch)
    await _register(stub_embedding_service, schema)
    operator = _operator()
    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert exact["operation"]["parameter_schema"] == schema
    missing = await dispatch(
        operator=operator, connector_id=_CONNECTOR, op_id=_OP, target=None, params={"name": "a"}
    )
    assert missing.error is not None
    assert missing.error.startswith("invalid_params")
    given = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=_OP,
        target=None,
        params={"name": "a", _HIDDEN: "abc"},
    )
    assert given.status == "awaiting_approval", given.error


@pytest.mark.asyncio
async def test_switch_off_hides_and_refuses_a_parameter_behind_a_ref(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service, _ref_schema())
    operator = _operator()
    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    _no_trace(exact, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)
    for params in (
        {"name": "a", _HIDDEN: "abc"},
        {"name": "a", "options": {"mode": "m", _NESTED_HIDDEN: "r"}},
    ):
        result = await dispatch(
            operator=operator, connector_id=_CONNECTOR, op_id=_OP, target=None, params=params
        )
        assert result.error is not None
        assert result.error.startswith("invalid_params")
    assert await _approval_count() == 0


@pytest.mark.asyncio
async def test_switch_on_shows_and_accepts_a_parameter_behind_a_ref(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_on(monkeypatch)
    await _register(stub_embedding_service, _ref_schema())
    operator = _operator()
    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert _HIDDEN in exact["operation"]["parameter_schema"]["properties"]
    params = {"name": "a", _HIDDEN: "abc", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR, op_id=_OP, target=None, params=params
    )
    assert result.status == "awaiting_approval", result.error


# ---------------------------------------------------------------------------
# Switch on: shown and accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_on_shows_and_accepts_the_parameter(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_on(monkeypatch)
    await _register(stub_embedding_service)
    operator = _operator()

    ranked = await search_operations(
        operator, {"connector_id": _CONNECTOR, "query": "upload widget", "limit": 10}
    )
    assert ranked["hits"][0]["required_params"] == ["name", _HIDDEN]
    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert _HIDDEN in exact["operation"]["parameter_schema"]["properties"]

    params = {"name": "a", _HIDDEN: "abc", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR, op_id=_OP, target=None, params=params
    )
    assert result.status == "awaiting_approval"
    assert await _approval_count() == 1


# ---------------------------------------------------------------------------
# Switch off: the answer of an unknown parameter
# ---------------------------------------------------------------------------


async def _errors(params: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Return the validation errors of a call and of its preview."""
    result = await dispatch(
        operator=_operator(), connector_id=_CONNECTOR, op_id=_OP, target=None, params=params
    )
    assert result.extras is not None
    preview = await preview_operation(
        _operator(), {"connector_id": _CONNECTOR, "op_id": _OP, "params": params}
    )
    return result.extras["validation_errors"], preview["extras"]["validation_errors"]


@pytest.mark.asyncio
async def test_switch_off_a_hidden_parameter_gets_the_answer_of_an_unknown_one(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)
    hidden = await _errors({"name": "a", _HIDDEN: "x"})
    unknown = await _errors({"name": "a", "unknown_x": "x"})

    # The answers are the same, but for the name the caller sent.
    renamed = json.loads(json.dumps(hidden).replace(_HIDDEN, "unknown_x"))
    assert renamed == json.loads(json.dumps(unknown))
    assert unknown[0][0]["validator"] == "additionalProperties"
    assert 'declared params are: "name", "options"' in unknown[0][0]["remediation"]
    # No answer names a hidden parameter the caller did not send.
    _no_trace(unknown, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)
    _no_trace(hidden, _NESTED_HIDDEN, SWITCH_MARKER)
    assert await _approval_count() == 0


@pytest.mark.asyncio
async def test_switch_off_a_hidden_parameter_of_an_open_object_names_nothing(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)
    for errors in await _errors({"name": "a", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}):
        assert errors == [
            {
                "path": "$.options",
                "validator": "additionalProperties",
                "expected": "undeclared property (not in the op's declared params)",
                "remediation": "remove the undeclared property",
            }
        ]
    assert await _approval_count() == 0


# ---------------------------------------------------------------------------
# Switch off: the same answers as without the keyword
# ---------------------------------------------------------------------------


def _alternative_schemas() -> list[tuple[str, dict[str, Any]]]:
    marked = _schema()["properties"][_HIDDEN]
    found = []
    for keyword in ("oneOf", "anyOf"):
        found.append(
            (
                keyword,
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, _HIDDEN: marked},
                    keyword: [
                        {"required": [_HIDDEN]},
                        {"allOf": [{"$ref": "#/$defs/NeedsHidden"}]},
                        {"required": ["name"]},
                    ],
                    "$defs": {"NeedsHidden": {"required": [_HIDDEN, "name"]}},
                },
            )
        )
    return found


@pytest.mark.parametrize(
    "schema",
    [schema for _, schema in _alternative_schemas()],
    ids=[name for name, _ in _alternative_schemas()],
)
def test_switch_off_a_branch_that_requires_the_parameter_gives_the_same_answers(
    schema: dict[str, Any],
) -> None:
    stored = Draft202012Validator(schema)
    checked = Draft202012Validator(checked_parameter_schema(schema))
    calls: list[dict[str, Any]] = [{}, {"name": "a"}, {"name": 1}, {"other": 1}]
    for call in calls:
        assert checked.is_valid(call) == stored.is_valid(call), call
    assert not checked.is_valid({"name": "a", _HIDDEN: "x"})
    shown = visible_parameter_schema(schema)
    _no_trace(shown, _HIDDEN, "NeedsHidden")


@pytest.mark.asyncio
async def test_switch_off_a_one_of_call_that_passes_without_the_keyword_waits_for_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service, _alternative_schemas()[0][1])
    result = await dispatch(
        operator=_operator(), connector_id=_CONNECTOR, op_id=_OP, target=None, params={"name": "a"}
    )
    assert result.status == "awaiting_approval", result.error


@pytest.mark.parametrize(
    "schema",
    [schema for _, schema in _alternative_schemas()],
    ids=[name for name, _ in _alternative_schemas()],
)
def test_switch_on_a_branch_that_requires_the_parameter_stays(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    _switch_on(monkeypatch)
    assert checked_parameter_schema(schema) is schema
    assert visible_parameter_schema(schema) is schema


# ---------------------------------------------------------------------------
# llm_instructions
# ---------------------------------------------------------------------------


def _instructions() -> dict[str, Any]:
    return {
        "examples": [{"name": "a"}, {"name": "a", _HIDDEN: "x"}, f"pass {_HIDDEN} too"],
        "example_call": {"params": {"name": "a", "options": {_NESTED_HIDDEN: "r"}}},
        "parameter_hints": {"name": "The widget name.", _HIDDEN: "The checksum."},
        "when_to_call": "To upload a widget.",
    }


@pytest.mark.asyncio
async def test_switch_off_hides_instruction_examples_that_name_the_parameter(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATION_CONTRACT_GUIDANCE", "true")
    get_settings.cache_clear()
    await _register(stub_embedding_service, llm_instructions=_instructions())

    exact = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": _OP})
    contract = exact["operation"]
    _no_trace(contract, _HIDDEN, _NESTED_HIDDEN)
    assert {"source": "llm_instructions", "param": None, "value": {"name": "a"}} in contract[
        "examples"
    ]
    assert contract["guidance"]["parameter_hints"] == {"name": "The widget name."}

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.llm_instructions == _instructions()
    described = await describe_descriptor(_operator(), row.id)
    assert described is not None
    _no_trace(described.llm_instructions, _HIDDEN, _NESTED_HIDDEN)
    assert described.llm_instructions == {
        "examples": [{"name": "a"}],
        "parameter_hints": {"name": "The widget name."},
        "when_to_call": "To upload a widget.",
    }


@pytest.mark.asyncio
async def test_switch_on_shows_instruction_examples_that_name_the_parameter(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATION_CONTRACT_GUIDANCE", "true")
    _switch_on(monkeypatch)
    await _register(stub_embedding_service, llm_instructions=_instructions())
    exact = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": _OP})
    assert _HIDDEN in json.dumps(exact["operation"]["examples"])
    assert exact["operation"]["guidance"]["parameter_hints"][_HIDDEN] == "The checksum."


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _misplaced_schemas() -> list[tuple[str, dict[str, Any]]]:
    marker = {SWITCH_MARKER: FILE_REF_PARAMS_SWITCH}
    shared = {"$defs": {"Marked": {"type": "string", **marker}}}
    return [
        ("root", {"type": "object", **marker}),
        ("items", {"properties": {"files": {"type": "array", "items": marker}}}),
        ("additional", {"properties": {"map": {"additionalProperties": marker}}}),
        ("pattern", {"properties": {"map": {"patternProperties": {"^f": marker}}}}),
        ("branch-of-property", {"properties": {"x": {"anyOf": [marker, {"type": "null"}]}}}),
        ("link-from-items", {"properties": {"x": {"items": {"$ref": "#/$defs/Marked"}}}, **shared}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [schema for _, schema in _misplaced_schemas()],
    ids=[name for name, _ in _misplaced_schemas()],
)
async def test_a_keyword_not_on_a_property_stops_a_typed_operation_from_registering(
    stub_embedding_service: AsyncMock, schema: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="belongs on a property"):
        await _register(stub_embedding_service, schema)
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).all()
    assert rows == []


@pytest.mark.parametrize(
    "schema",
    [schema for _, schema in _misplaced_schemas()],
    ids=[name for name, _ in _misplaced_schemas()],
)
def test_a_keyword_not_on_a_property_stops_an_ingested_operation_from_registering(
    schema: dict[str, Any],
) -> None:
    proto = EndpointDescriptorProto(
        op_id="GET:/v1/widget",
        method="GET",
        path="/v1/widget",
        summary="Read a widget.",
        description="Read a widget.",
        parameter_schema=schema,
        safety_level="safe",
        requires_approval=False,
    )
    with pytest.raises(ValueError, match="belongs on a property"):
        apply_safety_floor(product="widget", version="1.x", impl_id="widget", proto=proto)


@pytest.mark.parametrize(
    "schema",
    [_schema(), _ref_schema(), *(schema for _, schema, _ in _linked_required_schemas())],
)
def test_a_keyword_on_a_property_or_a_linked_shared_schema_registers(
    schema: dict[str, Any],
) -> None:
    check_switch_names(schema, op_id="widget.x")


@pytest.mark.asyncio
async def test_unknown_switch_stops_a_typed_operation_from_registering(
    stub_embedding_service: AsyncMock,
) -> None:
    schema = _schema()
    schema["properties"][_HIDDEN][SWITCH_MARKER] = "file_ref_param"
    with pytest.raises(ValueError, match="file_ref_param"):
        await _register(stub_embedding_service, schema)
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).all()
    assert rows == []


def test_unknown_switch_stops_an_ingested_operation_from_registering() -> None:
    schema = _schema()
    schema["properties"]["options"]["properties"][_NESTED_HIDDEN][SWITCH_MARKER] = "typo"

    def proto(parameter_schema: dict[str, Any]) -> EndpointDescriptorProto:
        return EndpointDescriptorProto(
            op_id="GET:/v1/widget",
            method="GET",
            path="/v1/widget",
            summary="Read a widget.",
            description="Read a widget.",
            parameter_schema=parameter_schema,
            safety_level="safe",
            requires_approval=False,
        )

    with pytest.raises(ValueError, match="typo"):
        apply_safety_floor(product="widget", version="1.x", impl_id="widget", proto=proto(schema))
    apply_safety_floor(product="widget", version="1.x", impl_id="widget", proto=proto(_schema()))
