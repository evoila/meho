# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A block of an agent by its name holds on every check, through the real routes.

Each test uses the real app, signed stand-in tokens, the real block route
``POST /api/v1/principals/blocked``, the real call route
``POST /api/v1/operations/call`` and the real approve route. The Keycloak
stand-in answers the login id behind the agent at block time, or an error.

What these tests check:

* An approved call that the agent asked for does not run after a block of
  its name, also when MEHO has no client id for the agent's login id (as
  right after the upgrade), when MEHO has no row for it at all, and when
  Keycloak cannot be read at block time.
* The same for a step of an approved multi-step run, and for a running call
  of the agent that reaches a send point.
* A token MEHO never saw is refused, and after that a check by its login
  id alone finds the block.
* An operation call of a blocked agent gets 403, by name and by login id.
  After an unblock, the old token reads with 200 and a safe call runs.
* A hosted agent run stops at its next tool call: the check passes the
  agent name.
* A person whose token carries the agent name as client id is not refused.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import delete, select, update

from meho_backplane.agent.principal_block_gate import PrincipalBlockedRunError, _stop_if_blocked
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    block_principal,
    is_principal_blocked,
    reset_block_cache,
)
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, MachineLogin, PrincipalBlock
from meho_backplane.main import app
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from . import _agent_block_paths as p
from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

pytestmark = pytest.mark.asyncio

_ADMIN = "admin-person"
_APPROVER = "approver-person"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", p.KEYCLOAK_ADMIN)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "off")
    for clear in (get_settings.cache_clear, clear_jwks_cache, reset_block_cache, clear_registry):
        clear()
    reset_dispatcher_caches()
    p.clear()
    yield
    for clear in (get_settings.cache_clear, clear_jwks_cache, reset_block_cache, clear_registry):
        clear()
    reset_dispatcher_caches()
    p.clear()


class _World:
    def __init__(self, ac: httpx.AsyncClient, key: Any, router: respx.Router) -> None:
        self.ac = ac
        self.key = key
        self.router = router

    def keycloak(self, *, readable: bool) -> None:
        """The Keycloak stand-in: the login id behind the agent, or an error."""
        self.keycloak_answers(
            httpx.Response(200, json={"id": p.AGENT_SUB})
            if readable
            else httpx.Response(500, json={"error": "unavailable"})
        )

    def keycloak_answers(self, answer: httpx.Response) -> None:
        """The Keycloak stand-in answers *answer* to the read of the agent's login id."""
        self.router.post(p.KEYCLOAK_TOKEN).mock(
            return_value=httpx.Response(200, json={"access_token": "made-up-admin-token"})
        )
        url = f"{p.KEYCLOAK_ADMIN}/clients/{p.AGENT_INTERNAL_ID}/service-account-user"
        self.router.get(url).mock(return_value=answer)

    @property
    def agent(self) -> str:
        return p.agent_token(self.key)

    def person(self, sub: str = _ADMIN) -> str:
        return p.person_token(self.key, sub)


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    key = make_rsa_keypair("kid-agent-paths")
    await p.seed()
    await p.register_ops()
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield _World(ac, key, router)


async def _set_agent_row(row: str) -> None:
    """``no-client-id``: the row as right after the upgrade. ``no-row``: no row at all."""
    async with get_sessionmaker()() as session:
        if row == "no-client-id":
            await session.execute(update(MachineLogin).values(client_id=None))
        else:
            await session.execute(delete(MachineLogin))
        await session.commit()


async def _ask_for_update(w: _World) -> str:
    asked = await p.call(w.ac, w.agent, p.UPDATE_OP)
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "awaiting_approval", asked.text
    return str(asked.json()["extras"]["approval_request_id"])


async def _approve(w: _World, request_id: str, params: dict[str, Any]) -> httpx.Response:
    return await w.ac.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={"params": params},
        headers=p.auth(w.person(_APPROVER)),
    )


async def _resume_audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.path == "approval.resume")
        return list((await session.execute(query)).scalars())


