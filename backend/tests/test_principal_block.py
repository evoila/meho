# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The admin block list: a blocked login is refused within five seconds.

What these tests check:

* An admin blocks a login. REST, MCP, the console, a direct operation call
  (hosted and scheduled runs) and an approved call whose asker is blocked
  are refused. Unblocking lets them work again.
* Another process sees a block within the cache time (five seconds).
* A block list that cannot be read refuses the call; it never lets it through.
* A tenant admin blocks in their own tenant only. A platform admin blocks
  in every tenant. Nobody blocks their own login.
* Only a person with ``tenant_admin`` blocks or unblocks. A platform admin
  also removes their own block, with a fresh sign-in and a reason. There is
  no MCP tool.
* Each block and each unblock writes one audit row with the admin, the
  login and the reason.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    BLOCK_CACHE_SECONDS,
    BLOCK_SCOPE_ALL,
    PRINCIPAL_BLOCKED_MESSAGE,
    PrincipalBlockUnavailableError,
    block_principal,
    is_principal_blocked,
    reset_block_cache,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    PrincipalBlock,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt
from meho_backplane.mcp.registry import registered_tool_names
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing

from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000b10a")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b10b")
_MCP_AUDIENCE = "https://meho.test/mcp"
_BLOCKED = "person-to-block"
_ROUTE = "/api/v1/principals/blocked"
_WORK_ROUTE = "/api/v1/targets"
_BLOCK_MODULE = "meho_backplane.auth.principal_block"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", _MCP_AUDIENCE)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("PER_ACTION_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("APPROVAL_SIGN_IN_LEVEL", raising=False)
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_jwks_cache()
    reset_block_cache()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_jwks_cache()
    reset_block_cache()


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-principal-block")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _seed_tenants() -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="block-a", name="Block A"))
        session.add(Tenant(id=_TENANT_B, slug="block-b", name="Block B"))
        await session.commit()


