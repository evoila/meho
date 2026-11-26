# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a block text names: an agent name (``agent:<name>``) or a login id.

What these tests check:

* An admin blocks ``agent:<name>``. The agent's token is refused on REST
  and on MCP, also a new token and a token MEHO never saw before. An
  unblock by the name lets the token read with 200 again.
* A text that starts with ``agent:`` and names no agent in the admin's
  reach is refused with a plain sentence, and no block is stored.
* Every other text is a login id, and MEHO stores its block: also when it
  is the short name of an agent, in any tenant. The answer says in one
  sentence that MEHO refuses the login within a few seconds on every
  MEHO process.
* A block of a login id stops only the login with that id: a service
  login whose client id is that text is not refused.
* Another process sees the block and the unblock within the refresh time
  of the block list.
* The block answer and the list name the agent, also for a block of the
  agent's login id.
* The ``unknown_login`` answer matches the published 422 schema: a list of
  field errors whose ``msg`` is the plain sentence.
* MEHO keeps the client id of a machine login (``machine_login.client_id``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jsonschema
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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, MachineLogin, PrincipalBlock, Tenant
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt
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
_AGENT = "agent:sample-agent"
_AGENT_SUB = "5b1c9d1e-0000-4000-8000-00000000a6e1"
_ROUTE = "/api/v1/principals/blocked"
_READ_ROUTE = "/api/v1/connectors"
_BLOCK_MODULE = "meho_backplane.auth.principal_block"
_LOGIN_STORED = (
    "The block is stored. MEHO refuses this login within a few seconds on every MEHO process."
)


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
        session.add(_agent_row(_TENANT_A, "sample-agent", revoked=revoked))
        await session.commit()


def _agent_row(tenant: uuid.UUID, name: str, *, revoked: bool = False) -> AgentPrincipal:
    return AgentPrincipal(
        tenant_id=tenant,
        name=name,
        keycloak_client_id=f"agent:{name}",
        keycloak_internal_id=f"{name}-internal-id",
        owner_sub="owner-person",
        created_by_sub="owner-person",
        revoked=revoked,
    )


def _agent_token(key: Any, *, audience: str | None = None) -> str:
    """A client-credentials token of the agent: the client id is in ``azp`` only."""
    return _machine_token(key, _AGENT_SUB, _AGENT, kind="agent", audience=audience)


def _machine_token(
    key: Any, sub: str, client_id: str, *, kind: str, audience: str | None = None
) -> str:
    return mint_token(
        key,
        sub=sub,
        name=None,
        email=None,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind=kind,
        audience=audience,
        extra_claims={"azp": client_id, "jti": str(uuid.uuid4())},
    )


def _person_token(
    key: Any,
    sub: str,
    *,
    tenant: uuid.UUID = _TENANT_A,
    platform_admin: bool | None = None,
    role: TenantRole = TenantRole.TENANT_ADMIN,
) -> str:
    return mint_token(
        key, sub=sub, tenant_id=str(tenant), tenant_role=role.value, platform_admin=platform_admin
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _blocked(response: httpx.Response) -> bool:
    return response.status_code == 403 and "principal_blocked" in response.text


async def _block(ac: httpx.AsyncClient, admin: str, sub: str = _AGENT) -> httpx.Response:
    return await ac.post(_ROUTE, json={"sub": sub, "reason": "agent revoked"}, headers=_auth(admin))


async def _read(ac: httpx.AsyncClient, token: str) -> httpx.Response:
    return await ac.get(_READ_ROUTE, headers=_auth(token))


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


def _machine_operator(sub: str, client_id: str | None, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=client_id,
    )


async def _note_login(
    sub: str = _AGENT_SUB, client_id: str | None = _AGENT, kind: PrincipalKind = PrincipalKind.AGENT
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await note_machine_login(session, _machine_operator(sub, client_id, kind))


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
    before = await _read(ac, old)
    assert before.status_code == 200, before.text

    created = await _block(ac, admin)
    assert created.status_code == 201, created.text
    assert created.json()["sub"] == _AGENT
    assert created.json()["agent"] == _AGENT

    refused = await _read(ac, old)
    assert _blocked(refused)
    assert refused.json()["detail"] == f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"
    assert PRINCIPAL_BLOCKED_MESSAGE.startswith("This login is blocked")
    new = await _read(ac, _agent_token(key))
    assert _blocked(new)
    with pytest.raises(HTTPException) as on_mcp:
        await verify_mcp_jwt(f"Bearer {_agent_token(key, audience=_MCP_AUDIENCE)}", None)
    assert on_mcp.value.status_code == 403
    assert PRINCIPAL_BLOCKED_MESSAGE in str(on_mcp.value.detail)

    removed = await ac.delete(f"{_ROUTE}/{_AGENT}", headers=_auth(admin))
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"sub": _AGENT, "removed": 1}
    after = await _read(ac, old)
    assert after.status_code == 200, after.text


@pytest.mark.asyncio
async def test_agent_token_that_meho_never_saw_is_refused_by_the_agent_name(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    created = await _block(ac, _person_token(key, "admin-person"))
    assert created.status_code == 201, created.text

    refused = await _read(ac, _agent_token(key))

    assert _blocked(refused)


@pytest.mark.asyncio
async def test_block_by_login_id_refuses_the_agent_token(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    admin = _person_token(key, "admin-person")
    first = await _read(ac, _agent_token(key))
    assert first.status_code == 200, first.text

    created = await _block(ac, admin, _AGENT_SUB)
    assert created.status_code == 201, created.text
    # MEHO saw the login id with the agent's client id, so the answer names the agent.
    assert created.json()["agent"] == _AGENT
    refused = await _read(ac, _agent_token(key))
    assert _blocked(refused)

    removed = await ac.delete(f"{_ROUTE}/{_AGENT_SUB}", headers=_auth(admin))
    assert removed.status_code == 200, removed.text
    after = await _read(ac, _agent_token(key))
    assert after.status_code == 200, after.text


# ---------------------------------------------------------------------------
# A login id stops only that login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_of_a_person_login_id_does_not_stop_a_service_whose_client_id_is_that_text(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    person, service_sub = "svc-person-login", "1f00c3a2-0000-4000-8000-00000000a6e3"
    await _note_login(service_sub, person, PrincipalKind.SERVICE)
    created = await _block(ac, _person_token(key, "admin-person"), person)
    assert created.status_code == 201, created.text

    service = await _read(ac, _machine_token(key, service_sub, person, kind="service"))
    refused = await _read(ac, _person_token(key, person, role=TenantRole.OPERATOR))

    assert service.status_code == 200, service.text
    assert not await is_principal_blocked(service_sub, _TENANT_A)
    assert _blocked(refused)


@pytest.mark.asyncio
async def test_block_text_without_the_agent_prefix_does_not_stop_a_service_with_that_client_id(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    created = await _block(ac, _person_token(key, "admin-person"), "service-x")
    assert created.status_code == 201, created.text

    service = await _read(ac, _machine_token(key, str(uuid.uuid4()), "service-x", kind="service"))

    assert service.status_code == 200, service.text


# ---------------------------------------------------------------------------
# Another process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_and_unblock_by_name_from_another_process_hold_within_the_refresh_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows written by another process count once the list copy is older than its refresh time."""
    await _seed()
    await _note_login()
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
# Agent names MEHO does not know, and login ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["agent:nobody", "agent:", "agent:sample-agent "])
async def test_block_of_an_unknown_agent_name_is_refused_with_a_plain_sentence(
    _client: tuple[httpx.AsyncClient, Any], text: str
) -> None:
    ac, key = _client
    await _seed()

    refused = await _block(ac, _person_token(key, "admin-person"), text)

    assert refused.status_code == 422
    assert refused.json()["detail"] == [
        {
            "type": "unknown_login",
            "loc": ["body", "sub"],
            "msg": f"MEHO knows no agent with the login name {text}. "
            "Copy the login name from the agent list.",
        }
    ]
    assert await _block_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["sample-agent", "nobody-at-all", "AGENT:sample-agent", " agent:sample-agent", "runner:nobody"],
)
async def test_text_without_the_agent_prefix_is_stored_as_a_login_id_with_the_sentence(
    _client: tuple[httpx.AsyncClient, Any], text: str
) -> None:
    ac, key = _client
    await _seed()

    created = await _block(ac, _person_token(key, "admin-person"), text)

    assert created.status_code == 201, created.text
    assert created.json()["message"] == _LOGIN_STORED
    assert created.json()["agent"] is None
    assert await _block_rows() == [text]


@pytest.mark.asyncio
async def test_block_of_a_login_id_that_made_no_request_holds_from_its_first_request(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    created = await _block(ac, _person_token(key, "admin-person"), "new-person")
    first = await _read(ac, _person_token(key, "new-person", role=TenantRole.OPERATOR))

    assert created.status_code == 201, created.text
    assert created.json()["message"] == _LOGIN_STORED
    assert _blocked(first)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin",
    [
        pytest.param({"tenant": _TENANT_A, "platform_admin": True}, id="platform-admin"),
        pytest.param({"tenant": _TENANT_B}, id="tenant-admin-of-the-agent-tenant"),
    ],
)
async def test_login_id_equal_to_the_short_name_of_a_revoked_agent_of_another_tenant_is_blocked(
    _client: tuple[httpx.AsyncClient, Any], admin: dict[str, Any]
) -> None:
    ac, key = _client
    await _seed()
    shared = "shared-login-text"
    async with get_sessionmaker()() as session:
        session.add(_agent_row(_TENANT_B, shared, revoked=True))
        await session.commit()

    created = await _block(ac, _person_token(key, "admin-person", **admin), shared)
    person = _person_token(key, shared, tenant=admin["tenant"], role=TenantRole.OPERATOR)
    refused = await _read(ac, person)

    assert created.status_code == 201, created.text
    assert await _block_rows() == [shared]
    assert _blocked(refused)


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
async def test_block_of_a_person_login_id_is_stored(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()

    created = await _block(ac, _person_token(key, "admin-person"), "person-to-block")

    assert created.status_code == 201, created.text
    assert created.json()["agent"] is None


# ---------------------------------------------------------------------------
# The answer schema
# ---------------------------------------------------------------------------


def _schema_of_422() -> dict[str, Any]:
    spec = app.openapi()
    answer = spec["paths"][_ROUTE]["post"]["responses"]["422"]
    schema = answer["content"]["application/json"]["schema"]
    return {**schema, "components": spec["components"]}


@pytest.mark.asyncio
async def test_unknown_login_answer_matches_the_published_422_schema(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    admin = _person_token(key, "admin-person")
    schema = _schema_of_422()

    unknown = await _block(ac, admin, "agent:nobody")
    not_valid = await ac.post(_ROUTE, json={"sub": "", "reason": "x"}, headers=_auth(admin))

    assert unknown.status_code == 422
    assert not_valid.status_code == 422
    assert unknown.json()["detail"][0]["type"] == "unknown_login"
    assert schema["$ref"] == "#/components/schemas/HTTPValidationError"
    jsonschema.validate(unknown.json(), schema)
    jsonschema.validate(not_valid.json(), schema)


# ---------------------------------------------------------------------------
# The list and the agent list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_list_names_the_agent_for_a_block_by_name_and_by_login_id(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed()
    await _note_login()
    admin = _person_token(key, "admin-person")
    for text in (_AGENT, _AGENT_SUB, "person-to-block"):
        created = await _block(ac, admin, text)
        assert created.status_code == 201, created.text

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
    assert schema["properties"]["keycloak_client_id"]["description"] == (
        "A block of this name (POST /api/v1/principals/blocked) stops every token of the agent."
    )


# ---------------------------------------------------------------------------
# machine_login.client_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_login_keeps_the_client_id_and_fills_it_on_a_row_without_one() -> None:
    await _seed()
    await _note_login(client_id=None)
    await _note_login()
    await _note_login(client_id=None)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(MachineLogin))).scalars().all()
    assert [(r.sub, r.client_id) for r in rows] == [(_AGENT_SUB, _AGENT)]
