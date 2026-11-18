# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_HIT_CALL_HINTS`` switch of ``search_operations`` (#4331).

These tests check:

* switch off (the default): a ranked hit, the MCP output schema and the
  REST answer are the same as before, and no handler is imported;
* switch on: each hit lists the top-level required parameter names in
  order, and says whether a call needs a target;
* ``target_required`` follows the dispatcher's own rule for every kind of
  operation (typed, multi-step, generic), and agrees with the call
  contract of exact-op mode;
* the MCP wire answer and the REST ``/search`` answer carry the fields.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.mcp.registry import get_tool, tool_for_settings
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._search_call_hints import (
    CALL_HINT_FIELDS,
    required_param_names,
    search_hit_call_hints,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_SWITCH = "SEARCH_HIT_CALL_HINTS"
_CONNECTOR = "demo-1.x"
_MODULE = "tests.test_search_hit_call_hints"

#: The keys of a slim ranked hit before this change (#3668).
_OLD_HIT_KEYS = {
    "op_id",
    "summary",
    "description",
    "group_key",
    "safety_level",
    "requires_approval",
}


async def _plain_handler(operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A module-level handler: it needs no target."""
    return {}


async def _bound_handler(
    operator: Any, connector: Any, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """A handler that reaches its connector through the target."""
    return {}


#: op_id -> (source_kind, handler_ref, parameter_schema, target needed).
_OPS: dict[str, tuple[str, str | None, dict[str, Any], bool]] = {
    "demo.secret.read": (
        "typed",
        f"{_MODULE}._plain_handler",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "mount": {"type": "string"}},
            "required": ["path", "mount"],
        },
        False,
    ),
    "demo.secret.bound": (
        "typed",
        f"{_MODULE}._bound_handler",
        {"type": "object", "required": ["path"]},
        True,
    ),
    "demo.secret.steps": (
        "composite",
        f"{_MODULE}._plain_handler",
        {"type": "object"},
        False,
    ),
    "demo.secret.bound_steps": (
        "composite",
        f"{_MODULE}._bound_handler",
        {"type": "object", "required": []},
        True,
    ),
    "GET:/v1/secret/{path}": (
        "ingested",
        None,
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "body": {"type": "object"}},
            "required": ["path", "body"],
        },
        True,
    ),
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_SWITCH, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
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


async def _seed_ops() -> None:
    """Seed one enabled row for each entry of :data:`_OPS`."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as s, s.begin():
        for op_id, (kind, handler_ref, schema, _needs_target) in _OPS.items():
            s.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product="demo",
                    version="1.x",
                    impl_id="demo",
                    op_id=op_id,
                    source_kind=kind,
                    method="GET" if kind == "ingested" else None,
                    path="/v1/secret/{path}" if kind == "ingested" else None,
                    handler_ref=handler_ref,
                    summary=f"Secret operation {op_id}.",
                    description="Works on a secret.",
                    group_id=None,
                    tags=[],
                    parameter_schema=schema,
                    response_schema=None,
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                    embedding=None,
                    custom_description=None,
                    custom_notes=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _operator() -> Operator:
    return build_operator(TenantRole.OPERATOR)


async def _hits() -> dict[str, dict[str, Any]]:
    result = await search_operations(
        _operator(), {"connector_id": _CONNECTOR, "query": "secret", "limit": 50}
    )
    hits = {hit["op_id"]: hit for hit in result["hits"]}
    assert set(hits) == set(_OPS)
    return hits


# ---------------------------------------------------------------------------
# The names in the required list
# ---------------------------------------------------------------------------


def test_switch_is_off_by_default() -> None:
    assert search_hit_call_hints() is False


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "object", "required": ["b", "a", "c"]}, ["b", "a", "c"]),
        ({"type": "object"}, []),
        ({"type": "object", "required": []}, []),
        ({"required": "path"}, []),
        ({"required": None}, []),
        ({"required": ["path", 7, None, "mount"]}, ["path", "mount"]),
        ({"required": ["path", "path", "Path"]}, ["path", "Path"]),
        ({"properties": {"x": {"type": "object", "required": ["inner"]}}}, []),
        (None, []),
        ([], []),
        ("not a schema", []),
    ],
)
def test_required_param_names(schema: Any, expected: list[str]) -> None:
    assert required_param_names(schema) == expected


# ---------------------------------------------------------------------------
# Switch off: nothing changes
# ---------------------------------------------------------------------------


