# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search hits, the call contract and the approval request show the classification.

* With the ``POLICY_CLASSIFICATION`` switch on, a ranked search hit and the
  call contract of exact-op mode carry ``category`` and ``labels``, and an
  approval request carries ``category``, ``labels`` and
  ``classification_rule``.
* A conflict between two entries shows as a null category.
* With the switch off, every answer has exactly the keys it had before,
  in the same order, and the ``search_operations`` output schema is the
  one from before.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.tools._search_classification import (
    CLASSIFICATION_PROPERTIES,
    classification_schema_for_settings,
)
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006041")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006042")
_FIX = {"product": "clsshow", "version": "1.x", "impl_id": "clsshow"}
_CONNECTOR = "clsshow-1.x"
_OP = "clsshow.switch.port.disable"

#: The keys of a ranked hit and of the call contract before the change.
_HIT_KEYS = ["op_id", "summary", "description", "group_key", "safety_level", "requires_approval"]
_CONTRACT_KEYS = [
    "connector_id",
    "op_id",
    "summary",
    "description",
    "group_key",
    "safety_level",
    "requires_approval",
    "parameter_schema",
    "examples",
    "target",
    "result_shape",
    "pagination_hint",
    "recovery",
]

_NETWORK = {
    "classifications": [
        {"match": {"connector": _CONNECTOR}, "category": "network", "labels": {"team": "net"}},
        {"match": {"op": "clsshow.switch.*"}, "labels": {"env": "prod"}},
    ]
}
_CONFLICT = {
    "classifications": [
        {"match": {"op": "clsshow.switch.*"}, "category": "network"},
        {"match": {"op": "clsshow.switch.?ort.disable"}, "category": "storage"},
    ]
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if on else "false")
    get_settings.cache_clear()


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(embedding: AsyncMock) -> None:
    """A ``caution`` op: a service call waits for approval."""
    await register_typed_operation(
        **_FIX,
        op_id=_OP,
        handler=_handler,
        summary="Disable a switch port.",
        description="Disable a switch port.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="ports",
        when_to_use="Fixture group.",
        safety_level="caution",
        embedding_service=embedding,
    )


async def _store_policy(document: dict[str, Any], tenant_id: uuid.UUID | None = _TENANT) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


def _operator(tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub="svc:shown",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _hit(tenant_id: uuid.UUID = _TENANT) -> dict[str, Any]:
    answer = await search_operations(
        _operator(tenant_id), {"connector_id": _CONNECTOR, "query": "disable switch port"}
    )
    hits = [hit for hit in answer["hits"] if hit["op_id"] == _OP]
    assert len(hits) == 1, answer
    hit: dict[str, Any] = hits[0]
    return hit


async def _contract() -> dict[str, Any]:
    answer = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": _OP})
    contract: dict[str, Any] = answer["operation"]
    return contract


async def _approval_effect() -> dict[str, Any]:
    call = await call_operation(_operator(), {"connector_id": _CONNECTOR, "op_id": _OP})
    assert call["status"] == "awaiting_approval", call
    request_id = uuid.UUID(str(call["extras"]["approval_request_id"]))
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    effect: dict[str, Any] = request.proposed_effect or {}
    return effect


# ---------------------------------------------------------------------------
# Switch on: the three places show the same classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_hit_shows_category_and_labels(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(_NETWORK)

    hit = await _hit()
    assert list(hit) == [*_HIT_KEYS, "category", "labels"]
    assert (hit["category"], hit["labels"]) == ("network", {"team": "net", "env": "prod"})


@pytest.mark.asyncio
async def test_search_hit_is_for_the_callers_tenant(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another tenant's entry does not show; the hit has the default category."""
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(_NETWORK)

    hit = await _hit(_OTHER_TENANT)
    assert (hit["category"], hit["labels"]) == ("other", {})


@pytest.mark.asyncio
async def test_contract_shows_category_and_labels(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(_NETWORK)

    contract = await _contract()
    keys = list(contract)
    assert keys[: keys.index("parameter_schema")] == [*_CONTRACT_KEYS[:7], "category", "labels"]
    assert (contract["category"], contract["labels"]) == ("network", {"team": "net", "env": "prod"})


@pytest.mark.asyncio
async def test_approval_request_carries_category_labels_and_rule(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(_NETWORK)

    effect = await _approval_effect()
    assert effect["category"] == "network"
    assert effect["labels"] == {"team": "net", "env": "prod"}
    assert effect["classification_rule"] == "tenant:classifications[1]"


@pytest.mark.asyncio
async def test_a_conflict_shows_as_a_null_category(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(_CONFLICT)

    hit = await _hit()
    contract = await _contract()
    effect = await _approval_effect()
    assert hit["category"] is None
    assert contract["category"] is None
    assert effect["category"] is None
    assert hit["requires_approval"] is True


# ---------------------------------------------------------------------------
# Switch off: every answer is as before
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_answers_are_as_before(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored document changes nothing: same keys, same order, no classification."""
    _switch(monkeypatch, False)
    await _register(embedding)
    await _store_policy(_NETWORK)

    hit = await _hit()
    assert list(hit) == _HIT_KEYS
    contract = await _contract()
    assert list(contract) == _CONTRACT_KEYS
    effect = await _approval_effect()
    assert not {"category", "labels", "classification_rule"} & effect.keys()


@pytest.mark.asyncio
async def test_switch_off_scores_keep_their_order(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant admin who asks for the scores gets the hit as before."""
    _switch(monkeypatch, False)
    await _register(embedding)
    admin = _operator().model_copy(
        update={"tenant_role": TenantRole.TENANT_ADMIN, "principal_kind": PrincipalKind.USER}
    )
    answer = await search_operations(
        admin, {"connector_id": _CONNECTOR, "query": "disable switch port", "include_scores": True}
    )
    assert list(answer["hits"][0]) == [*_HIT_KEYS, "fused_score", "bm25_score", "cosine_score"]


# ---------------------------------------------------------------------------
# The search_operations output schema
# ---------------------------------------------------------------------------


def _search_tool() -> ToolDefinition:
    from meho_backplane.mcp import registry as mcp_registry
    from meho_backplane.mcp.tools import operations  # noqa: F401  (registers the tool)

    definition: ToolDefinition = mcp_registry._TOOLS["search_operations"][0]
    return definition


def _object_schemas(definition: ToolDefinition) -> list[dict[str, Any]]:
    """The hit schema and the contract schema of the output schema."""
    ranked, exact = (definition.outputSchema or {})["oneOf"]
    return [ranked["properties"]["hits"]["items"], exact["properties"]["operation"]]


def test_output_schema_names_the_fields_while_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    definition = _search_tool()
    shown = classification_schema_for_settings(definition)
    assert shown is definition
    for schema in _object_schemas(shown):
        for name, value in CLASSIFICATION_PROPERTIES.items():
            assert schema["properties"][name] == value


def test_output_schema_is_as_before_while_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema without the two fields is, byte for byte, the registered one without them."""
    _switch(monkeypatch, False)
    definition = _search_tool()
    shown = classification_schema_for_settings(definition)
    for schema in _object_schemas(shown):
        assert not CLASSIFICATION_PROPERTIES.keys() & schema["properties"].keys()
    expected = json.loads(json.dumps(definition.outputSchema))
    for schema in _object_schemas(definition.model_copy(update={"outputSchema": expected})):
        for name in CLASSIFICATION_PROPERTIES:
            del schema["properties"][name]
    assert json.dumps(shown.outputSchema) == json.dumps(expected)
    assert shown.inputSchema == definition.inputSchema
    assert shown.description == definition.description