def _token(
    key: Any,
    sub: str,
    *,
    tenant: uuid.UUID = _TENANT_A,
    role: TenantRole = TenantRole.OPERATOR,
    platform_admin: bool | None = None,
    principal_kind: str | None = None,
    audience: str | None = None,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(tenant),
        tenant_role=role.value,
        platform_admin=platform_admin,
        principal_kind=principal_kind,
        audience=audience,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _block(
    ac: httpx.AsyncClient, admin_token: str, sub: str = _BLOCKED, reason: str = "left the team"
) -> httpx.Response:
    return await ac.post(_ROUTE, json={"sub": sub, "reason": reason}, headers=_auth(admin_token))


async def _rows(sub: str) -> list[PrincipalBlock]:
    async with get_sessionmaker()() as session:
        query = select(PrincipalBlock).where(PrincipalBlock.sub == sub)
        return list((await session.execute(query)).scalars())


def _admin(key: Any, *, tenant: uuid.UUID = _TENANT_A) -> str:
    return _token(key, "admin-person", tenant=tenant, role=TenantRole.TENANT_ADMIN)


def _platform_admin(key: Any) -> str:
    return _token(key, "platform-person", role=TenantRole.TENANT_ADMIN, platform_admin=True)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_person_is_refused_on_rest_and_works_again_after_unblock(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    person = _token(key, _BLOCKED)
    assert (await ac.get(_WORK_ROUTE, headers=_auth(person))).status_code != 403

    created = await _block(ac, _admin(key))
    assert created.status_code == 201, created.text

    refused = await ac.get(_WORK_ROUTE, headers=_auth(person))
    assert refused.status_code == 403
    assert refused.json()["detail"] == f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"
    # The refusal names no admin and no reason.
    assert "admin-person" not in refused.text
    assert "left the team" not in refused.text

    removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(_admin(key)))
    assert removed.status_code == 200, removed.text
    assert (await ac.get(_WORK_ROUTE, headers=_auth(person))).status_code != 403


@pytest.mark.asyncio
async def test_block_made_by_another_process_is_seen_within_five_seconds(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written by another process shows up when the cache is five seconds old."""
    ac, key = _client
    await _seed_tenants()
    person = _token(key, _BLOCKED)
    clock = [1000.0]
    monkeypatch.setattr(f"{_BLOCK_MODULE}.time.monotonic", lambda: clock[0])
    assert (await ac.get(_WORK_ROUTE, headers=_auth(person))).status_code != 403

    async with get_sessionmaker()() as session:
        session.add(
            PrincipalBlock(
                sub=_BLOCKED,
                scope=BLOCK_SCOPE_ALL,
                reason="written by another process",
                blocked_by_sub="other-admin",
                blocked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    clock[0] += BLOCK_CACHE_SECONDS - 0.5
    assert (await ac.get(_WORK_ROUTE, headers=_auth(person))).status_code != 403
    clock[0] += 1.0
    assert (await ac.get(_WORK_ROUTE, headers=_auth(person))).status_code == 403


@pytest.mark.asyncio
async def test_unreadable_block_list_refuses_the_request(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ac, key = _client
    await _seed_tenants()

    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    monkeypatch.setattr(f"{_BLOCK_MODULE}._load_entries", _broken)
    response = await ac.get(_WORK_ROUTE, headers=_auth(_token(key, "someone")))

    assert response.status_code == 503
    assert response.json()["detail"] == "principal_block_check_unavailable"


@pytest.mark.asyncio
async def test_tenant_admin_block_holds_in_their_tenant_only(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key, tenant=_TENANT_A))).status_code == 201

    (row,) = await _rows(_BLOCKED)
    assert row.scope == str(_TENANT_A)
    in_a = await ac.get(_WORK_ROUTE, headers=_auth(_token(key, _BLOCKED, tenant=_TENANT_A)))
    in_b = await ac.get(_WORK_ROUTE, headers=_auth(_token(key, _BLOCKED, tenant=_TENANT_B)))
    assert in_a.status_code == 403
    assert in_b.status_code != 403
    # The admin of tenant B sees no block and cannot remove it.
    admin_b = _admin(key, tenant=_TENANT_B)
    assert (await ac.get(_ROUTE, headers=_auth(admin_b))).json() == {"blocked": []}
    not_removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(admin_b))
    assert not_removed.status_code == 404
    assert len(await _rows(_BLOCKED)) == 1


def _two_tenant_token(key: Any) -> str:
    """A token of the blocked login that lists tenant A first, then tenant B."""
    entries = [f"{_TENANT_A}:block-a:Readers", f"{_TENANT_B}:block-b:Operators"]
    return mint_token(key, sub=_BLOCKED, extra_claims={"meho_tenants": entries})


@pytest.mark.asyncio
async def test_block_in_the_first_tenant_holds_only_when_that_tenant_is_chosen(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key, tenant=_TENANT_A))).status_code == 201
    bearer = f"Bearer {_two_tenant_token(key)}"

    # Sign-in and status answers pick no tenant: the block in tenant A does not hold.
    unchosen = await verify_jwt_for_audience(
        bearer, expected_audience=AUDIENCE, allow_unchosen=True
    )
    assert unchosen.tenant_chosen is False
    in_b = await verify_jwt_for_audience(
        bearer, expected_audience=AUDIENCE, requested_tenant=str(_TENANT_B)
    )
    assert in_b.tenant_id == _TENANT_B
    with pytest.raises(HTTPException) as refused:
        await verify_jwt_for_audience(
            bearer, expected_audience=AUDIENCE, requested_tenant=str(_TENANT_A)
        )
    assert refused.value.status_code == 403
    assert str(refused.value.detail).startswith("principal_blocked")


@pytest.mark.asyncio
async def test_block_in_every_tenant_holds_before_a_tenant_is_chosen(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _platform_admin(key))).status_code == 201

    with pytest.raises(HTTPException) as refused:
        await verify_jwt_for_audience(
            f"Bearer {_two_tenant_token(key)}", expected_audience=AUDIENCE, allow_unchosen=True
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_platform_admin_block_holds_in_every_tenant(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _platform_admin(key))).status_code == 201

    for tenant in (_TENANT_A, _TENANT_B):
        response = await ac.get(_WORK_ROUTE, headers=_auth(_token(key, _BLOCKED, tenant=tenant)))
        assert response.status_code == 403
    # A tenant admin cannot lift the block of a platform admin.
    admin_a = _admin(key)
    not_removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(admin_a))
    assert not_removed.status_code == 404
    assert len(await _rows(_BLOCKED)) == 1
    # A platform admin lifts every block of the login.
    assert (await _block(ac, admin_a)).status_code == 201
    removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(_platform_admin(key)))
    assert removed.json() == {"sub": _BLOCKED, "removed": 2}
    assert await _rows(_BLOCKED) == []


@pytest.mark.asyncio
async def test_list_shows_blocks_to_the_right_admin(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    await _block(ac, _admin(key, tenant=_TENANT_A), sub="in-a")
    await _block(ac, _admin(key, tenant=_TENANT_B), sub="in-b")

    own = (await ac.get(_ROUTE, headers=_auth(_admin(key, tenant=_TENANT_A)))).json()["blocked"]
    every = (await ac.get(_ROUTE, headers=_auth(_platform_admin(key)))).json()["blocked"]

    assert [row["sub"] for row in own] == ["in-a"]
    assert own[0]["reason"] == "left the team"
    assert own[0]["blocked_by_sub"] == "admin-person"
    assert sorted(row["sub"] for row in every) == ["in-a", "in-b"]


@pytest.mark.asyncio
async def test_nobody_blocks_their_own_login(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    for admin, own_sub in (
        (_admin(key), "admin-person"),
        (_platform_admin(key), "platform-person"),
    ):
        response = await _block(ac, admin, sub=own_sub)
        assert response.status_code == 422, response.text
        assert response.json()["detail"].startswith("cannot_block_self")
    assert await _rows("admin-person") == []
    assert await _rows("platform-person") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "principal_kind", "detail"),
    [
        (TenantRole.OPERATOR, None, "insufficient_role"),
        (TenantRole.READ_ONLY, None, "insufficient_role"),
        (TenantRole.TENANT_ADMIN, "agent", "insufficient_role"),
        (TenantRole.TENANT_ADMIN, "service", "human_principal_required"),
    ],
    ids=["operator", "read-only", "agent", "service"],
)
async def test_only_a_person_with_tenant_admin_blocks_and_unblocks(
    _client: tuple[httpx.AsyncClient, Any],
    role: TenantRole,
    principal_kind: str | None,
    detail: str,
) -> None:
    ac, key = _client
    await _seed_tenants()
    token = _token(key, "not-allowed", role=role, principal_kind=principal_kind)

    blocked = await _block(ac, token)
    unblocked = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(token))
    listed = await ac.get(_ROUTE, headers=_auth(token))

    for response in (blocked, unblocked, listed):
        assert response.status_code == 403, response.text
        assert response.json()["detail"].startswith(detail)
        if principal_kind == "service":
            assert "Use the CLI" in response.json()["detail"]
            assert "console" not in response.json()["detail"]
    assert await _rows(_BLOCKED) == []


@pytest.mark.asyncio
async def test_a_refused_block_call_writes_no_block_and_one_audit_row(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()

    refused = await _block(ac, _admin(key), sub="admin-person")

    assert refused.status_code == 422
    assert await _rows("admin-person") == []
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    block_rows = [r for r in rows if r.payload.get("op_id") == "principal.block"]
    assert len(block_rows) == 1
    assert block_rows[0].status_code == 422
    assert block_rows[0].operator_sub == "admin-person"


@pytest.mark.asyncio
async def test_block_and_unblock_each_write_one_audit_row(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    admin = _admin(key)
    await _block(ac, admin, reason="reason typed by the admin")
    await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(admin))

    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    block_rows = [r for r in rows if r.payload.get("blocked_sub") == _BLOCKED]
    unblock_rows = [r for r in rows if r.payload.get("unblocked_sub") == _BLOCKED]
    assert len(block_rows) == 1
    assert block_rows[0].operator_sub == "admin-person"
    assert block_rows[0].payload["op_id"] == "principal.block"
    assert block_rows[0].payload["reason"] == "reason typed by the admin"
    assert len(unblock_rows) == 1
    assert unblock_rows[0].operator_sub == "admin-person"
    assert unblock_rows[0].payload["op_id"] == "principal.unblock"
    assert unblock_rows[0].payload["removed_blocks"] == [
        {"scope": str(_TENANT_A), "reason": "reason typed by the admin"}
    ]


@pytest.mark.asyncio
async def test_platform_admin_unblock_audit_row_names_each_removed_block_and_reason(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    await _block(ac, _admin(key, tenant=_TENANT_A), reason="reason in tenant a")
    await _block(ac, _admin(key, tenant=_TENANT_B), reason="reason in tenant b")
    await _block(ac, _platform_admin(key), reason="reason everywhere")
    removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(_platform_admin(key)))
    assert removed.json() == {"sub": _BLOCKED, "removed": 3}

    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    (unblock_row,) = [r for r in rows if r.payload.get("unblocked_sub") == _BLOCKED]
    assert unblock_row.operator_sub == "platform-person"
    assert unblock_row.payload["removed_blocks"] == [
        {"scope": "*", "reason": "reason everywhere"},
        {"scope": str(_TENANT_A), "reason": "reason in tenant a"},
        {"scope": str(_TENANT_B), "reason": "reason in tenant b"},
    ]
    assert unblock_row.payload["scopes"] == ["*", str(_TENANT_A), str(_TENANT_B)]


@pytest.mark.asyncio
async def test_block_again_updates_the_reason_and_unblock_of_nobody_is_404(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    admin = _admin(key)
    not_removed = await ac.delete(f"{_ROUTE}/{_BLOCKED}", headers=_auth(admin))
    assert not_removed.status_code == 404
    await _block(ac, admin, reason="first")
    await _block(ac, admin, reason="second")

    (row,) = await _rows(_BLOCKED)
    assert row.reason == "second"


def test_there_is_no_mcp_tool_to_block_or_unblock() -> None:
    names = registered_tool_names()
    assert not [n for n in names if "block" in n]


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_person_is_refused_on_mcp(_client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = _client
    await _seed_tenants()
    token = _token(key, _BLOCKED, audience=_MCP_AUDIENCE)
    operator = await verify_mcp_jwt(f"Bearer {token}", None)
    assert operator.sub == _BLOCKED

    assert (await _block(ac, _admin(key))).status_code == 201

    with pytest.raises(HTTPException) as refused:
        await verify_mcp_jwt(f"Bearer {token}", None)
    assert refused.value.status_code == 403
    assert PRINCIPAL_BLOCKED_MESSAGE in str(refused.value.detail)


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_person_is_refused_on_the_next_console_request() -> None:
    await _seed_tenants()
    console = FastAPI()
    console.add_middleware(UISessionMiddleware)

    @console.get("/ui/ping")
    async def _ping() -> PlainTextResponse:
        return PlainTextResponse("page")

    async with get_sessionmaker()() as session, session.begin():
        decrypted = await create_session(
            session,
            operator_sub=_BLOCKED,
            tenant_id=_TENANT_A,
            access_token="<test>",
            refresh_token="<test>",
            lifetime=timedelta(hours=1),
        )
    headers = {"Cookie": f"{SESSION_COOKIE_NAME}={decrypted.id}"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=console), base_url="https://testserver"
    ) as ac:
        assert (await ac.get("/ui/ping", headers=headers)).text == "page"

        admin = Operator(
            sub="admin-person",
            name=None,
            email=None,
            raw_jwt="<test>",
            tenant_id=_TENANT_A,
            tenant_role=TenantRole.TENANT_ADMIN,
            principal_kind=PrincipalKind.USER,
        )
        await block_principal(admin, _BLOCKED, "left the team")

        refused = await ac.get("/ui/ping", headers=headers)
        assert refused.status_code == 403
        assert refused.text == PRINCIPAL_BLOCKED_MESSAGE


# ---------------------------------------------------------------------------
# Direct calls: hosted runs, scheduled runs, approved calls
# ---------------------------------------------------------------------------


def _operator(sub: str, tenant_id: uuid.UUID = _TENANT_A) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
async def test_dispatch_refuses_a_blocked_operator_before_anything_runs() -> None:
    await _seed_tenants()
    await block_principal(_operator("admin-person"), _BLOCKED, "left the team")

    result = await dispatch(
        operator=_operator(_BLOCKED),
        connector_id="vault-1.x",
        op_id="vault.kv.get",
        target=None,
        params={},
    )
    other_tenant = await dispatch(
        operator=_operator(_BLOCKED, _TENANT_B),
        connector_id="vault-1.x",
        op_id="vault.kv.get",
        target=None,
        params={},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "principal_blocked"
    assert result.error is not None
    assert PRINCIPAL_BLOCKED_MESSAGE in result.error
    # The block of a tenant admin holds in that tenant only.
    assert other_tenant.extras.get("error_code") != "principal_blocked"


@pytest.mark.asyncio
async def test_approved_call_does_not_run_while_the_asker_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenants()
    params = {"path": "app/config", "value": "<test>"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator("asker-person"),
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
    await block_principal(_operator("admin-person"), "asker-person", "left the team")

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    result = await resume_dispatch_after_approval(
        operator=_operator("approver-person"), request=row
    )

    assert calls == []
    assert result.status == "denied"
    assert result.extras["error_code"] == "principal_blocked"
    async with get_sessionmaker()() as session:
        stored = await session.get(ApprovalRequest, request_id)
    assert stored is not None
    assert stored.resumed_at is not None
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "principal_blocked"


# ---------------------------------------------------------------------------
# A platform admin's own way back
# ---------------------------------------------------------------------------

_PA = "platform-person"
_PA_ROLE = "meho-platform-admin"


def _pa_token(
    key: Any,
    *,
    tenant: uuid.UUID = _TENANT_A,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    platform_admin: Any = True,
    extra_claims: dict[str, Any] | None = None,
    fresh_sign_in: bool = True,
) -> str:
    """A token of the person ``_PA``; the platform admin flag is the claim value.

    By default the token comes from a fresh approval sign-in, as the way
    back needs it.
    """
    claims: dict[str, Any] = {"platform_admin": platform_admin}
    claims.update(extra_claims or {})
    return mint_token(
        key,
        sub=_PA,
        tenant_id=str(tenant),
        tenant_role=role.value,
        extra_claims=claims,
        fresh_sign_in=fresh_sign_in,
    )


_WHY = "the block was a mistake"


async def _unblock(
    ac: httpx.AsyncClient,
    token: str,
    sub: str,
    *,
    tenant: uuid.UUID | None = None,
    reason: str | None = _WHY,
) -> httpx.Response:
    headers = _auth(token)
    if tenant is not None:
        headers["Meho-Tenant"] = str(tenant)
    body = None if reason is None else {"reason": reason}
    return await ac.request("DELETE", f"{_ROUTE}/{sub}", json=body, headers=headers)


async def _unblock_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    return [r for r in rows if r.payload.get("op_id") == "principal.unblock"]


@pytest.mark.asyncio
async def test_blocked_platform_admin_removes_own_block_and_works_again(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    token = _pa_token(key, role=TenantRole.READ_ONLY)

    # Every other route of the tenant still refuses the person.
    assert (await ac.get(_WORK_ROUTE, headers=_auth(token))).status_code == 403
    assert (await ac.get(_ROUTE, headers=_auth(token))).status_code == 403

    removed = await _unblock(ac, token, _PA, tenant=_TENANT_A)
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"sub": _PA, "removed": 1}
    assert await _rows(_PA) == []
    after = await ac.get(_WORK_ROUTE, headers=_auth(token))
    assert "principal_blocked" not in after.text
    # The audit row names the person who removed the block, the reason and the sign-in.
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    (row,) = [r for r in rows if r.payload.get("unblocked_sub") == _PA]
    assert row.operator_sub == _PA
    assert row.payload["reason"] == _WHY
    sign_in = row.payload["sign_in"]
    assert sign_in["method"] == "fresh_sign_in"
    assert sign_in["acr"] == "1"
    assert isinstance(sign_in["auth_time"], int)
    assert len(sign_in["sign_in_key"]) == 32


@pytest.mark.asyncio
async def test_way_back_works_on_an_install_with_one_tenant(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="block-a", name="Block A"))
        await session.commit()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    token = _pa_token(key)
    assert (await ac.get(_WORK_ROUTE, headers=_auth(token))).status_code == 403

    assert (await _unblock(ac, token, _PA)).status_code == 200
    assert (await ac.get(_WORK_ROUTE, headers=_auth(token))).status_code != 403


@pytest.mark.asyncio
async def test_platform_admin_under_a_block_in_every_tenant_cannot_unblock(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    other_platform_admin = _token(
        key, "other-platform-person", role=TenantRole.TENANT_ADMIN, platform_admin=True
    )
    assert (await _block(ac, other_platform_admin, sub=_PA)).status_code == 201
    (row,) = await _rows(_PA)
    assert row.scope == BLOCK_SCOPE_ALL

    refused = await _unblock(ac, _pa_token(key), _PA)
    assert refused.status_code == 403
    assert refused.json()["detail"] == f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"
    assert len(await _rows(_PA)) == 1
    # A platform admin who is not blocked removes it.
    assert (await _unblock(ac, other_platform_admin, _PA)).status_code == 200


@pytest.mark.asyncio
async def test_blocked_tenant_admin_cannot_unblock_anyone(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub="admin-person-2")).status_code == 201
    blocked_admin = _token(key, "admin-person-2", role=TenantRole.TENANT_ADMIN)

    own = await _unblock(ac, blocked_admin, "admin-person-2")
    assert own.status_code == 403
    assert own.json()["detail"].startswith("principal_blocked")
    assert (await _block(ac, _admin(key))).status_code == 201
    other = await _unblock(ac, blocked_admin, _BLOCKED)
    assert other.status_code == 403
    assert len(await _rows("admin-person-2")) == 1
    assert len(await _rows(_BLOCKED)) == 1


@pytest.mark.asyncio
async def test_blocked_platform_admin_cannot_remove_another_persons_block(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    assert (await _block(ac, _admin(key))).status_code == 201

    refused = await _unblock(ac, _pa_token(key), _BLOCKED)
    assert refused.status_code == 403
    assert len(await _rows(_BLOCKED)) == 1
    assert len(await _rows(_PA)) == 1

    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    unblock_rows = [r for r in rows if r.payload.get("op_id") == "principal.unblock"]
    assert len(unblock_rows) == 1
    assert unblock_rows[0].status_code == 403
    assert unblock_rows[0].operator_sub == _PA


@pytest.mark.asyncio
async def test_a_token_without_a_valid_platform_admin_flag_has_no_way_back(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201

    for flag in (False, "yes", "nope", [], None):
        token = _pa_token(key, platform_admin=flag)
        response = await _unblock(ac, token, _PA)
        assert response.status_code == 403, flag
    assert len(await _rows(_PA)) == 1


@pytest.mark.asyncio
async def test_platform_admin_from_a_realm_role_has_the_same_way_back(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ac, key = _client
    await _seed_tenants()
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", _PA_ROLE)
    get_settings.cache_clear()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    token = _pa_token(
        key,
        platform_admin=None,
        extra_claims={"realm_access": {"roles": [_PA_ROLE]}},
    )
    assert (await ac.get(_WORK_ROUTE, headers=_auth(token))).status_code == 403
    assert (await _unblock(ac, token, _PA)).status_code == 200
    assert await _rows(_PA) == []


@pytest.mark.asyncio
async def test_unreadable_block_list_gives_a_platform_admin_no_way_through(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    reset_block_cache()

    async def _broken() -> Any:
        raise PrincipalBlockUnavailableError

    monkeypatch.setattr(f"{_BLOCK_MODULE}._load_entries", _broken)
    response = await _unblock(ac, _pa_token(key), _PA)
    assert response.status_code == 503
    assert response.json()["detail"] == "principal_block_check_unavailable"


@pytest.mark.asyncio
async def test_way_back_removes_only_the_block_of_the_current_tenant(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key, tenant=_TENANT_A), sub=_PA)).status_code == 201
    assert (await _block(ac, _admin(key, tenant=_TENANT_B), sub=_PA)).status_code == 201

    removed = await _unblock(ac, _pa_token(key), _PA, tenant=_TENANT_A)

    assert removed.status_code == 200, removed.text
    assert removed.json() == {"sub": _PA, "removed": 1}
    (row,) = await _rows(_PA)
    assert row.scope == str(_TENANT_B)


@pytest.mark.asyncio
async def test_way_back_reads_the_table_and_a_new_block_in_every_tenant_stays(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    # This process holds the list with the tenant block only.
    assert await is_principal_blocked(_PA, None) is False
    # Another process adds a block in every tenant; the cached list does not show it yet.
    async with get_sessionmaker()() as session:
        session.add(
            PrincipalBlock(
                sub=_PA,
                scope=BLOCK_SCOPE_ALL,
                tenant_id=None,
                reason="blocked everywhere",
                blocked_by_sub="other-platform-person",
                blocked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    refused = await _unblock(ac, _pa_token(key), _PA, tenant=_TENANT_A)

    assert refused.status_code == 403
    assert refused.json()["detail"] == f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"
    assert sorted(row.scope for row in await _rows(_PA)) == sorted(
        [BLOCK_SCOPE_ALL, str(_TENANT_A)]
    )


@pytest.mark.asyncio
async def test_unblock_answers_503_when_the_table_read_fails_with_the_list_cached(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ac, key = _client
    await _seed_tenants()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    assert (await _block(ac, _admin(key))).status_code == 201
    # The cached list lets sign-in pass without a read of the table.
    assert await is_principal_blocked(_PA, _TENANT_A) is True

    real_execute = AsyncSession.execute

    async def _execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(statement, "is_select", False) and "principal_block" in str(statement):
            raise OperationalError(str(statement), {}, Exception("database is gone"))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _execute)

    for token, sub in ((_pa_token(key), _PA), (_admin(key), _BLOCKED)):
        response = await _unblock(ac, token, sub, tenant=_TENANT_A)
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "principal_block_check_unavailable"
    monkeypatch.undo()
    assert len(await _rows(_PA)) == 1
    assert len(await _rows(_BLOCKED)) == 1


# ---------------------------------------------------------------------------
# Who may unblock, and what the way back needs
# ---------------------------------------------------------------------------


async def _seed_one_tenant() -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="block-a", name="Block A"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.OPERATOR])
async def test_platform_admin_below_tenant_admin_cannot_remove_another_persons_block(
    _client: tuple[httpx.AsyncClient, Any], role: TenantRole
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key))).status_code == 201
    platform_reader = _token(key, "platform-reader", role=role, platform_admin=True)

    refused = await _unblock(ac, platform_reader, _BLOCKED, reason=None)

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "insufficient_role"
    assert len(await _rows(_BLOCKED)) == 1


@pytest.mark.asyncio
async def test_own_unblock_with_a_login_token_asks_for_a_fresh_sign_in(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201

    refused = await _unblock(ac, _pa_token(key, fresh_sign_in=False), _PA)

    assert refused.status_code == 401, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("fresh_sign_in_required: ")
    assert "meho principal unblock <sub> --reason <text>" in detail
    assert "approvals approve" not in detail
    challenge = refused.headers["WWW-Authenticate"]
    assert 'error="insufficient_user_authentication"' in challenge
    assert "max_age=900" in challenge
    assert len(await _rows(_PA)) == 1
    (row,) = await _unblock_rows()
    assert row.status_code == 401
    assert row.operator_sub == _PA


_OWN_REASON_TEXT = (
    "unblock_reason_required: Removing a block on your own login needs a reason: "
    "1 to 500 characters, with at least one character that is not a blank. "
    "Add --reason <text>."
)
_OTHER_REASON_TEXT = (
    "unblock_reason_required: A reason has 1 to 500 characters, with at least one "
    "character that is not a blank. Leave out --reason, or fix the text."
)
_BLANK_REASONS = [" ", "     ", "\t", "\n", " \t\r\n "]
_BLANK_IDS = ["space", "spaces", "tab", "line-break", "mixed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [None, "", "x" * 501, *_BLANK_REASONS],
    ids=["none", "empty", "too-long", *_BLANK_IDS],
)
async def test_own_unblock_needs_a_reason(
    _client: tuple[httpx.AsyncClient, Any], reason: str | None
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201

    refused = await _unblock(ac, _pa_token(key), _PA, reason=reason)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == _OWN_REASON_TEXT
    assert len(await _rows(_PA)) == 1
    (row,) = await _unblock_rows()
    assert row.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["", "x" * 501, *_BLANK_REASONS],
    ids=["empty", "too-long", *_BLANK_IDS],
)
async def test_unblock_of_another_login_refuses_a_reason_without_text(
    _client: tuple[httpx.AsyncClient, Any], reason: str
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key))).status_code == 201

    refused = await _unblock(ac, _admin(key), _BLOCKED, reason=reason)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == _OTHER_REASON_TEXT
    assert len(await _rows(_BLOCKED)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", _BLANK_REASONS, ids=_BLANK_IDS)
async def test_block_with_a_reason_of_only_blanks_writes_no_block(
    _client: tuple[httpx.AsyncClient, Any], reason: str
) -> None:
    ac, key = _client
    await _seed_one_tenant()

    refused = await _block(ac, _admin(key), reason=reason)

    assert refused.status_code == 422, refused.text
    assert await _rows(_BLOCKED) == []


@pytest.mark.asyncio
async def test_a_reason_with_blanks_around_a_word_is_stored_as_sent(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    reason = "  planned  maintenance \n"

    assert (await _block(ac, _admin(key), reason=reason)).status_code == 201
    assert [row.reason for row in await _rows(_BLOCKED)] == [reason]

    removed = await _unblock(ac, _admin(key), _BLOCKED, reason=reason)
    assert removed.status_code == 200, removed.text


@pytest.mark.asyncio
async def test_own_unblock_with_an_approval_sign_in_older_than_the_window_is_refused(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key), sub=_PA)).status_code == 201
    old = _pa_token(key, extra_claims={"auth_time": int(time.time()) - 900 - 60})

    refused = await _unblock(ac, old, _PA)

    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"].startswith("fresh_sign_in_required: ")
    assert len(await _rows(_PA)) == 1


@pytest.mark.asyncio
async def test_admins_remove_another_persons_block_with_a_login_token_and_no_reason(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_one_tenant()
    assert (await _block(ac, _admin(key))).status_code == 201
    assert (await _unblock(ac, _admin(key), _BLOCKED, reason=None)).status_code == 200
    assert await _rows(_BLOCKED) == []

    assert (await _block(ac, _admin(key))).status_code == 201
    platform_admin = _pa_token(key, fresh_sign_in=False)
    removed = await _unblock(ac, platform_admin, _BLOCKED, reason="sent by the admin")
    assert removed.status_code == 200, removed.text
    assert await _rows(_BLOCKED) == []
    rows = [r for r in await _unblock_rows() if r.payload.get("unblocked_sub") == _BLOCKED]
    assert [r.payload.get("reason") for r in rows] == [None, "sent by the admin"]
    assert all("sign_in" not in r.payload for r in rows)
