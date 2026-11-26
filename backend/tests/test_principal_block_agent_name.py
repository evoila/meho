# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A block of an agent by the name MEHO shows for it (``agent:<name>``).

What these tests check:

* An admin blocks ``agent:<name>``. The agent's token is refused on REST,
  on MCP and on an operation call, also a new token and a token MEHO never
  saw before. An unblock by the name lets the token work again.
* A check that knows only the login id refuses the agent too: an approved
  call that the agent asked for does not run.
* Another process sees the block and the unblock within the cache time.
* A block of a name that is no agent MEHO knows is refused with a plain
  sentence, and no block is stored.
* The block answer and the list name the agent, also for a block of the
  agent's login id.
* MEHO keeps the client id of a machine login (``machine_login.client_id``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi import HTTPException
from httpx import ASGITransport
from sqlalchemy import delete, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.machine_logins import note_machine_login
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    BLOCK_CACHE_SECONDS,
    BLOCK_SCOPE_ALL,
    PRINCIPAL_BLOCKED_MESSAGE,
    block_principal,
    is_principal_blocked,
    reset_block_cache,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    ApprovalRequest,
    ApprovalRequestStatus,
    MachineLogin,
    PrincipalBlock,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000a6e1")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000a6e2")
_MCP_AUDIENCE = "https://meho.test/mcp"
_AGENT = "agent:probe-bot"
_AGENT_SUB = "5b1c9d1e-0000-4000-8000-00000000a6e1"
_ROUTE = "/api/v1/principals/blocked"
_WORK_ROUTE = "/api/v1/targets"
_BLOCK_MODULE = "meho_backplane.auth.principal_block"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", _MCP_AUDIENCE)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_block_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_block_cache()


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-agent-block")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _seed(*, revoked: bool = False) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="agent-block-a", name="Agent block A"))
        session.add(Tenant(id=_TENANT_B, slug="agent-block-b", name="Agent block B"))
        await session.flush()
        session.add(
            AgentPrincipal(
                tenant_id=_TENANT_A,
                name="probe-bot",
                keycloak_client_id=_AGENT,
                keycloak_internal_id="client-internal-id",
                owner_sub="owner-person",
                created_by_sub="owner-person",
                revoked=revoked,
            )
        )
        await session.commit()


def _agent_token(key: Any, *, audience: str | None = None, jti: str | None = None) -> str:
    """A client-credentials token of the agent: the client id is in ``azp`` only."""
    return mint_token(
        key,
        sub=_AGENT_SUB,
        name=None,
        email=None,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind="agent",
        audience=audience,
        extra_claims={"azp": _AGENT, "jti": jti or str(uuid.uuid4())},
    )


def _person_token(
    key: Any, sub: str, *, tenant: uuid.UUID = _TENANT_A, platform_admin: bool | None = None
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(tenant),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _blocked(response: httpx.Response) -> bool:
    return response.status_code == 403 and "principal_blocked" in response.text


async def _block(ac: httpx.AsyncClient, admin: str, sub: str = _AGENT) -> httpx.Response:
    return await ac.post(_ROUTE, json={"sub": sub, "reason": "agent revoked"}, headers=_auth(admin))


def _admin_operator(*, platform_admin: bool = False) -> Operator:
    return Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        platform_admin=platform_admin,
    )


def _agent_operator(*, client_id: str | None = _AGENT) -> Operator:
    return Operator(
        sub=_AGENT_SUB,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=client_id,
    )


async def _note_agent_login(client_id: str | None = _AGENT) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await note_machine_login(session, _agent_operator(client_id=client_id))


async def _block_rows() -> list[str]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(PrincipalBlock.sub))).scalars())


# ---------------------------------------------------------------------------
# The token of the agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_by_agent_name_refuses_old_and_new_token_and_unblock_lets_it_work(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    old = _agent_token(key)
    admin = _person_token(key, "admin-person")
    assert not _blocked(await ac.get(_WORK_ROUTE, headers=_auth(old)))

    created = await _block(ac, admin)
    assert created.status_code == 201, created.text
    assert created.json()["sub"] == _AGENT
    assert created.json()["agent"] == _AGENT

    refused = await ac.get(_WORK_ROUTE, headers=_auth(old))
    assert _blocked(refused)
    assert refused.json()["detail"] == f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"
    assert PRINCIPAL_BLOCKED_MESSAGE.startswith("This login is blocked")
    assert _blocked(await ac.get(_WORK_ROUTE, headers=_auth(_agent_token(key))))
    with pytest.raises(HTTPException) as on_mcp:
        await verify_mcp_jwt(f"Bearer {_agent_token(key, audience=_MCP_AUDIENCE)}", None)
    assert on_mcp.value.status_code == 403
    assert PRINCIPAL_BLOCKED_MESSAGE in str(on_mcp.value.detail)

    removed = await ac.delete(f"{_ROUTE}/{_AGENT}", headers=_auth(admin))
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"sub": _AGENT, "removed": 1}
    assert not _blocked(await ac.get(_WORK_ROUTE, headers=_auth(old)))