def _admin_operator() -> Operator:
    return Operator(
        sub=_ADMIN,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=p.TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


_CASES = [
    pytest.param("no-client-id", True, id="keycloak-read-row-without-client-id"),
    pytest.param("no-client-id", False, id="keycloak-error-row-without-client-id"),
    pytest.param("no-row", False, id="keycloak-error-no-row"),
    pytest.param("no-row", True, id="keycloak-read-no-row"),
]


@pytest.mark.parametrize(("row", "keycloak_readable"), _CASES)
async def test_approved_call_of_an_agent_blocked_by_name_does_not_run_on_approve(
    world: _World, row: str, keycloak_readable: bool
) -> None:
    w = world
    w.keycloak(readable=keycloak_readable)
    request_id = await _ask_for_update(w)
    await _set_agent_row(row)

    blocked = await p.block(w.ac, w.person())
    assert blocked.status_code == 201, blocked.text
    approved = await _approve(w, request_id, {"name": "one"})

    assert approved.status_code == 200, approved.text
    assert approved.json()["dispatch_status"] == "denied", approved.text
    assert "principal_blocked" in str(approved.json()["dispatch_error"])
    assert [op for op, _params in p.ran] == []
    assert [r.status_code for r in await _resume_audit_rows()] == [409]


async def test_block_stores_the_login_id_behind_the_agent_that_keycloak_answers(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=True)
    assert (await p.block(w.ac, w.person())).status_code == 201

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(PrincipalBlock))).scalars().all()
    assert [(r.sub, r.login_sub) for r in rows] == [(p.AGENT, p.AGENT_SUB)]
    assert await is_principal_blocked(p.AGENT_SUB, p.TENANT_A)


@pytest.mark.parametrize(
    "keycloak_readable", [True, False], ids=["keycloak-read", "keycloak-error"]
)
async def test_step_of_an_approved_run_stops_after_a_block_of_the_agent_name(
    world: _World, keycloak_readable: bool
) -> None:
    w = world
    w.keycloak(readable=keycloak_readable)
    await p.register_steps_op()
    asked = await p.call(w.ac, w.agent, p.STEPS_OP)
    assert asked.json()["status"] == "awaiting_approval", asked.text
    await _set_agent_row("no-client-id")

    async def _block_now() -> None:
        await block_principal(_admin_operator(), p.AGENT, "stop it")

    p.after_first.append(_block_now)
    approved = await _approve(
        w, str(asked.json()["extras"]["approval_request_id"]), {"name": "one"}
    )

    assert approved.status_code == 200, approved.text
    assert "principal_blocked" in str(approved.json()["dispatch_error"]), approved.text
    assert p.ran == [(p.READ_OP, {"name": "1"})]


async def test_running_call_of_an_agent_stops_at_its_next_send_after_a_block_of_its_name(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=False)
    first = await p.call(w.ac, w.agent)
    assert first.status_code == 200, first.text

    async def _block_now() -> None:
        # The call itself noted the client id; the row then has none, as after the upgrade.
        await _set_agent_row("no-client-id")
        await block_principal(_admin_operator(), p.AGENT, "stop it")

    p.after_first.append(_block_now)
    answer = await p.call(w.ac, w.agent, p.SEND_OP)

    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "denied", answer.text
    assert answer.json()["extras"]["error_code"] == "principal_blocked"
    assert p.sent == ["GET /one"]


async def test_token_meho_never_saw_is_refused_and_then_its_login_id_alone_is_blocked(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=False)
    assert (await p.block(w.ac, w.person())).status_code == 201
    assert not await is_principal_blocked(p.AGENT_SUB, p.TENANT_A)

    assert p.is_blocked(await w.ac.get(p.READ_ROUTE, headers=p.auth(w.agent)))

    assert await is_principal_blocked(p.AGENT_SUB, p.TENANT_A)


@pytest.mark.parametrize("text", [p.AGENT, p.AGENT_SUB], ids=["agent-name", "login-id"])
async def test_operation_call_of_a_blocked_agent_gets_403_and_runs_nothing(
    world: _World, text: str
) -> None:
    w = world
    w.keycloak(readable=True)
    assert (await p.block(w.ac, w.person(), text)).status_code == 201

    refused = await p.call(w.ac, w.agent)

    assert p.is_blocked(refused), refused.text
    assert p.ran == []


