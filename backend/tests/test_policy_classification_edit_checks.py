# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks on the edit of operation classifications (``POLICY_CLASSIFICATION``).

* The approval card of a ``policy.classify`` request shows how many
  operations the entry matches now, the first of them ("and N more"), and
  that a pattern also matches operations added later.
* The doors that edit one connector, group or operation refuse an id with
  ``*``, ``?`` or ``[``.
* The admin MCP edit tools refuse ``tenant_id`` next to ``category`` or
  ``labels``.
* The dry run shows the values in force next to the classified values, so
  a floor that keeps a wait still shows it.
* Only a person asks for ``policy.classify``: the policy gate refuses an
  agent, a service login and a hosted agent run; the run checks the
  recorded requester again; the requester's own run changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import router as connectors_ingest_router
from meho_backplane.api.v1.policy_classification import connector_router, router
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy import classify as classify_module
from meho_backplane.connectors.policy.classify import (
    CARD_FIRST_MATCHES,
    CLASSIFY_NEEDS_PERSON,
    CLASSIFY_NEEDS_SECOND_ADMIN,
    POLICY_CLASSIFY_OP_ID,
    POLICY_REGISTRY_CONNECTOR_ID,
    register_policy_classify_operation,
)
from meho_backplane.connectors.policy.preview import (
    register_policy_classify_preview_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_rerun import rerun_failed_request
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.bulk import preview_classification_edit, request_classification_edit
from meho_backplane.policy.store import ClassificationEdit
from meho_backplane.settings import get_settings
from meho_backplane.ui.templating import get_templates

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000633a")
_FIX = {"product": "clschk", "version": "1.x", "impl_id": "clschk"}
_CONNECTOR = "clschk-1.x"
_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"host": {"type": "string", "x-meho-caller-address": True}},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(
    embedding: AsyncMock,
    op_id: str,
    *,
    group_key: str = "vm",
    parameter_schema: dict[str, Any] | None = None,
) -> None:
    await register_typed_operation(
        **_FIX,
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema=parameter_schema or {"type": "object", "properties": {}},
        group_key=group_key,
        when_to_use="Fixture group.",
        embedding_service=embedding,
    )


@pytest.fixture
async def ops(embedding: AsyncMock) -> None:
    """Two fixture operations in one group, plus the two policy operations."""
    await _register(embedding, "clschk.vm.start")
    await _register(embedding, "clschk.vm.stop")
    await register_policy_classify_operation(embedding_service=embedding)
    await register_policy_classify_preview_operation(embedding_service=embedding)


def _admin(sub: str = "admin-a", *, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        raw_jwt="header.payload.signature",
    )


def _edit(match: dict[str, str], **values: Any) -> ClassificationEdit:
    return ClassificationEdit(
        match=match,
        category=values.get("category"),
        labels=values.get("labels"),
        always_wait=values.get("always_wait"),
    )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _stored() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(TenantPolicy))).scalar_one_or_none()
    return None if row is None else row.document


async def _store_policy(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=_TENANT, document=document, set_by_sub="fixture"))
        await session.commit()


async def _card(match: dict[str, str]) -> dict[str, Any]:
    result = await request_classification_edit(_admin(), _edit(match, category="compute"))
    assert result["status"] == "awaiting_approval", result
    async with get_sessionmaker()() as session:
        request = await session.get(
            ApprovalRequest, uuid.UUID(str(result["extras"]["approval_request_id"]))
        )
    assert request is not None and request.proposed_effect is not None
    card: dict[str, Any] = request.proposed_effect["preview"]
    return card


