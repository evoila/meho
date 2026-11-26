# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One fresh sign-in is used once, across the own-login unblock and an approve.

A sign-in that removed the person's own block cannot approve a destructive
request. A sign-in that already approved a request cannot remove the
person's own block. Both refusals are the fresh sign-in challenge, so the
person signs in again. Another person's unblock spends no sign-in.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from meho_backplane.auth import principal_block
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    PrincipalBlock,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.operations import approval_queue
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c10a")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000c10b")
_PERSON = "platform-person"
_OTHER = "other-person"
_PARAMS: dict[str, Any] = {"disk_gb": 40}
_WHY = "the block was a mistake"
_ROUTE = "/api/v1/principals/blocked"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in (
        "FRESH_SIGN_IN_MAX_AGE_SECONDS",
        "PER_ACTION_SIGN_IN_MAX_AGE_SECONDS",
        "KEYCLOAK_CLI_APPROVE_CLIENT_ID",
        "APPROVAL_SIGN_IN_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_block_cache()
    _SIGN_IN_TIMES.clear()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_block_cache()


@pytest.fixture(autouse=True)
def _fake_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the re-dispatch after an approve."""
    from meho_backplane.api.v1 import approvals as approvals_module

    async def _resume(*, operator: Operator, request: ApprovalRequest, params: Any) -> Any:
        return SimpleNamespace(status="ok", op_id=request.op_id, result=None, error=None)

    monkeypatch.setattr(approvals_module, "_resume_dispatch_after_approval", _resume)


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-sign-in-one-use")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


#: The ``auth_time`` of each sign-in of the running test, so one *sid* is one sign-in.
_SIGN_IN_TIMES: dict[str, int] = {}


def _sign_in(key: Any, sid: str, *, sub: str = _PERSON) -> str:
    """A token of one sign-in: the same *sid* is the same sign-in."""
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(_TENANT),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        fresh_sign_in=True,
        extra_claims={
            "platform_admin": True,
            "sid": sid,
            "auth_time": _SIGN_IN_TIMES.setdefault(sid, int(time.time())),
        },
    )


async def _seed_block(sub: str = _PERSON) -> None:
    """Block *sub* in the tenant."""
    async with get_sessionmaker()() as session:
        await _ensure_tenant(session, _TENANT)
        session.add(
            PrincipalBlock(
                sub=sub,
                scope=str(_TENANT),
                reason="left the team",
                blocked_by_sub="admin-person",
                blocked_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _blocked(sub: str = _PERSON) -> bool:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(PrincipalBlock).where(PrincipalBlock.sub == sub))
        return rows.first() is not None


async def _ensure_tenant(session: Any, tenant: uuid.UUID) -> None:
    if await session.get(Tenant, tenant) is None:
        session.add(Tenant(id=tenant, slug=f"one-use-{tenant.hex[-4:]}", name="One use"))
        await session.flush()


async def _park(*, destructive: bool, tenant: uuid.UUID = _TENANT) -> uuid.UUID:
    requester = Operator(
        sub="op-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    async with get_sessionmaker()() as session:
        await _ensure_tenant(session, tenant)
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="vmware-rest-9.0",
            op_id="vm.create",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={
                "op_id": "vm.create",
                "safety_level": "destructive" if destructive else "caution",
            },
            preview_hash="preview-hash" if destructive else None,
        )
        await session.commit()
        return request.id


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


async def _approve(
    ac: httpx.AsyncClient, request_id: uuid.UUID, token: str, tenant: uuid.UUID | None = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers["Meho-Tenant"] = str(tenant)
    return await ac.post(
        f"/api/v1/approvals/{request_id}/decide",
        json={"decision": "approved", "reason": ""},
        headers=headers,
    )


async def _unblock(ac: httpx.AsyncClient, token: str, sub: str = _PERSON) -> httpx.Response:
    return await ac.request(
        "DELETE",
        f"{_ROUTE}/{sub}",
        json={"reason": _WHY},
        headers={"Authorization": f"Bearer {token}", "Meho-Tenant": str(_TENANT)},
    )


def _assert_used(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.json()["detail"].startswith("fresh_sign_in_already_used: ")
    assert "insufficient_user_authentication" in response.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_a_sign_in_that_removed_the_own_block_cannot_approve_a_destructive_request(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_block()
    destructive = await _park(destructive=True)

    assert (await _unblock(ac, _sign_in(key, "s-unblock-first"))).status_code == 200
    refused = await _approve(ac, destructive, _sign_in(key, "s-unblock-first"))

    _assert_used(refused)
    assert await _status(destructive) == ApprovalRequestStatus.PENDING.value
    # A second sign-in works for the second step.
    assert (await _approve(ac, destructive, _sign_in(key, "s-second"))).status_code == 200
    assert await _status(destructive) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_sign_in_that_approved_a_destructive_request_cannot_remove_the_own_block(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    destructive = await _park(destructive=True)
    assert (await _approve(ac, destructive, _sign_in(key, "s-approve-first"))).status_code == 200
    await _seed_block()

    refused = await _unblock(ac, _sign_in(key, "s-approve-first"))

    _assert_used(refused)
    assert await _blocked()
    # A second sign-in works for the second step.
    assert (await _unblock(ac, _sign_in(key, "s-second"))).status_code == 200
    assert not await _blocked()


@pytest.mark.asyncio
async def test_a_sign_in_that_approved_a_plain_request_cannot_remove_the_own_block(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    plain = await _park(destructive=False)
    assert (await _approve(ac, plain, _sign_in(key, "s-plain"))).status_code == 200
    await _seed_block()

    _assert_used(await _unblock(ac, _sign_in(key, "s-plain")))
    assert await _blocked()


@pytest.mark.asyncio
async def test_a_sign_in_that_removed_the_own_block_still_approves_a_plain_request(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_block()
    plain = await _park(destructive=False)

    assert (await _unblock(ac, _sign_in(key, "s-mixed"))).status_code == 200

    assert (await _approve(ac, plain, _sign_in(key, "s-mixed"))).status_code == 200


@pytest.mark.asyncio
async def test_removing_the_block_of_another_person_does_not_use_up_a_sign_in(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_block(_OTHER)
    destructive = await _park(destructive=True)

    assert (await _unblock(ac, _sign_in(key, "s-other"), _OTHER)).status_code == 200

    assert (await _approve(ac, destructive, _sign_in(key, "s-other"))).status_code == 200


@pytest.mark.asyncio
async def test_a_sign_in_that_removed_the_own_block_in_one_tenant_cannot_approve_in_another(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_block()
    destructive = await _park(destructive=True, tenant=_TENANT_B)

    assert (await _unblock(ac, _sign_in(key, "s-tenants-unblock"))).status_code == 200
    refused = await _approve(ac, destructive, _sign_in(key, "s-tenants-unblock"), _TENANT_B)

    _assert_used(refused)
    assert await _status(destructive) == ApprovalRequestStatus.PENDING.value
    assert not await _blocked()


@pytest.mark.asyncio
async def test_a_sign_in_that_approved_in_one_tenant_cannot_remove_the_own_block_in_another(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_block()
    destructive = await _park(destructive=True, tenant=_TENANT_B)

    approved = await _approve(ac, destructive, _sign_in(key, "s-tenants-approve"), _TENANT_B)
    assert approved.status_code == 200, approved.text
    refused = await _unblock(ac, _sign_in(key, "s-tenants-approve"))

    _assert_used(refused)
    assert await _blocked()
    assert await _status(destructive) == ApprovalRequestStatus.APPROVED.value


async def _unblock_rows() -> list[AuditLog]:
    """The ``principal.unblock`` rows that name a removed login (not the request records)."""
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [r for r in rows if "unblocked_sub" in (r.payload or {})]


@pytest.mark.parametrize("step", ["lock", "scan"])
@pytest.mark.asyncio
async def test_a_database_error_in_the_sign_in_check_answers_503_and_keeps_the_block(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    ac, key = _client
    await _seed_block()

    async def _fail(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("SELECT 1", {}, ConnectionError("database went away"))

    if step == "lock":
        monkeypatch.setattr(approval_queue, "take_approval_lock", _fail)
    else:
        monkeypatch.setattr(principal_block, "earlier_sign_in_uses", _fail)

    response = await _unblock(ac, _sign_in(key, "s-database-down"))

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "principal_block_check_unavailable"
    assert await _blocked()
    assert await _unblock_rows() == []
