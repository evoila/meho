# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approve over REST needs a fresh approval sign-in (#3979, #5515).

``POST /api/v1/approvals/{id}/approve`` and ``/decide`` with
``decision=approved`` accept only a person's token from the approval
client (``azp`` = ``meho-cli-approve``) whose ``auth_time`` is inside the
approval window (``FRESH_SIGN_IN_MAX_AGE_SECONDS``, default 900).
Otherwise they answer 401 with the RFC 9470 challenge and the request
stays pending. Inside the window one sign-in may approve several requests.
A ``destructive`` request needs a sign-in of its own, at most
``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`` old; that limit replaces the window
for it, in both directions. Reject is unchanged. The ``approval.decision``
audit row records how the approval was given.
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

from ._oidc_jwt_helpers import APPROVAL_CLIENT_ID as _APPROVAL_CLIENT
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
    monkeypatch.delenv("PER_ACTION_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
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


async def _park(*, destructive: bool = False) -> uuid.UUID:
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
            proposed_effect=(
                {"op_id": "vm.create", "safety_level": "destructive"} if destructive else None
            ),
            preview_hash="preview-hash" if destructive else None,
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
    """A person's token from a password sign-in with the approval client.

    *claims* change or add claims; a claim set to ``None`` is left out.
    """
    payload = {"azp": _APPROVAL_CLIENT, "acr": "1", **claims}
    return mint_token(
        key,
        sub="op-approver",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        extra_claims={k: v for k, v in payload.items() if v is not None},
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


def _assert_challenge(response: httpx.Response, max_age: int = 900) -> None:
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
    token = _token(key, auth_time=int(time.time()) - 901, sid="s-old")

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

    # A session id but no sign-in time.
    response = await _approve(ac, route, request_id, _token(key, sid="s-no-time"))

    _assert_challenge(response)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
@pytest.mark.parametrize("acr", ["0", None])
async def test_a_recent_sign_in_without_credentials_is_refused(
    route: str,
    acr: str | None,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """A recent approval-client token needs ``acr`` set and not ``"0"``."""
    ac, key = _client
    request_id = await _park()
    token = _token(key, auth_time=int(time.time()) - 5, sid="s-no-password", acr=acr)

    response = await _approve(ac, route, request_id, token)

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


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
    assert recorded["acr"] == "1"
    assert "single_use" not in recorded
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
    recorded = await _approval_auth(request_id)
    assert recorded["method"] == "one_time_code"
    assert recorded["acr"] == _OTP_ACR


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
    recorded = await _approval_auth(request_id)
    assert recorded["method"] == "fresh_sign_in"
    assert recorded["acr"] == _OTP_ACR


@pytest.mark.asyncio
async def test_one_sign_in_approves_several_requests_inside_the_window(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """Three non-destructive requests, one sign-in, two routes (#5515)."""
    ac, key = _client
    requests = [await _park() for _ in range(3)]
    auth_time = int(time.time()) - 600
    # A renewed token of the same sign-in: same sid and auth_time.
    token_a = _token(key, auth_time=auth_time, sid="s-window")
    token_b = _token(key, auth_time=auth_time, sid="s-window", jti="renewed")

    assert (await _approve(ac, "decide", requests[0], token_a)).status_code == 200
    assert (await _approve(ac, "approve", requests[1], token_b)).status_code == 200
    assert (await _approve(ac, "decide", requests[2], token_a)).status_code == 200

    assert _fake_resume == requests
    keys = {(await _approval_auth(r))["sign_in_key"] for r in requests}
    assert len(keys) == 1, "all three approvals name the same sign-in"


@pytest.mark.asyncio
async def test_the_window_ends_900_seconds_after_the_sign_in(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """The window counts from ``auth_time``; approving does not make it longer."""
    ac, key = _client
    first, second = await _park(), await _park()
    inside = _token(key, auth_time=int(time.time()) - 890, sid="s-end")
    assert (await _approve(ac, "decide", first, inside)).status_code == 200

    # The same sign-in, a few seconds later in its life: outside the window.
    outside = _token(key, auth_time=int(time.time()) - 901, sid="s-end")
    _assert_challenge(await _approve(ac, "decide", second, outside))
    assert await _status(second) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("azp", ["meho-cli", "meho-mcp", None])
async def test_only_the_approval_client_can_approve(
    azp: str | None,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """A stored login, an MCP token or a token with no ``azp`` gets the 401 (#5515)."""
    ac, key = _client
    request_id = await _park()
    token = mint_token(
        key,
        sub="op-approver",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        extra_claims={"auth_time": int(time.time()), "sid": "s-other", "azp": azp},
    )

    response = await _approve(ac, "decide", request_id, token)

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
async def test_the_approval_client_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    monkeypatch.setenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", "my-approve")
    get_settings.cache_clear()
    ac, key = _client
    first, second = await _park(), await _park()

    _assert_challenge(await _approve(ac, "decide", first, _token(key, auth_time=int(time.time()))))
    ok = _token(key, auth_time=int(time.time()), azp="my-approve")
    assert (await _approve(ac, "decide", second, ok)).status_code == 200

    # Empty: no token approves at all.
    monkeypatch.setenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", "")
    get_settings.cache_clear()
    third = await _park()
    _assert_challenge(
        await _approve(ac, "decide", third, _token(key, auth_time=int(time.time()), azp=""))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_route", ["approve", "decide"])
async def test_a_destructive_request_needs_a_sign_in_of_its_own(
    second_route: str,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    ac, key = _client
    first, second = await _park(destructive=True), await _park(destructive=True)
    auth_time = int(time.time())
    token_a = _token(key, auth_time=auth_time, sid="s-once")
    token_b = _token(key, auth_time=auth_time, sid="s-once", jti="second-token")

    assert (await _approve(ac, "decide", first, token_a)).status_code == 200
    assert (await _approval_auth(first))["single_use"] is True
    response = await _approve(ac, second_route, second, token_b)

    _assert_challenge(response, max_age=300)
    assert response.json()["detail"].startswith("fresh_sign_in_already_used:")
    assert await _status(second) == ApprovalRequestStatus.PENDING.value

    # A new sign-in may approve it.
    token_c = _token(key, auth_time=auth_time, sid="s-new")
    assert (await _approve(ac, second_route, second, token_c)).status_code == 200


@pytest.mark.asyncio
async def test_a_used_sign_in_cannot_approve_a_destructive_request(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    ac, key = _client
    plain, destructive = await _park(), await _park(destructive=True)
    token = _token(key, auth_time=int(time.time()), sid="s-used")

    assert (await _approve(ac, "decide", plain, token)).status_code == 200
    response = await _approve(ac, "decide", destructive, token)

    _assert_challenge(response, max_age=300)
    assert response.json()["detail"].startswith("fresh_sign_in_already_used:")
    assert await _status(destructive) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_destructive_approval_uses_up_the_sign_in(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    ac, key = _client
    destructive, plain = await _park(destructive=True), await _park()
    token = _token(key, auth_time=int(time.time()), sid="s-spent")

    assert (await _approve(ac, "decide", destructive, token)).status_code == 200
    response = await _approve(ac, "decide", plain, token)

    _assert_challenge(response, max_age=300)
    assert response.json()["detail"].startswith("fresh_sign_in_already_used:")
    assert await _status(plain) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_destructive_request_does_not_use_the_window(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """A 400-second-old sign-in is inside the window but too old here."""
    ac, key = _client
    request_id = await _park(destructive=True)
    token = _token(key, auth_time=int(time.time()) - 400, sid="s-400")

    response = await _approve(ac, "approve", request_id, token)

    _assert_challenge(response, max_age=300)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_a_destructive_request_keeps_its_own_limit_when_the_window_is_shorter(
    route: str,
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """Window 120 seconds, destructive limit 300: a 180-second-old sign-in
    is too old for a plain request and young enough for a destructive one."""
    monkeypatch.setenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", "120")
    get_settings.cache_clear()
    ac, key = _client
    plain, destructive = await _park(), await _park(destructive=True)
    token = _token(key, auth_time=int(time.time()) - 180, sid="s-180")

    response = await _approve(ac, route, plain, token)
    _assert_challenge(response, max_age=120)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(plain) == ApprovalRequestStatus.PENDING.value

    assert (await _approve(ac, route, destructive, token)).status_code == 200
    assert await _status(destructive) == ApprovalRequestStatus.APPROVED.value
    assert (await _approval_auth(destructive))["single_use"] is True
    assert _fake_resume == [destructive]


@pytest.mark.asyncio
async def test_a_destructive_request_finds_an_approval_written_before_auth_time(
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """The reuse check also finds an approve written before ``auth_time``.

    The sign-in server's clock is 20 seconds ahead. The fresh sign-in check
    accepts that, so the first approve is written about 20 seconds before
    the token's ``auth_time``. A destructive approve with the same sign-in
    must still be refused.
    """
    ac, key = _client
    first, second = await _park(), await _park(destructive=True)
    auth_time = int(time.time()) + 20
    token_a = _token(key, auth_time=auth_time, sid="s-ahead")
    token_b = _token(key, auth_time=auth_time, sid="s-ahead", jti="second-token")

    assert (await _approve(ac, "approve", first, token_a)).status_code == 200
    response = await _approve(ac, "approve", second, token_b)

    _assert_challenge(response, max_age=300)
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
        headers={"Authorization": f"Bearer {_token(key, sid='s-reject')}"},
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
