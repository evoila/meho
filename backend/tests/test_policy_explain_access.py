# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who sees what in ``POST /api/v1/policy/explain``, and what a what-if question reads.

* Only a tenant admin or a platform admin sees the entries of the policy
  and their places. An operator and an agent get the answer, the reason,
  the risk level and who may approve.
* The made-up login of a what-if question: no grant can name it, and the
  question never reads the grants of the real caller or of a hosted agent
  that acts for the caller.
* ``request_id``: the groups stored on a request count only while
  ``POLICY_APPROVER_ROUTING`` is on, as in the approve check.
* A stored routing that is not valid: explain answers 409 with its
  sentence; the preview still answers, with ``approvers: null``.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.agents.grant_schemas import AgentGrantCreate, GrantPrincipalKind
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantError
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    EndpointDescriptor,
    PermissionVerdict,
    TenantPolicy,
)
from meho_backplane.main import app
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import GrantValidationError as ServiceGrantError
from meho_backplane.operations.service_grants import ServicePrincipalGrantService
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.policy.explain_asker import WHAT_IF_SUB, WHAT_IF_SUB_REFUSAL, what_if_operator
from meho_backplane.policy.who_approves import preview_policy_fields
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_CONNECTOR = "explacc-1.x"
_SAFE = "explacc.vm.list"
_CAUTION = "explacc.vm.restart"

_INSTALL_POLICY: dict[str, Any] = {
    "policy": "v1",
    "classifications": [
        {"match": {"op": _CAUTION}, "category": "compute", "labels": {"team": "blue"}}
    ],
    "routing": [{"match": {"category": "compute"}, "approvers": ["Admins"]}],
}


def _switches(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _setup(embedding: AsyncMock) -> None:
    for op_id, level in ((_SAFE, "safe"), (_CAUTION, "caution")):
        await register_typed_operation(
            product="explacc",
            version="1.x",
            impl_id="explacc",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=level,  # type: ignore[arg-type]
            requires_approval=False,
            embedding_service=embedding,
        )


async def _store_policy(document: dict[str, Any], tenant_id: uuid.UUID | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="admin-a"))
        await session.commit()


def _token(key: Any, *, sub: str = "admin-a", **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(_TENANT), **claims)


def _ask(client: TestClient, token: str, **body: Any) -> Any:
    return client.post(
        "/api/v1/policy/explain", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def _me(op_id: str) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR, "op_id": op_id, "me": True}


def _paths(body: dict[str, Any]) -> list[str]:
    """Every place in the policy the answer names."""
    rules = body["evaluator"]["rules"] if body["evaluator"] else []
    found = [rule["path"] for rule in rules if rule["path"]]
    found += [entry["path"] for entry in body["classification"]["entries"]]
    if body["approvers"] and body["approvers"]["rule"]:
        found.append(body["approvers"]["rule"])
    return found


# ---------------------------------------------------------------------------
# Who sees the policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_an_admin_sees_the_policy_entries(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(
        monkeypatch, "POLICY_ENGINE_SHADOW", "POLICY_CLASSIFICATION", "POLICY_APPROVER_ROUTING"
    )
    await _setup(embedding)
    await _store_policy(_INSTALL_POLICY)
    key = make_rsa_keypair("kid-explacc-see")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        admin = _ask(client, _token(key), **_me(_CAUTION)).json()
        operator = _ask(client, _token(key, sub="op-a", tenant_role="operator"), **_me(_CAUTION))
        agent = _ask(
            client,
            _token(key, sub="agent-a", tenant_role="operator", principal_kind="agent"),
            **_me(_CAUTION),
        )
    assert admin["classification"]["category"] == "compute", admin
    assert admin["classification"]["labels"] == {"team": "blue"}
    assert [entry["path"] for entry in admin["classification"]["entries"]] == [
        "install:classifications[0]"
    ]
    for resp in (operator, agent):
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == "wait", body
        assert body["today"]["reason"]
        assert body["classification"] == {
            "risk_level": "caution",
            "requires_approval": False,
            "category": None,
            "labels": {},
            "entries": [],
        }
        assert _paths(body) == []
    operator_body = operator.json()
    assert operator_body["approvers"] == {"groups": ["MEHO-Admins"], "rule": None, "floor": None}
    assert operator_body["evaluator"]["rules"], operator_body