async def test_switch_off_hit_is_the_same_as_before(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    def _must_not_run(descriptor: Any) -> bool:
        raise AssertionError("the target rule must not run while the switch is off")

    monkeypatch.setattr(
        "meho_backplane.operations.dispatcher.descriptor_requires_target", _must_not_run
    )
    await _seed_ops()
    for hit in (await _hits()).values():
        assert set(hit) == _OLD_HIT_KEYS


def test_switch_off_output_schema_is_the_same_as_before() -> None:
    registered = get_tool("search_operations")
    assert registered is not None
    definition = tool_for_settings(registered[0], _operator())
    assert definition.outputSchema is not None
    ranked, exact = definition.outputSchema["oneOf"]
    assert not CALL_HINT_FIELDS & set(ranked["properties"]["hits"]["items"]["properties"])
    assert not CALL_HINT_FIELDS & set(exact["properties"]["operation"]["properties"])


# ---------------------------------------------------------------------------
# Switch on: the two fields
# ---------------------------------------------------------------------------


async def test_switch_on_hits_carry_required_params_and_target_rule(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch_on(monkeypatch)
    await _seed_ops()
    hits = await _hits()
    for op_id, (_kind, _ref, schema, needs_target) in _OPS.items():
        hit = hits[op_id]
        assert set(hit) == _OLD_HIT_KEYS | CALL_HINT_FIELDS
        assert hit["required_params"] == schema.get("required", [])
        assert hit["target_required"] is needs_target, op_id
    assert hits["demo.secret.read"]["required_params"] == ["path", "mount"]


async def test_switch_on_hit_agrees_with_the_exact_op_contract(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch_on(monkeypatch)
    await _seed_ops()
    hits = await _hits()
    for op_id in _OPS:
        result = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": op_id})
        contract = result["operation"]
        assert hits[op_id]["target_required"] is contract["target"]["required"], op_id
        assert hits[op_id]["required_params"] == contract["parameter_schema"].get("required", [])


async def test_switch_on_unimportable_handler_reads_as_no_target(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """Same as the dispatcher: an unimportable handler is not a target error."""
    _switch_on(monkeypatch)
    gone = {"demo.secret.gone": ("typed", f"{_MODULE}._no_such_handler", {}, False)}
    monkeypatch.setattr(f"{_MODULE}._OPS", {**_OPS, **gone})
    await _seed_ops()
    hits = await _hits()
    assert hits["demo.secret.gone"]["target_required"] is False
    assert hits["demo.secret.gone"]["required_params"] == []


def test_switch_on_output_schema_names_the_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    registered = get_tool("search_operations")
    assert registered is not None
    definition = tool_for_settings(registered[0], _operator())
    assert definition.outputSchema is not None
    ranked, exact = definition.outputSchema["oneOf"]
    hit_properties = ranked["properties"]["hits"]["items"]["properties"]
    assert hit_properties["required_params"]["type"] == "array"
    assert hit_properties["target_required"]["type"] == "boolean"
    assert "required_params" not in ranked["properties"]["hits"]["items"]["required"]
    # The exact-op contract keeps its own ``target`` block.
    assert not CALL_HINT_FIELDS & set(exact["properties"]["operation"]["properties"])


# ---------------------------------------------------------------------------
# MCP wire and REST route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("switch_on", [False, True])
async def test_mcp_wire_answer(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    switch_on: bool,
) -> None:
    if switch_on:
        _switch_on(monkeypatch)
    await _seed_ops()
    client, operator = client_with_operator
    assert operator.tenant_id == OPERATOR_TENANT_ID
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_operations",
                "arguments": {"connector_id": _CONNECTOR, "query": "secret", "limit": 50},
            },
        },
    )
    assert response.status_code == 200
    [content] = response.json()["result"]["content"]
    hits = {hit["op_id"]: hit for hit in json.loads(content["text"])["hits"]}
    read = hits["demo.secret.read"]
    if switch_on:
        assert read["required_params"] == ["path", "mount"]
        assert read["target_required"] is False
        assert hits["GET:/v1/secret/{path}"]["target_required"] is True
    else:
        assert not CALL_HINT_FIELDS & set(read)


@pytest.mark.parametrize("switch_on", [False, True])
async def test_rest_search_answer(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, switch_on: bool
) -> None:
    if switch_on:
        _switch_on(monkeypatch)
    await _seed_ops()
    app = FastAPI()
    app.include_router(operations_api.router)

    async def _caller() -> Operator:
        return _operator()

    app.dependency_overrides[verify_jwt_and_bind] = _caller
    response = TestClient(app).get(
        "/api/v1/operations/search", params={"connector_id": _CONNECTOR, "q": "secret", "limit": 50}
    )
    assert response.status_code == 200
    hits = {hit["op_id"]: hit for hit in response.json()["hits"]}
    bound = hits["demo.secret.bound"]
    if switch_on:
        assert bound["required_params"] == ["path"]
        assert bound["target_required"] is True
    else:
        assert set(bound) == _OLD_HIT_KEYS