async def test_unblock_by_name_lets_the_old_token_read_and_run_a_safe_operation(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=True)
    old = w.agent
    assert (await w.ac.get(p.READ_ROUTE, headers=p.auth(old))).status_code == 200
    assert (await p.block(w.ac, w.person())).status_code == 201
    assert p.is_blocked(await w.ac.get(p.READ_ROUTE, headers=p.auth(old)))

    removed = await w.ac.delete(f"{p.BLOCK_ROUTE}/{p.AGENT}", headers=p.auth(w.person()))
    assert removed.status_code == 200, removed.text

    assert (await w.ac.get(p.READ_ROUTE, headers=p.auth(old))).status_code == 200
    ran = await p.call(w.ac, old)
    assert ran.status_code == 200, ran.text
    assert ran.json()["status"] == "ok", ran.text
    assert p.ran == [(p.READ_OP, {"name": "one"})]


async def test_hosted_agent_run_stops_at_its_next_tool_call_after_a_block_of_its_name() -> None:
    await p.seed()
    await block_principal(_admin_operator(), p.AGENT, "stop it")
    agent = Operator(
        sub=p.AGENT_SUB,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=p.TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=p.AGENT,
    )

    with pytest.raises(PrincipalBlockedRunError):
        await _stop_if_blocked(agent, "call_operation")


async def test_person_whose_token_carries_the_agent_name_is_not_refused(world: _World) -> None:
    w = world
    w.keycloak(readable=True)
    assert (await p.block(w.ac, w.person())).status_code == 201
    person = mint_token(
        w.key,
        sub="some-person",
        tenant_id=str(p.TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind=PrincipalKind.USER.value,
        extra_claims={"preferred_username": f"service-account-{p.AGENT}"},
    )
    operator = await verify_jwt(authorization=f"Bearer {person}")
    assert (operator.principal_kind, operator.client_id) == (PrincipalKind.USER, p.AGENT)

    answer = await w.ac.get(p.READ_ROUTE, headers=p.auth(person))

    assert answer.status_code == 200, answer.text
    assert p.is_blocked(await w.ac.get(p.READ_ROUTE, headers=p.auth(w.agent)))


async def test_block_text_is_kept_by_its_agent_tenant() -> None:
    """A block in tenant A does not reach the agent's token that claims tenant B."""
    await p.seed()
    await block_principal(_admin_operator(), p.AGENT, "stop it")

    assert await is_principal_blocked(p.AGENT_SUB, p.TENANT_A, client_id=p.AGENT)
    assert not await is_principal_blocked(p.AGENT_SUB, p.TENANT_B, client_id=p.AGENT)
    assert not await is_principal_blocked(str(uuid.uuid4()), p.TENANT_A)


async def _request_status(request_id: str) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, uuid.UUID(request_id))
    assert row is not None
    return str(row.status)


async def _block_rows() -> list[tuple[str, str | None, str]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(PrincipalBlock))).scalars().all()
    return [(r.sub, r.login_sub, r.reason) for r in rows]


def _assert_refused_and_kept(approved: httpx.Response) -> None:
    assert approved.status_code == 200, approved.text
    assert approved.json()["dispatch_status"] == "denied", approved.text
    assert "principal_blocked" in str(approved.json()["dispatch_error"])
    assert p.ran == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"id": None}),
        httpx.Response(200, content=b""),
        httpx.Response(200, json={"id": ""}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["id-null", "empty-body", "id-empty", "not-an-object"],
)
async def test_unclear_keycloak_answer_stores_the_block_without_a_login_id(
    world: _World, answer: httpx.Response
) -> None:
    w = world
    w.keycloak_answers(answer)
    request_id = await _ask_for_update(w)
    await _set_agent_row("no-client-id")

    blocked = await p.block(w.ac, w.person())

    assert blocked.status_code == 201, blocked.text
    assert await _block_rows() == [(p.AGENT, None, "stop it")]
    _assert_refused_and_kept(await _approve(w, request_id, {"name": "one"}))