# ---------------------------------------------------------------------------
# The made-up login of a what-if question
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_grant_can_name_the_what_if_login() -> None:
    agent_grant = AgentGrantCreate(
        principal_sub=WHAT_IF_SUB,
        op_pattern="*",
        verdict=PermissionVerdict.AUTO_EXECUTE,
        principal_kind=GrantPrincipalKind.USER_AGENT,
    )
    with pytest.raises(AgentGrantError, match="made-up login"):
        await AgentGrantService().grant(_TENANT, "admin-a", agent_grant)
    standing = ServiceGrantCreate(
        principal_sub=WHAT_IF_SUB, op_id=_SAFE, connector_id=_CONNECTOR, reason="Fixture grant."
    )
    with pytest.raises(ServiceGrantError) as refused:
        await ServicePrincipalGrantService().create(_TENANT, "admin-a", standing)
    assert str(refused.value) == WHAT_IF_SUB_REFUSAL


async def _deny_grant(principal_sub: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=principal_sub,
                op_pattern="explacc.*",
                verdict=PermissionVerdict.DENY.value,
                created_by_sub="admin-a",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_what_if_question_never_reads_the_callers_or_a_hosted_agents_grants(
    embedding: AsyncMock,
) -> None:
    await _setup(embedding)
    caller = Operator(
        sub="admin-a",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        client_id="admin-a",
    )
    await _deny_grant("admin-a")
    await _deny_grant("agent:helper")

    def question(me: bool, principal: Any = "person") -> CallQuestion:
        asker = (
            caller
            if me
            else what_if_operator(caller, groups=["Operators"], principal=principal, via=None)
        )
        return CallQuestion(asker=asker, me=me, connector_id=_CONNECTOR, op_id=_SAFE)

    with actor_delegation("agent:helper"):
        # The caller's own question counts the hosted agent's deny grant.
        assert (await explain_call(question(me=True))).answer == "deny"
        for principal in ("person", "agent", "service"):
            answer = await explain_call(question(me=False, principal=principal))
            assert answer.answer == "run", (principal, answer)


# ---------------------------------------------------------------------------
# The approver groups of a stored request
# ---------------------------------------------------------------------------


async def _waiting_request(embedding: AsyncMock) -> str:
    await _setup(embedding)
    caller = Operator(
        sub="op-a", raw_jwt="<jwt>", tenant_id=_TENANT, tenant_role=TenantRole.OPERATOR
    )
    result = await dispatch(
        operator=caller, connector_id=_CONNECTOR, op_id=_CAUTION, target=None, params={}
    )
    assert result.status == "awaiting_approval", result
    request_id = uuid.UUID(str(result.extras["approval_request_id"]))
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
        row.approver_roles = ["Admins"]
        row.approver_rule = "tenant:routing[0]"
        await session.commit()
    return str(request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("routing", [False, True])
async def test_a_request_shows_the_groups_the_approve_check_uses(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, routing: bool
) -> None:
    request_id = await _waiting_request(embedding)
    if routing:
        _switches(monkeypatch, "POLICY_APPROVER_ROUTING")
    key = make_rsa_keypair(f"kid-explacc-request-{routing}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(client, _token(key), request_id=request_id).json()
    approvers = body["request"]["approvers"]
    if routing:
        assert approvers == {"groups": ["MEHO-Admins"], "rule": "tenant:routing[0]", "floor": None}
    else:
        assert approvers == {
            "groups": ["MEHO-Operators", "MEHO-SA-Operators", "MEHO-Admins", "MEHO-Approvers"],
            "rule": "default",
            "floor": None,
        }


# ---------------------------------------------------------------------------
# A stored routing that is not valid
# ---------------------------------------------------------------------------

_BAD_ROUTING: dict[str, Any] = {"policy": "v1", "routing": "everyone"}


@pytest.mark.asyncio
async def test_a_stored_routing_that_is_not_valid_is_409(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch, "POLICY_APPROVER_ROUTING")
    await _setup(embedding)
    await _store_policy(_BAD_ROUTING, _TENANT)
    key = make_rsa_keypair("kid-explacc-badroute")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        stored = _ask(client, _token(key), **_me(_CAUTION))
        _switches(monkeypatch, "POLICY_CLASSIFICATION")
        classified = _ask(client, _token(key), **_me(_CAUTION))
    for resp in (stored, classified):
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("The stored routing is not valid"), detail
        assert "`routing` must be a list" in detail


@pytest.mark.asyncio
async def test_the_preview_still_answers_with_a_routing_that_is_not_valid(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch, "POLICY_ENGINE_SHADOW", "POLICY_APPROVER_ROUTING")
    await _setup(embedding)
    await _store_policy(_BAD_ROUTING, _TENANT)
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _CAUTION)
            )
        ).scalar_one()
    fields = await preview_policy_fields(
        tenant_id=_TENANT,
        connector_id=_CONNECTOR,
        descriptor=descriptor,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
        stamp=None,
    )
    assert fields == {"policy_rule": None, "approvers": None}