# ---------------------------------------------------------------------------
# The approval card shows what the entry matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_card_of_a_pattern_shows_the_count_and_later_operations() -> None:
    card = await _card({"op": "clschk.vm.*"})
    assert card["match"] == {"op": "clschk.vm.*"}
    assert card["category"] == "compute"
    assert card["matches_now"] == 2
    assert card["first_matches"] == [
        f"{_CONNECTOR}: clschk.vm.start",
        f"{_CONNECTOR}: clschk.vm.stop",
    ]
    assert "and_more" not in card
    assert "added later" in card["later_operations"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_card_of_one_operation_has_no_later_sentence() -> None:
    card = await _card({"connector": _CONNECTOR, "op": "clschk.vm.start"})
    assert card["matches_now"] == 1
    assert "later_operations" not in card


@pytest.mark.asyncio
async def test_card_names_the_first_matches_and_counts_the_rest(embedding: AsyncMock) -> None:
    extra = 3
    for index in range(CARD_FIRST_MATCHES + extra):
        await _register(embedding, f"clschk.bulk.op{index:02d}", group_key="bulk")
    await register_policy_classify_operation(embedding_service=embedding)
    card = await _card({"connector": _CONNECTOR})
    assert card["matches_now"] == CARD_FIRST_MATCHES + extra
    assert len(card["first_matches"]) == CARD_FIRST_MATCHES
    assert card["and_more"] == f"and {extra} more"
    assert "added later" in card["later_operations"]


# ---------------------------------------------------------------------------
# One-thing doors take the id as plain text
# ---------------------------------------------------------------------------


def _call(method: str, path: str, body: dict[str, Any]) -> Any:
    clear_jwks_cache()
    key = _make_rsa_keypair(f"kid-{uuid.uuid4()}")
    token = _mint_token(
        key, sub="admin-a", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(connector_router)
    app.include_router(connectors_ingest_router)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return TestClient(app).request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param(f"/api/v1/connectors/{_CONNECTOR}/operations/clschk.vm.*", id="op-star"),
        pytest.param(f"/api/v1/connectors/{_CONNECTOR}/operations/clschk.vm.st%3Frt", id="op-?"),
        pytest.param(f"/api/v1/connectors/{_CONNECTOR}/groups/v%5Bm%5D", id="group-["),
        pytest.param("/api/v1/connectors/clschk-*/classification", id="connector-star"),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_rest_one_thing_routes_refuse_a_wildcard(path: str) -> None:
    response = _call("PATCH", path, {"category": "compute"})
    assert response.status_code == 400, response.text
    assert "classification_wildcard" in response.json()["detail"]
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_rest_one_thing_route_still_takes_a_plain_id() -> None:
    path = f"/api/v1/connectors/{_CONNECTOR}/operations/clschk.vm.start"
    response = _call("PATCH", path, {"category": "compute"})
    assert response.status_code == 202, response.text
    (request,) = await _requests()
    assert request.params is not None
    assert request.params["match"] == {"connector": _CONNECTOR, "op": "clschk.vm.start"}


def _tool(name: str) -> Any:
    found = get_tool(name)
    assert found is not None
    return found[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param(
            "meho_connector_edit_op",
            {"connector_id": _CONNECTOR, "op_id": "clschk.vm.*", "category": "compute"},
            id="op",
        ),
        pytest.param(
            "meho_connector_edit_group",
            {"connector_id": "clschk-?.x", "group_key": "vm", "labels": {"team": "ops"}},
            id="group",
        ),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_mcp_edit_tools_refuse_a_wildcard(name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(McpInvalidParamsError, match="classification_wildcard"):
        await _tool(name)(_admin(), arguments)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param(
            "meho_connector_edit_op",
            {"connector_id": _CONNECTOR, "op_id": "clschk.vm.start", "category": "compute"},
            id="op-category",
        ),
        pytest.param(
            "meho_connector_edit_group",
            {"connector_id": _CONNECTOR, "group_key": "vm", "labels": {"team": "ops"}},
            id="group-labels",
        ),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_mcp_edit_tools_refuse_tenant_id_with_a_classification(
    name: str, arguments: dict[str, Any]
) -> None:
    other = str(uuid.uuid4())
    with pytest.raises(McpInvalidParamsError, match="classification_tenant_id"):
        await _tool(name)(_admin(), {**arguments, "tenant_id": other})
    assert await _requests() == []


# ---------------------------------------------------------------------------
# The dry run shows the values in force
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_shows_that_a_floor_still_waits(embedding: AsyncMock) -> None:
    await _register(embedding, "clschk.vm.start")
    await _register(embedding, "clschk.vm.probe", parameter_schema=_ADDRESS_SCHEMA)
    entry = {"match": {"op": "clschk.vm.*"}, "always_wait": True}
    await _store_policy({"classifications": [entry]})
    preview = await preview_classification_edit(
        _admin(), _edit({"op": "clschk.vm.*"}, always_wait=False)
    )
    rows = {row["op_id"]: row for row in preview.to_dict()["operations"]}
    probe, start = rows["clschk.vm.probe"], rows["clschk.vm.start"]
    assert (probe["before"]["always_wait"], probe["after"]["always_wait"]) == (True, False)
    assert probe["before"]["in_force"] == {"risk": "caution", "waits": True}
    assert probe["after"]["in_force"] == {"risk": "caution", "waits": True}
    assert start["before"]["in_force"] == {"risk": "safe", "waits": True}
    assert start["after"]["in_force"] == {"risk": "safe", "waits": False}

    html = (
        get_templates()
        .env.get_template("policy/_classify_result.html")
        .render(error=None, requested=None, form={}, preview=preview.to_dict())
    )
    assert "In force" in html
    assert "caution, waits &rarr; caution, waits" in html
    assert "safe, waits &rarr; safe, runs at once" in html


# ---------------------------------------------------------------------------
# Only a person asks for policy.classify, and a second admin runs it
# ---------------------------------------------------------------------------

_PARAMS = {"match": {"op": "clschk.vm.*"}, "category": "compute"}


async def _dispatch(operator: Operator) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=POLICY_REGISTRY_CONNECTOR_ID,
        op_id=POLICY_CLASSIFY_OP_ID,
        target=None,
        params=dict(_PARAMS),
    )


async def _approve(request_id: uuid.UUID, approver: Operator) -> Any:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
@pytest.mark.usefixtures("ops")
async def test_dispatch_refuses_a_requester_who_is_not_a_person(kind: PrincipalKind) -> None:
    result = await _dispatch(_admin("caller", kind=kind))
    assert result.status == "denied", result
    assert CLASSIFY_NEEDS_PERSON in str(result.error)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_dispatch_refuses_a_hosted_agent_run() -> None:
    with actor_delegation("agent:helper"):
        result = await _dispatch(_admin())
    assert result.status == "denied", result
    assert CLASSIFY_NEEDS_PERSON in str(result.error)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_preview_denies_what_dispatch_denies() -> None:
    preview = await preview_dispatch(
        operator=_admin("caller", kind=PrincipalKind.AGENT),
        connector_id=POLICY_REGISTRY_CONNECTOR_ID,
        op_id=POLICY_CLASSIFY_OP_ID,
        target=None,
        params=dict(_PARAMS),
    )
    assert preview["policy_verdict"] == "deny", preview
    assert CLASSIFY_NEEDS_PERSON in str(preview["policy_reason"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_second_admin_runs_the_change_and_the_card_records_the_requester() -> None:
    result = await _dispatch(_admin("admin-a"))
    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    record = request.proposed_effect["permission_preflight"]["requester"]  # type: ignore[index]
    assert record["sub"] == "admin-a" and record["principal_kind"] == "user"
    ran = await _approve(request.id, _admin("admin-b"))
    assert ran.status == "ok", ran
    assert (await _stored() or {})["classifications"] == [
        {"match": {"op": "clschk.vm.*"}, "category": "compute"}
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_requester_cannot_rerun_a_failed_approved_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _dispatch(_admin("admin-a"))
    (request,) = await _requests()

    async def _fails(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("The first run failed.")

    with monkeypatch.context() as m:
        m.setattr(classify_module, "set_classification", _fails)
        first = await _approve(request.id, _admin("admin-b"))
    assert first.status != "ok"
    outcome = await rerun_failed_request(operator=_admin("admin-a"), request_id=request.id)
    assert outcome.result.status != "ok"
    assert CLASSIFY_NEEDS_SECOND_ADMIN in str(outcome.result.error) + str(outcome.result.extras)
    assert await _stored() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [None, {"sub": "admin-a", "principal_kind": "agent", "platform_admin": False, "act": None}],
)
@pytest.mark.usefixtures("ops")
async def test_the_run_checks_the_recorded_requester_again(record: dict[str, Any] | None) -> None:
    await _dispatch(_admin("admin-a"))
    (request,) = await _requests()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request.id)
        assert row is not None
        effect = dict(row.proposed_effect or {})
        effect["permission_preflight"] = None if record is None else {"requester": record}
        row.proposed_effect = effect
        await session.commit()
    result = await _approve(request.id, _admin("admin-b"))
    assert result.status != "ok"
    assert CLASSIFY_NEEDS_PERSON in str(result.error) + str(result.extras)
    assert await _stored() is None