@pytest.mark.asyncio
async def test_agent_token_that_meho_never_saw_is_refused_by_the_agent_name(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    assert (await _block(ac, _person_token(key, "admin-person"))).status_code == 201

    assert _blocked(await ac.get(_WORK_ROUTE, headers=_auth(_agent_token(key))))


@pytest.mark.asyncio
async def test_block_by_login_id_still_refuses_the_agent_token(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    admin = _person_token(key, "admin-person")
    assert not _blocked(await ac.get(_WORK_ROUTE, headers=_auth(_agent_token(key))))

    created = await _block(ac, admin, _AGENT_SUB)
    assert created.status_code == 201, created.text
    # MEHO saw the login id with the agent's client id, so the answer names the agent.
    assert created.json()["agent"] == _AGENT
    assert _blocked(await ac.get(_WORK_ROUTE, headers=_auth(_agent_token(key))))

    assert (await ac.delete(f"{_ROUTE}/{_AGENT_SUB}", headers=_auth(admin))).status_code == 200
    assert not _blocked(await ac.get(_WORK_ROUTE, headers=_auth(_agent_token(key))))


@pytest.mark.asyncio
async def test_block_of_an_agent_in_one_tenant_does_not_reach_another_tenant() -> None:
    await _seed()
    await block_principal(_admin_operator(), _AGENT, "agent revoked")

    assert await is_principal_blocked(_AGENT_SUB, _TENANT_A, client_id=_AGENT)
    assert not await is_principal_blocked(_AGENT_SUB, _TENANT_B, client_id=_AGENT)


# ---------------------------------------------------------------------------
# Operation calls and approved calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operation_call_of_an_agent_blocked_by_name_does_not_run() -> None:
    await _seed()
    await block_principal(_admin_operator(), _AGENT, "agent revoked")

    result = await dispatch(
        operator=_agent_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.get",
        target=None,
        params={},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "principal_blocked"


@pytest.mark.asyncio
async def test_check_by_login_id_alone_refuses_an_agent_blocked_by_name() -> None:
    await _seed()
    await _note_agent_login()
    await block_principal(_admin_operator(), _AGENT, "agent revoked")

    assert await is_principal_blocked(_AGENT_SUB, _TENANT_A)


@pytest.mark.asyncio
async def test_approved_call_of_an_agent_blocked_by_name_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    await _note_agent_login()
    params = {"path": "app/config", "value": "<test>"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_agent_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.put",
            target=None,
            params=dict(params),
            params_hash=compute_params_hash(params),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        request.reviewed_by = "approver-person"
        await session.commit()
        request_id = request.id
    calls: list[dict[str, Any]] = []

    async def _fake_dispatch(**kwargs: Any) -> OperationResult:
        calls.append(kwargs)
        return OperationResult(status="ok", op_id=kwargs["op_id"], data={}, duration_ms=1.0)

    monkeypatch.setattr("meho_backplane.operations.dispatcher.dispatch", _fake_dispatch)
    await block_principal(_admin_operator(), _AGENT, "agent revoked")

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    approver = Operator(
        sub="approver-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    result = await resume_dispatch_after_approval(operator=approver, request=row)

    assert calls == []
    assert result.status == "denied"
    assert result.extras["error_code"] == "principal_blocked"


# ---------------------------------------------------------------------------
# Another process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_and_unblock_by_name_from_another_process_hold_within_the_cache_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows written by another process count when the cache is five seconds old."""
    await _seed()
    await _note_agent_login()
    clock = [1000.0]
    monkeypatch.setattr(f"{_BLOCK_MODULE}.time.monotonic", lambda: clock[0])
    assert not await is_principal_blocked(_AGENT_SUB, _TENANT_A, client_id=_AGENT)

    async with get_sessionmaker()() as session:
        session.add(
            PrincipalBlock(
                sub=_AGENT,
                scope=BLOCK_SCOPE_ALL,
                reason="written by another process",
                blocked_by_sub="other-admin",
                blocked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    clock[0] += BLOCK_CACHE_SECONDS + 0.5
    assert await is_principal_blocked(_AGENT_SUB, _TENANT_A, client_id=_AGENT)
    assert await is_principal_blocked(_AGENT_SUB, _TENANT_A)
    clock[0] += BLOCK_CACHE_SECONDS + 0.5
    assert await is_principal_blocked(_AGENT_SUB, _TENANT_A)

    async with get_sessionmaker()() as session:
        await session.execute(delete(PrincipalBlock).where(PrincipalBlock.sub == _AGENT))
        await session.commit()

    clock[0] += BLOCK_CACHE_SECONDS + 0.5
    assert not await is_principal_blocked(_AGENT_SUB, _TENANT_A, client_id=_AGENT)
    assert not await is_principal_blocked(_AGENT_SUB, _TENANT_A)


# ---------------------------------------------------------------------------
# Names MEHO does not know
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_of_an_unknown_agent_name_is_refused_with_a_plain_sentence(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    refused = await _block(ac, _person_token(key, "admin-person"), "agent:nobody")

    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "unknown_login: MEHO knows no agent with the login name agent:nobody. "
        "Copy the login name from the agent list."
    )
    assert await _block_rows() == []


@pytest.mark.asyncio
async def test_block_of_the_short_agent_name_names_the_login_name_to_use(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    refused = await _block(ac, _person_token(key, "admin-person"), "probe-bot")

    assert refused.status_code == 422
    assert _AGENT in refused.json()["detail"]
    assert await _block_rows() == []


@pytest.mark.asyncio
async def test_tenant_admin_cannot_block_an_agent_of_another_tenant_but_a_platform_admin_can(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    other = await _block(ac, _person_token(key, "admin-b", tenant=_TENANT_B))
    platform = await _block(
        ac, _person_token(key, "platform-person", tenant=_TENANT_B, platform_admin=True)
    )

    assert other.status_code == 422
    assert platform.status_code == 201, platform.text
    assert platform.json()["scope"] == BLOCK_SCOPE_ALL


@pytest.mark.asyncio
async def test_revoked_agent_can_be_blocked_by_its_name() -> None:
    await _seed(revoked=True)

    blocked = await block_principal(_admin_operator(), _AGENT, "agent revoked")

    assert blocked.agent == _AGENT


@pytest.mark.asyncio
async def test_block_of_a_person_login_id_is_stored_as_before(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    created = await _block(ac, _person_token(key, "admin-person"), "person-to-block")

    assert created.status_code == 201, created.text
    assert created.json()["agent"] is None


# ---------------------------------------------------------------------------
# The list and the agent list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_list_names_the_agent_for_a_block_by_name_and_by_login_id(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    await _note_agent_login()
    admin = _person_token(key, "admin-person")
    assert (await _block(ac, admin)).status_code == 201
    assert (await _block(ac, admin, _AGENT_SUB)).status_code == 201
    assert (await _block(ac, admin, "person-to-block")).status_code == 201

    listed = await ac.get(_ROUTE, headers=_auth(admin))

    assert listed.status_code == 200, listed.text
    agents = {row["sub"]: row["agent"] for row in listed.json()["blocked"]}
    assert agents == {_AGENT: _AGENT, _AGENT_SUB: _AGENT, "person-to-block": None}


@pytest.mark.asyncio
async def test_agent_list_names_the_value_a_block_takes(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    listed = await ac.get("/api/v1/agent-principals", headers=_auth(_person_token(key, "admin")))
    schema = app.openapi()["components"]["schemas"]["AgentPrincipalRead"]

    assert listed.status_code == 200, listed.text
    assert [p["keycloak_client_id"] for p in listed.json()["principals"]] == [_AGENT]
    assert "block" in schema["properties"]["keycloak_client_id"]["description"]


# ---------------------------------------------------------------------------
# machine_login.client_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_login_keeps_the_client_id_and_fills_it_on_a_row_without_one() -> None:
    await _seed()
    await _note_agent_login(client_id=None)
    await _note_agent_login()
    await _note_agent_login(client_id=None)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(MachineLogin))).scalars().all()
    assert [(r.sub, r.client_id) for r in rows] == [(_AGENT_SUB, _AGENT)]
