# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approve over REST needs a fresh sign-in or a one-time code (#3979).

``POST /api/v1/approvals/{id}/approve`` and ``/decide`` with
``decision=approved`` accept only a token whose ``auth_time`` is at most
``FRESH_SIGN_IN_MAX_AGE_SECONDS`` old. Otherwise they answer 401
with the RFC 9470 challenge and the request stays pending. One sign-in
(same ``sid`` and ``auth_time``) allows one approval. Reject is unchanged.
The ``approval.decision`` audit row records how the approval was given.
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
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import _extract_sign_in_claims, clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PARAMS: dict[str, Any] = {"disk_gb": 40}
_OTP_ACR = "otp-step"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`~meho_backplane.settings.Settings` needs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("APPROVAL_ONE_TIME_CODE_ACR_VALUES", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def _fake_resume(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Replace the re-dispatch after approval; record which requests ran."""
    from meho_backplane.api.v1 import approvals as approvals_module

    resumed: list[uuid.UUID] = []

    async def _resume(*, operator: Operator, request: ApprovalRequest, params: Any) -> Any:
        resumed.append(request.id)
        return SimpleNamespace(status="ok", op_id=request.op_id, result=None, error=None)

    monkeypatch.setattr(approvals_module, "_resume_dispatch_after_approval", _resume)
    return resumed


async def _park() -> uuid.UUID:
    """Park one request by another person and return its id."""
    requester = Operator(
        sub="op-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
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


def _token(key: Any, **claims: Any) -> str:
    return mint_token(
        key,
        sub="op-approver",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        extra_claims=claims,
    )


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-fresh-sign-in")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _approve(ac: httpx.AsyncClient, route: str, request_id: uuid.UUID, token: str) -> Any:
    body: dict[str, Any] = (
        {"params": _PARAMS} if route == "approve" else {"decision": "approved", "reason": ""}
    )
    return await ac.post(
        f"/api/v1/approvals/{request_id}/{route}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _assert_challenge(response: httpx.Response, max_age: int = 300) -> None:
    assert response.status_code == 401, response.text
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith('Bearer error="insufficient_user_authentication"')
    assert f"max_age={max_age}" in challenge
    assert "approve in the web interface, or sign in again" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_old_sign_in_is_refused_and_the_request_stays_pending(
    route: str, _client: tuple[httpx.AsyncClient, Any], _fake_resume: list[uuid.UUID]
) -> None:
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()) - 301, sid="s-old")

    response = await _approve(ac, route, request_id, token)

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_login_from_a_remembered_browser_cannot_approve(
    route: str, _client: tuple[httpx.AsyncClient, Any], _fake_resume: list[uuid.UUID]
) -> None:
    """A remembered browser gives new tokens without a new sign-in.

    Keycloak keeps the ``auth_time`` of the sign-in from days ago in those
    tokens. So a browser cookie alone cannot approve: the approval needs a
    new password and code, or a passkey.
    """
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()) - 10 * 24 * 3600, sid="s-remembered")

    response = await _approve(ac, route, request_id, token)

    _assert_challenge(response)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_token_without_sign_in_time_is_refused(
    route: str, _client: tuple[httpx.AsyncClient, Any], _fake_resume: list[uuid.UUID]
) -> None:
    ac, key = _client
    request_id = await _park()

    response = await _approve(ac, route, request_id, _token(key))

    _assert_challenge(response)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_max_age_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    monkeypatch.setenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", "60")
    get_settings.cache_clear()
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()) - 90, sid="s-90")

    _assert_challenge(await _approve(ac, "decide", request_id, token), max_age=60)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_fresh_sign_in_approves_and_is_recorded(
    route: str, _client: tuple[httpx.AsyncClient, Any], _fake_resume: list[uuid.UUID]
) -> None:
    ac, key = _client
    request_id = await _park()
    auth_time = int(time.time()) - 10

    response = await _approve(ac, route, request_id, _token(key, auth_time=auth_time, sid="s-1"))

    assert response.status_code == 200, response.text
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert _fake_resume == [request_id]
    recorded = await _approval_auth(request_id)
    assert recorded["channel"] == "rest"
    assert recorded["method"] == "fresh_sign_in"
    assert recorded["auth_time"] == auth_time
    # The session id itself is not stored, only a hash of the sign-in.
    assert "s-1" not in str(recorded)
    assert len(recorded["sign_in_key"]) == 32


@pytest.mark.asyncio
async def test_one_time_code_acr_is_recorded_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    monkeypatch.setenv("APPROVAL_ONE_TIME_CODE_ACR_VALUES", f"other, {_OTP_ACR}")
    get_settings.cache_clear()
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()), sid="s-otp", acr=_OTP_ACR)

    response = await _approve(ac, "decide", request_id, token)

    assert response.status_code == 200, response.text
    assert (await _approval_auth(request_id))["method"] == "one_time_code"


@pytest.mark.asyncio
async def test_one_time_code_acr_alone_is_not_enough(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """An ``acr`` never replaces a recent ``auth_time``, set or not."""
    ac, key = _client
    stale = int(time.time()) - 3600
    first = await _park()
    token = _token(key, auth_time=stale, sid="s-acr", acr=_OTP_ACR)
    _assert_challenge(await _approve(ac, "decide", first, token))

    monkeypatch.setenv("APPROVAL_ONE_TIME_CODE_ACR_VALUES", _OTP_ACR)
    get_settings.cache_clear()
    second = await _park()
    _assert_challenge(await _approve(ac, "decide", second, token))
    assert await _status(second) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_acr_without_the_setting_counts_as_a_fresh_sign_in(
    _client: tuple[httpx.AsyncClient, Any], _fake_resume: list[uuid.UUID]
) -> None:
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()), sid="s-acr-off", acr=_OTP_ACR)

    response = await _approve(ac, "decide", request_id, token)

    assert response.status_code == 200, response.text
    assert (await _approval_auth(request_id))["method"] == "fresh_sign_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_route", ["approve", "decide"])
async def test_one_sign_in_allows_one_approval(
    second_route: str,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    ac, key = _client
    first, second = await _park(), await _park()
    auth_time = int(time.time())
    # Two tokens of the same sign-in: same sid and auth_time.
    token_a = _token(key, auth_time=auth_time, sid="s-once")
    token_b = _token(key, auth_time=auth_time, sid="s-once", jti="second-token")

    assert (await _approve(ac, "decide", first, token_a)).status_code == 200
    response = await _approve(ac, second_route, second, token_b)

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_already_used:")
    assert await _status(second) == ApprovalRequestStatus.PENDING.value

    # A new sign-in may approve it.
    token_c = _token(key, auth_time=auth_time, sid="s-new")
    assert (await _approve(ac, second_route, second, token_c)).status_code == 200


@pytest.mark.asyncio
async def test_one_sign_in_allows_one_approval_when_the_sign_in_clock_is_ahead(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """The reuse check also finds an approve written before ``auth_time``.

    The sign-in server's clock is 20 seconds ahead. The fresh sign-in check
    accepts that, so the first approve is written about 20 seconds before
    the token's ``auth_time``. The second approve with the same sign-in
    must still be refused.
    """
    ac, key = _client
    first, second = await _park(), await _park()
    auth_time = int(time.time()) + 20
    token_a = _token(key, auth_time=auth_time, sid="s-ahead")
    token_b = _token(key, auth_time=auth_time, sid="s-ahead", jti="second-token")

    assert (await _approve(ac, "approve", first, token_a)).status_code == 200
    response = await _approve(ac, "approve", second, token_b)

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_already_used:")
    assert await _status(second) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["reject", "decide"])
async def test_reject_needs_no_fresh_sign_in(
    route: str, _client: tuple[httpx.AsyncClient, Any]
) -> None:
    ac, key = _client
    request_id = await _park()
    body: dict[str, Any] = {"reason": "no"}
    if route == "decide":
        body["decision"] = "rejected"

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/{route}",
        json=body,
        headers={"Authorization": f"Bearer {_token(key)}"},
    )

    assert response.status_code == 200, response.text
    assert await _status(request_id) == ApprovalRequestStatus.REJECTED.value


@pytest.mark.asyncio
async def test_machine_principal_still_gets_403_first(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    request_id = await _park()
    token = mint_token(
        key,
        sub="agent-x",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT_A),
        principal_kind="agent",
        fresh_sign_in=True,
    )

    response = await _approve(ac, "approve", request_id, token)

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("human_principal_required:")


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"auth_time": 1700000000, "acr": "1", "sid": "abc"}, (1700000000, "1", "abc")),
        ({"auth_time": 1700000000.7}, (None, None, None)),
        ({"auth_time": True, "acr": "", "sid": 5}, (None, None, None)),
        ({"auth_time": "1700000000"}, (None, None, None)),
        ({}, (None, None, None)),
    ],
)
def test_sign_in_claims_are_read_fail_closed(
    claims: dict[str, Any], expected: tuple[Any, Any, Any]
) -> None:
    assert _extract_sign_in_claims(claims) == expected
