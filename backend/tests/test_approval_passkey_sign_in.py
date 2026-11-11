# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A passkey sign-in counts as a fresh sign-in for an approval (#6006).

A passkey (WebAuthn: Face ID, Touch ID, a fingerprint or Windows Hello)
signs a person in without the password form. Keycloak still puts the
sign-in time in ``auth_time`` and gives ``acr`` ``"1"``: with no
level-of-authentication mapping, its ``AcrProtocolMapper`` gives ``"1"`` to
every sign-in that was not made from the session cookie. A realm that adds
Keycloak's AMR mapper also names the method in ``amr``; MEHO does not read
that claim. So the approval checks need no change: these tests pin that a
passkey sign-in approves on REST and in the web interface, and that an old
passkey sign-in does not.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi import HTTPException
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.approval_sign_in import check_fresh_web_approval_sign_in
from meho_backplane.auth.jwt import _extract_sign_in_claims, clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import APPROVAL_CLIENT_ID as _APPROVAL_CLIENT
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_PARAMS: dict[str, Any] = {"disk_gb": 40}

#: The ``amr`` of a passkey sign-in when the realm has the AMR mapper:
#: "hwk" (a key kept in hardware) and "user" (the device checked the
#: person), from RFC 8176. ``None``: the realm has no AMR mapper, as the
#: chart's realm.
_PASSKEY_AMR = pytest.mark.parametrize("amr", [None, ["hwk", "user"]], ids=["no-amr", "amr"])


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("PER_ACTION_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("APPROVAL_ONE_TIME_CODE_ACR_VALUES", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def _resumed(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Replace the run after approval; record which requests ran."""
    from meho_backplane.api.v1 import approvals as approvals_module

    resumed: list[uuid.UUID] = []

    async def _resume(*, operator: Operator, request: ApprovalRequest, params: Any) -> Any:
        resumed.append(request.id)
        return SimpleNamespace(status="ok", op_id=request.op_id, result=None, error=None)

    monkeypatch.setattr(approvals_module, "_resume_dispatch_after_approval", _resume)
    return resumed


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-passkey-sign-in")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _park() -> uuid.UUID:
    """Park one request by another person and return its id."""
    requester = Operator(
        sub="op-passkey-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="vmware-rest-9.0",
            op_id="vm.create",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
        )
        await session.commit()
        return request.id


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


async def _approval_auth(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog.payload).where(AuditLog.path == "approval.decision")
        )
        for (payload,) in rows:
            if payload.get("approval_request_id") == str(request_id):
                return payload.get("approval_auth")
    raise AssertionError("no approval.decision audit row")


def _passkey_claims(*, age: int, amr: list[str] | None) -> dict[str, Any]:
    """The sign-in claims of a passkey sign-in *age* seconds ago."""
    claims: dict[str, Any] = {
        "azp": _APPROVAL_CLIENT,
        "acr": "1",
        "auth_time": int(time.time()) - age,
        "sid": f"s-passkey-{age}",
    }
    if amr is not None:
        claims["amr"] = amr
    return claims


def _passkey_token(key: Any, *, age: int, amr: list[str] | None) -> str:
    return mint_token(
        key,
        sub="op-passkey-approver",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
        extra_claims=_passkey_claims(age=age, amr=amr),
    )


async def _approve(ac: httpx.AsyncClient, request_id: uuid.UUID, token: str) -> Any:
    return await ac.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={"params": _PARAMS},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
@_PASSKEY_AMR
async def test_a_recent_passkey_sign_in_approves_on_rest(
    amr: list[str] | None,
    _client: tuple[httpx.AsyncClient, Any],
    _resumed: list[uuid.UUID],
) -> None:
    ac, key = _client
    request_id = await _park()

    response = await _approve(ac, request_id, _passkey_token(key, age=10, amr=amr))

    assert response.status_code == 200, response.text
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert _resumed == [request_id]
    recorded = await _approval_auth(request_id)
    assert recorded["channel"] == "rest"
    assert recorded["method"] == "fresh_sign_in"
    assert recorded["acr"] == "1"


@pytest.mark.asyncio
async def test_a_passkey_sign_in_is_not_recorded_as_a_one_time_code(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _resumed: list[uuid.UUID],
) -> None:
    """With a code ``acr`` set, a passkey sign-in still counts as a fresh sign-in."""
    monkeypatch.setenv("APPROVAL_ONE_TIME_CODE_ACR_VALUES", "otp-step")
    get_settings.cache_clear()
    ac, key = _client
    request_id = await _park()

    response = await _approve(ac, request_id, _passkey_token(key, age=10, amr=["hwk", "user"]))

    assert response.status_code == 200, response.text
    assert (await _approval_auth(request_id))["method"] == "fresh_sign_in"


@pytest.mark.asyncio
@_PASSKEY_AMR
async def test_an_old_passkey_sign_in_does_not_approve(
    amr: list[str] | None,
    _client: tuple[httpx.AsyncClient, Any],
    _resumed: list[uuid.UUID],
) -> None:
    """A passkey gives no longer window: a sign-in older than 15 minutes is refused."""
    ac, key = _client
    request_id = await _park()

    response = await _approve(ac, request_id, _passkey_token(key, age=901, amr=amr))

    assert response.status_code == 401, response.text
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _resumed == []


def _web_operator(claims: dict[str, Any]) -> Operator:
    """The console session's person, with the sign-in claims read as the JWT check reads them."""
    auth_time, acr, sid = _extract_sign_in_claims(claims)
    return Operator(
        sub="op-passkey-approver",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        auth_time=auth_time,
        acr=acr,
        sid=sid,
    )


@_PASSKEY_AMR
def test_a_recent_passkey_sign_in_approves_in_the_web_interface(amr: list[str] | None) -> None:
    claims = _passkey_claims(age=10, amr=amr)

    proof = check_fresh_web_approval_sign_in(_web_operator(claims), get_settings())

    assert proof.channel == "web"
    assert proof.method == "fresh_sign_in"
    assert proof.auth_time == claims["auth_time"]


@_PASSKEY_AMR
def test_an_old_passkey_sign_in_does_not_approve_in_the_web_interface(
    amr: list[str] | None,
) -> None:
    operator = _web_operator(_passkey_claims(age=901, amr=amr))

    with pytest.raises(HTTPException) as refused:
        check_fresh_web_approval_sign_in(operator, get_settings())

    assert refused.value.status_code == 401
