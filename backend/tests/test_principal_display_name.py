# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An agent's rows and approval requests carry the agent's name (#3885).

An agent token (``client_credentials``) has no ``name`` claim. The name
MEHO stores comes from
:func:`~meho_backplane.auth.display_name.principal_display_name`: the
token's ``name``, else the registered agent name (tenant-scoped), else the
client id. These tests pin the helper and each place that stores the name:

* the operation run row and the refusal row (``write_audit_row``);
* the MCP tool row (``write_mcp_audit_row``);
* the tool row of MEHO's own agent runs (``write_agent_tool_row``);
* the approval request and its audit row (``create_pending_request``).

A person's token keeps its ``name`` claim on every path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID

import pytest

from meho_backplane.agent.tool_audit import write_agent_tool_row
from meho_backplane.auth.display_name import principal_display_name
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    ApprovalRequest,
    AuditEventClass,
    AuditLog,
    Tenant,
)
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request, requester_of
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000c3a1")
_OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000c3a2")
_CLIENT_ID = "agent:incident-triage"
_AGENT_NAME = "incident-triage"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_agent(
    *, tenant_id: UUID = _TENANT, client_id: str = _CLIENT_ID, revoked: bool = False
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[-4:]}", name="T"))
            await session.flush()
        session.add(
            AgentPrincipal(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=_AGENT_NAME,
                keycloak_client_id=client_id,
                keycloak_internal_id=f"kc-{uuid.uuid4().hex[:8]}",
                owner_sub="op-admin",
                revoked=revoked,
                created_by_sub="op-admin",
            )
        )


def _agent(*, client_id: str | None = _CLIENT_ID, name: str | None = None) -> Operator:
    return Operator(
        sub="11111111-2222-3333-4444-555555555555",
        name=name,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=client_id,
    )


def _person(name: str | None = "Alice Example") -> Operator:
    return Operator(
        sub="user-alice",
        name=name,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _display_name(operator: Operator) -> str | None:
    async with get_sessionmaker()() as session:
        return await principal_display_name(session, operator)


async def _row(audit_id: UUID) -> AuditLog:
    async with get_sessionmaker()() as session:
        row = await session.get(AuditLog, audit_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_name_claim_wins() -> None:
    assert await _display_name(_person()) == "Alice Example"


@pytest.mark.asyncio
async def test_person_without_name_gets_none() -> None:
    assert await _display_name(_person(name=None)) is None


@pytest.mark.asyncio
async def test_agent_name_claim_wins_over_registered_name() -> None:
    await _seed_agent()
    assert await _display_name(_agent(name="From Token")) == "From Token"


@pytest.mark.asyncio
async def test_registered_agent_gets_its_registered_name() -> None:
    await _seed_agent()
    assert await _display_name(_agent()) == _AGENT_NAME


@pytest.mark.asyncio
async def test_revoked_agent_still_gets_its_registered_name() -> None:
    await _seed_agent(revoked=True)
    assert await _display_name(_agent()) == _AGENT_NAME


@pytest.mark.asyncio
async def test_unregistered_agent_gets_its_client_id() -> None:
    assert await _display_name(_agent()) == _CLIENT_ID


@pytest.mark.asyncio
async def test_agent_registered_in_another_tenant_gets_only_its_client_id() -> None:
    await _seed_agent(tenant_id=_OTHER_TENANT)
    assert await _display_name(_agent()) == _CLIENT_ID


@pytest.mark.asyncio
async def test_service_token_gets_its_client_id() -> None:
    service = _agent().model_copy(
        update={"principal_kind": PrincipalKind.SERVICE, "client_id": "pipeline"}
    )
    assert await _display_name(service) == "pipeline"


@pytest.mark.asyncio
async def test_agent_without_client_id_gets_none() -> None:
    assert await _display_name(_agent(client_id=None)) is None


@pytest.mark.asyncio
async def test_person_with_a_client_id_gets_no_name() -> None:
    person = _person(name=None).model_copy(update={"client_id": "not-for-a-person"})
    assert await _display_name(person) is None


# ---------------------------------------------------------------------------
# The places that store the name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_class", "result_status"),
    [
        (AuditEventClass.OPERATION_EXECUTION, "ok"),
        (AuditEventClass.POLICY_DECISION, "denied"),
    ],
    ids=["run-row", "refusal-row"],
)
async def test_dispatch_rows_carry_the_agent_name(
    event_class: AuditEventClass, result_status: str
) -> None:
    await _seed_agent()
    descriptor = SimpleNamespace(
        op_id="demo.thing.get",
        source_kind="typed",
        product="demo",
        version="1.x",
        impl_id="demo",
    )
    audit_id = uuid.uuid4()

    await write_audit_row(
        audit_id=audit_id,
        event_class=event_class,
        operator=_agent(),
        descriptor=descriptor,  # type: ignore[arg-type]
        target=None,
        params_hash="",
        result_status=result_status,
        duration_ms=1.0,
    )

    assert (await _row(audit_id)).payload["principal_name"] == _AGENT_NAME


@pytest.mark.asyncio
async def test_mcp_tool_row_carries_the_agent_name() -> None:
    await _seed_agent()

    audit_id = await write_mcp_audit_row(
        event_class=AuditEventClass.LEGACY_REQUEST,
        operator=_agent(),
        method="MCP",
        path="/mcp/tools/call/meho_status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho_status", "op_class": "read"},
    )

    assert (await _row(audit_id)).payload["principal_name"] == _AGENT_NAME


