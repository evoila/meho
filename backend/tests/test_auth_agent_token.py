# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the agent ``client_credentials`` token flow (G11.2-T2 #816).

Exercises :func:`~meho_backplane.auth.agent_token.get_client_credentials_token`
against a mocked Keycloak token endpoint (respx) — the request contract and
every failure mode of the grant autonomous agent runs authenticate with. This
is a *real* OAuth grant Keycloak supports today (unlike the RFC 8693
delegation exchange #1051 attempted), so the contract is fully exercisable.
"""

from __future__ import annotations

import httpx
import pytest
import respx
import structlog

from meho_backplane.auth.agent_token import AgentTokenError, get_client_credentials_token

_ISSUER = "https://kc.test/realms/meho"
_TOKEN_URL = f"{_ISSUER}/protocol/openid-connect/token"
_SECRET = "super-secret-value"


@pytest.mark.asyncio
async def test_returns_access_token_and_sends_client_credentials_grant() -> None:
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "agent-tok", "expires_in": 300})
        )
        token = await get_client_credentials_token(
            issuer_url=_ISSUER, client_id="agent:bot", client_secret=_SECRET
        )
    assert token == "agent-tok"
    sent = route.calls[0].request
    body = sent.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=agent%3Abot" in body or "client_id=agent:bot" in body
    assert _SECRET in body  # the secret is sent on the wire (form-encoded), as required


@pytest.mark.asyncio
async def test_audience_is_forwarded_when_given() -> None:
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "agent-tok"})
        )
        await get_client_credentials_token(
            issuer_url=_ISSUER,
            client_id="agent:bot",
            client_secret=_SECRET,
            audience="meho-backplane",
        )
    assert "audience=meho-backplane" in route.calls[0].request.content.decode()


@pytest.mark.asyncio
async def test_issuer_trailing_slash_is_normalised() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
        token = await get_client_credentials_token(
            issuer_url=f"{_ISSUER}/", client_id="agent:bot", client_secret=_SECRET
        )
    assert token == "t"


@pytest.mark.asyncio
async def test_network_error_raises_typed() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(AgentTokenError) as exc:
            await get_client_credentials_token(
                issuer_url=_ISSUER, client_id="agent:bot", client_secret=_SECRET
            )
    assert exc.value.code == "network_error"


@pytest.mark.asyncio
async def test_http_401_invalid_client_raises_typed() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
        with pytest.raises(AgentTokenError) as exc:
            await get_client_credentials_token(
                issuer_url=_ISSUER, client_id="agent:bot", client_secret="wrong"
            )
    assert exc.value.code == "http_401"


@pytest.mark.asyncio
async def test_missing_access_token_raises_typed() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"expires_in": 300}))
        with pytest.raises(AgentTokenError) as exc:
            await get_client_credentials_token(
                issuer_url=_ISSUER, client_id="agent:bot", client_secret=_SECRET
            )
    assert exc.value.code == "missing_access_token"


@pytest.mark.asyncio
async def test_client_secret_never_logged() -> None:
    """The agent client secret must not appear in any structlog event."""
    with structlog.testing.capture_logs() as logs, respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(AgentTokenError):
            await get_client_credentials_token(
                issuer_url=_ISSUER, client_id="agent:bot", client_secret=_SECRET
            )
    serialised = repr(logs)
    assert _SECRET not in serialised
    # but the failure is observable by client_id + status
    assert any(e.get("client_id") == "agent:bot" for e in logs)
