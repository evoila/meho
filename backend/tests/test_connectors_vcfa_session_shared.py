# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared VCF Automation session login (``vmware_shared/vcfa_session.py``).

Covered with a respx-mocked VCF Automation answer: the login path per org,
the ``<user>@<org>`` Basic user, the token header, the end of the token
from its ``exp`` claim (and the default when there is none), the renewal
rule, the ``connector_auth_failed`` error with its fix for a refused login,
the other errors, and that no token or password reaches a log line, an
error or a ``repr``.

All names and values are made up.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx
import structlog

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, SessionLoginError
from meho_backplane.connectors.vmware_shared import vcfa_session
from meho_backplane.connectors.vmware_shared.vcfa_session import (
    DEFAULT_LIFETIME_SECONDS,
    RENEW_BEFORE_SECONDS,
    VcfaSessionToken,
    session_path,
    token_expiry,
    vcfa_session_login,
)

_HOST = "vcfa-session.test.invalid"
_BASE_URL = f"https://{_HOST}"
_PASSWORD = "made-up-session-password"
_NOW = 1_800_000_000.0


def make_token(claims: dict[str, Any] | None) -> str:
    """A JWT-shaped made-up token with *claims* (no real signature)."""

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims or {})}.made-up-signature"


class _Target:
    name = "vcfa-session-target"
    host = _HOST
    secret_ref = "targets/made-up/vcfa-session"


async def _login(org: str | None) -> VcfaSessionToken:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        return await vcfa_session_login(
            client,
            username="svc-made-up",
            password=_PASSWORD,
            org=org,
            target=_Target(),
            now=_NOW,
        )


def test_session_path_uses_the_provider_path_only_for_system() -> None:
    assert session_path("System") == "/cloudapi/1.0.0/sessions/provider"
    assert session_path("system") == "/cloudapi/1.0.0/sessions/provider"
    assert session_path("made-up-org") == "/cloudapi/1.0.0/sessions"
    assert session_path(None) == "/cloudapi/1.0.0/sessions"


@pytest.mark.asyncio
async def test_login_sends_user_at_org_and_reads_the_token_and_its_end() -> None:
    token = make_token({"exp": _NOW + 3600})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/cloudapi/1.0.0/sessions").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": token}, json={"id": "made-up"}
        )
        session = await _login("made-up-org")

    request = route.calls[0].request
    expected = base64.b64encode(f"svc-made-up@made-up-org:{_PASSWORD}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.url.host == _HOST
    assert request.headers["accept"].startswith("application/json")
    assert session.value == token
    assert session.authorization == f"Bearer {token}"
    assert session.expires_at == _NOW + 3600


@pytest.mark.asyncio
async def test_system_org_logs_in_on_the_provider_path() -> None:
    token = make_token({"exp": _NOW + 3600})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        provider = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": token}
        )
        await _login("System")

    assert provider.call_count == 1


def test_token_without_exp_gets_the_default_lifetime() -> None:
    assert token_expiry("not-a-jwt", now=_NOW) == _NOW + DEFAULT_LIFETIME_SECONDS
    assert token_expiry(make_token({}), now=_NOW) == _NOW + DEFAULT_LIFETIME_SECONDS
    assert token_expiry(make_token({"exp": "soon"}), now=_NOW) == _NOW + DEFAULT_LIFETIME_SECONDS
    assert token_expiry("a.%%%.c", now=_NOW) == _NOW + DEFAULT_LIFETIME_SECONDS


def test_renewal_starts_sixty_seconds_before_the_end() -> None:
    session = VcfaSessionToken(value="made-up", expires_at=_NOW + 600)
    assert RENEW_BEFORE_SECONDS == 60
    assert session.needs_renewal(_NOW) is False
    assert session.needs_renewal(_NOW + 539) is False
    assert session.needs_renewal(_NOW + 540) is True
    assert session.needs_renewal(_NOW + 700) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_wrong_password_gives_connector_auth_failed_with_the_fix(status: int) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post("/cloudapi/1.0.0/sessions").respond(status)
        with pytest.raises(ConnectorAuthError) as caught:
            await _login("made-up-org")

    error = caught.value
    assert error.status_code == status
    assert error.cause == f"session_establish_{status}"
    assert error.target_name == "vcfa-session-target"
    assert error.secret_ref == "targets/made-up/vcfa-session"
    assert error.remediation is not None
    assert "made-up-org" in error.remediation
    assert "current password" in error.remediation
    assert _PASSWORD not in str(error)


@pytest.mark.asyncio
async def test_missing_org_gives_connector_auth_failed_that_says_to_set_it() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/cloudapi/1.0.0/sessions").respond(401)
        with pytest.raises(ConnectorAuthError) as caught:
            await _login(None)

    expected = base64.b64encode(f"svc-made-up:{_PASSWORD}".encode()).decode()
    assert route.calls[0].request.headers["authorization"] == f"Basic {expected}"
    assert caught.value.remediation is not None
    assert caught.value.remediation.startswith("The target has no extras")


@pytest.mark.asyncio
async def test_server_error_and_missing_token_header_are_session_login_errors() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/cloudapi/1.0.0/sessions")
        route.side_effect = [httpx.Response(500), httpx.Response(200, json={})]
        with pytest.raises(SessionLoginError) as server_error:
            await _login("made-up-org")
        with pytest.raises(SessionLoginError) as no_header:
            await _login("made-up-org")

    assert not isinstance(server_error.value, ConnectorAuthError)
    assert "HTTP 500" in str(server_error.value)
    assert "X-VMWARE-VCLOUD-ACCESS-TOKEN" in str(no_header.value)


@pytest.mark.asyncio
async def test_no_token_or_password_in_log_lines_or_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = make_token({"exp": _NOW + 3600})
    with structlog.testing.capture_logs() as events:
        # A logger made inside capture_logs always reaches it, even when an
        # earlier test in the same worker cached the module logger.
        monkeypatch.setattr(vcfa_session, "_log", structlog.get_logger(vcfa_session.__name__))
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.post("/cloudapi/1.0.0/sessions").respond(
                200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": token}
            )
            session = await _login("made-up-org")

    assert any(event["event"] == "vcfa_session_established" for event in events)
    rendered = json.dumps(events, default=str) + repr(session)
    assert token not in rendered
    assert _PASSWORD not in rendered