@pytest.mark.asyncio
async def test_mcp_tool_row_of_a_person_keeps_the_name_claim() -> None:
    audit_id = await write_mcp_audit_row(
        event_class=AuditEventClass.LEGACY_REQUEST,
        operator=_person(),
        method="MCP",
        path="/mcp/tools/call/meho_status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho_status", "op_class": "read"},
    )

    assert (await _row(audit_id)).payload["principal_name"] == "Alice Example"


@pytest.mark.asyncio
async def test_agent_tool_row_carries_the_client_id_when_unregistered() -> None:
    audit_id = await write_agent_tool_row(
        operator=_agent(),
        tool_name="meho_status",
        args={},
        status_code=200,
        duration_ms=1.0,
    )

    assert (await _row(audit_id)).payload["principal_name"] == _CLIENT_ID


@pytest.mark.asyncio
async def test_approval_request_and_its_audit_row_carry_the_agent_name_and_kind() -> None:
    await _seed_agent()
    params = {"key": "value"}

    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_agent(),
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        await session.commit()
        request_id = request.id
        audit_id = request.request_audit_id

    async with get_sessionmaker()() as session:
        stored = await session.get(ApprovalRequest, request_id)
        assert stored is not None
        audit = await session.get(AuditLog, audit_id)
        assert audit is not None

    assert stored.principal_name == _AGENT_NAME
    assert stored.principal_kind == "agent"
    assert audit.payload["principal_name"] == _AGENT_NAME
    # The saved requester keeps the kind, so a composite step that waits
    # again during the re-run is saved for the same agent.
    assert requester_of(stored).kind == "agent"


@pytest.mark.asyncio
async def test_approval_request_of_a_person_keeps_the_name_claim() -> None:
    params = {"key": "value"}

    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(),
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        await session.commit()

    assert request.principal_name == "Alice Example"
    assert request.principal_kind == "user"


@pytest.mark.asyncio
async def test_composite_re_park_keeps_the_original_requester_kind() -> None:
    """A step re-parked while a reviewer re-runs it belongs to the agent."""
    await _seed_agent()
    params = {"key": "value"}

    async with get_sessionmaker()() as session:
        first = await create_pending_request(
            session,
            operator=_agent(),
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        again = await create_pending_request(
            session,
            operator=_person(name="Bob Reviewer"),
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            requester=requester_of(first),
        )
        await session.commit()

    assert again.principal_sub == first.principal_sub
    assert again.principal_name == _AGENT_NAME
    assert again.principal_kind == "agent"