async def test_second_block_of_the_name_keeps_the_login_id_when_keycloak_fails(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=True)
    assert (await p.block(w.ac, w.person())).status_code == 201
    w.keycloak(readable=False)

    again = await w.ac.post(
        p.BLOCK_ROUTE, json={"sub": p.AGENT, "reason": "new reason"}, headers=p.auth(w.person())
    )

    assert again.status_code == 201, again.text
    assert await _block_rows() == [(p.AGENT, p.AGENT_SUB, "new reason")]
    reset_block_cache()
    assert await is_principal_blocked(p.AGENT_SUB, p.TENANT_A)


async def test_link_written_after_the_list_read_does_not_let_the_next_approve_run(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=False)
    first = await _ask_for_update(w)
    second = await _ask_for_update(w)
    await _set_agent_row("no-client-id")
    assert (await p.block(w.ac, w.person())).status_code == 201
    _assert_refused_and_kept(await _approve(w, first, {"name": "one"}))

    # Another MEHO process writes the link when it refuses the agent's token by name.
    async with get_sessionmaker()() as session:
        await session.execute(update(MachineLogin).values(client_id=p.AGENT))
        await session.commit()

    _assert_refused_and_kept(await _approve(w, second, {"name": "one"}))


async def test_approved_call_of_an_agent_token_without_a_kind_claim_does_not_run(
    world: _World,
) -> None:
    w = world
    w.keycloak(readable=False)
    token = mint_token(
        w.key,
        sub=p.AGENT_SUB,
        name=None,
        email=None,
        tenant_id=str(p.TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        extra_claims={"azp": p.AGENT, "jti": str(uuid.uuid4())},
    )
    asked = await p.call(w.ac, token, p.UPDATE_OP)
    assert asked.json()["status"] == "awaiting_approval", asked.text
    request_id = str(asked.json()["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        kinds = (await session.execute(select(ApprovalRequest.principal_kind))).scalars().all()
    assert kinds == [PrincipalKind.SERVICE.value]
    await _set_agent_row("no-client-id")
    assert (await p.block(w.ac, w.person())).status_code == 201

    _assert_refused_and_kept(await _approve(w, request_id, {"name": "one"}))
    assert await _request_status(request_id) == "approved"


_SERVICE_SUB = "5b1f7a20-0000-4000-8000-00000000b6e3"


@pytest.mark.parametrize("then", ["unblock", "block-gets-login-id"])
async def test_refused_approved_call_of_another_service_keeps_its_approval(
    world: _World, then: str
) -> None:
    w = world
    w.keycloak(readable=False)
    service = mint_token(
        w.key,
        sub=_SERVICE_SUB,
        name=None,
        email=None,
        tenant_id=str(p.TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind=PrincipalKind.SERVICE.value,
        extra_claims={"azp": "sample-service", "jti": str(uuid.uuid4())},
    )
    asked = await p.call(w.ac, service, p.UPDATE_OP)
    assert asked.json()["status"] == "awaiting_approval", asked.text
    request_id = str(asked.json()["extras"]["approval_request_id"])
    await _set_agent_row("no-client-id")
    assert (await p.block(w.ac, w.person())).status_code == 201

    _assert_refused_and_kept(await _approve(w, request_id, {"name": "one"}))
    assert await _request_status(request_id) == "approved"

    if then == "unblock":
        removed = await w.ac.delete(f"{p.BLOCK_ROUTE}/{p.AGENT}", headers=p.auth(w.person()))
        assert removed.status_code == 200, removed.text
    else:
        w.keycloak(readable=True)
        assert (await p.block(w.ac, w.person())).status_code == 201
    rerun = await w.ac.post(
        f"/api/v1/approvals/{request_id}/rerun", json={}, headers=p.auth(service)
    )

    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["dispatch_status"] == "ok", rerun.text
    assert p.ran == [(p.UPDATE_OP, {"name": "one"})]


async def test_stored_login_id_of_another_login_does_not_end_the_refusal(
    world: _World,
) -> None:
    w = world
    w.keycloak_answers(httpx.Response(200, json={"id": "another-login-id"}))
    request_id = await _ask_for_update(w)
    await _set_agent_row("no-client-id")
    assert (await p.block(w.ac, w.person())).status_code == 201
    assert await _block_rows() == [(p.AGENT, "another-login-id", "stop it")]

    _assert_refused_and_kept(await _approve(w, request_id, {"name": "one"}))
    assert await _request_status(request_id) == "approved"
