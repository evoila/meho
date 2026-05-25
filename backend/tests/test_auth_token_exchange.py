# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for G11.2-T2 (#816): RFC 8693 token-exchange and client_credentials.

Coverage matrix:

* **Delegation happy path** — ``exchange_for_delegation`` calls the token
  endpoint twice (client_credentials to get actor_token, then the exchange)
  and returns the delegated access token.
* **Autonomous happy path** — ``get_client_credentials_token`` calls the token
  endpoint once and returns the token.
* **Exchange refused** — ``invalid_target`` / ``access_denied`` Keycloak error
  codes raise :class:`TokenExchangeExchangeRefusedError`.
* **Generic exchange failure** — other Keycloak error codes raise
  :class:`TokenExchangeError`.
* **Network error** — ``httpx.ConnectError`` on the token endpoint raises
  :class:`TokenExchangeError` with ``code="network_error"``.
* **Missing access_token in 200 response** — raises :class:`TokenExchangeError`
  with ``code="missing_access_token"``.
* **act claim extraction in jwt._operator_from_claims** — a JWT with an
  ``act: {sub: "agent-42"}`` claim populates ``Operator.actor_sub``; a JWT
  without the claim leaves ``actor_sub=None``; a malformed ``act`` claim
  (not a dict) is treated as absent.

All tests are unit-level: network calls are intercepted with ``respx``;
no Keycloak or DB setup is required.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.token_exchange import (
    TokenExchangeError,
    TokenExchangeExchangeRefusedError,
    exchange_for_delegation,
    get_client_credentials_token,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_TOKEN_URL: str = f"{_ISSUER}/protocol/openid-connect/token"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"

_AGENT_CLIENT_ID: str = "meho-agent"
_AGENT_CLIENT_SECRET: str = "supersecret"

_USER_TOKEN: str = "fake-user-access-token"
_ACTOR_TOKEN: str = "fake-actor-access-token"
_DELEGATED_TOKEN: str = "fake-delegated-access-token"
_CC_TOKEN: str = "fake-cc-access-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars required by Settings; reset settings + JWKS caches."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


# ---------------------------------------------------------------------------
# Token-exchange service tests
# ---------------------------------------------------------------------------


class TestExchangeForDelegation:
    """Happy path and failure modes for :func:`exchange_for_delegation`."""

    @pytest.mark.asyncio
    async def test_happy_path_calls_token_endpoint_twice_and_returns_token(
        self,
    ) -> None:
        """Two calls to the token endpoint: client_credentials then exchange."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            if body.get("grant_type") == "client_credentials":
                return httpx.Response(200, json={"access_token": _ACTOR_TOKEN})
            # delegation exchange
            return httpx.Response(200, json={"access_token": _DELEGATED_TOKEN})

        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=_handler)
            result = await exchange_for_delegation(
                issuer_url=_ISSUER,
                subject_token=_USER_TOKEN,
                agent_client_id=_AGENT_CLIENT_ID,
                agent_client_secret=_AGENT_CLIENT_SECRET,
                audience=_AUDIENCE,
            )

        assert result == _DELEGATED_TOKEN
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_invalid_target_raises_exchange_refused(self) -> None:
        """``invalid_target`` Keycloak error maps to ExchangeRefusedError."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            if body.get("grant_type") == "client_credentials":
                return httpx.Response(200, json={"access_token": _ACTOR_TOKEN})
            return httpx.Response(
                400,
                json={
                    "error": "invalid_target",
                    "error_description": "may_act not permitted",
                },
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=_handler)
            with pytest.raises(TokenExchangeExchangeRefusedError) as exc_info:
                await exchange_for_delegation(
                    issuer_url=_ISSUER,
                    subject_token=_USER_TOKEN,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "invalid_target"

    @pytest.mark.asyncio
    async def test_access_denied_raises_exchange_refused(self) -> None:
        """``access_denied`` is a policy-refused code → ExchangeRefusedError."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            if body.get("grant_type") == "client_credentials":
                return httpx.Response(200, json={"access_token": _ACTOR_TOKEN})
            return httpx.Response(
                403,
                json={"error": "access_denied", "error_description": "denied"},
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=_handler)
            with pytest.raises(TokenExchangeExchangeRefusedError) as exc_info:
                await exchange_for_delegation(
                    issuer_url=_ISSUER,
                    subject_token=_USER_TOKEN,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "access_denied"

    @pytest.mark.asyncio
    async def test_server_error_raises_token_exchange_error(self) -> None:
        """Non-refused Keycloak error raises base TokenExchangeError."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            if body.get("grant_type") == "client_credentials":
                return httpx.Response(200, json={"access_token": _ACTOR_TOKEN})
            return httpx.Response(500, json={"error": "server_error"})

        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=_handler)
            with pytest.raises(TokenExchangeError) as exc_info:
                await exchange_for_delegation(
                    issuer_url=_ISSUER,
                    subject_token=_USER_TOKEN,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "server_error"
        # Must NOT be the subclass (not a policy refusal)
        assert type(exc_info.value) is TokenExchangeError

    @pytest.mark.asyncio
    async def test_network_error_raises_token_exchange_error(self) -> None:
        """ConnectError on actor_token fetch raises TokenExchangeError(network_error)."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("unreachable"))
            with pytest.raises(TokenExchangeError) as exc_info:
                await exchange_for_delegation(
                    issuer_url=_ISSUER,
                    subject_token=_USER_TOKEN,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "network_error"

    @pytest.mark.asyncio
    async def test_missing_access_token_in_200_raises(self) -> None:
        """200 response without access_token raises TokenExchangeError."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            if body.get("grant_type") == "client_credentials":
                return httpx.Response(200, json={"access_token": _ACTOR_TOKEN})
            # exchange returns 200 but with no access_token
            return httpx.Response(200, json={"token_type": "Bearer"})

        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=_handler)
            with pytest.raises(TokenExchangeError) as exc_info:
                await exchange_for_delegation(
                    issuer_url=_ISSUER,
                    subject_token=_USER_TOKEN,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "missing_access_token"


class TestGetClientCredentialsToken:
    """Happy path and failure modes for :func:`get_client_credentials_token`."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_token(self) -> None:
        """Single call to the token endpoint; returns the access_token."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json={"access_token": _CC_TOKEN})
            )
            result = await get_client_credentials_token(
                issuer_url=_ISSUER,
                agent_client_id=_AGENT_CLIENT_ID,
                agent_client_secret=_AGENT_CLIENT_SECRET,
                audience=_AUDIENCE,
            )

        assert result == _CC_TOKEN

    @pytest.mark.asyncio
    async def test_network_error_raises(self) -> None:
        """ConnectError → TokenExchangeError(network_error)."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
            with pytest.raises(TokenExchangeError) as exc_info:
                await get_client_credentials_token(
                    issuer_url=_ISSUER,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "network_error"

    @pytest.mark.asyncio
    async def test_keycloak_error_raises(self) -> None:
        """Non-200 Keycloak response → TokenExchangeError."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(401, json={"error": "invalid_client"})
            )
            with pytest.raises(TokenExchangeError) as exc_info:
                await get_client_credentials_token(
                    issuer_url=_ISSUER,
                    agent_client_id=_AGENT_CLIENT_ID,
                    agent_client_secret=_AGENT_CLIENT_SECRET,
                    audience=_AUDIENCE,
                )

        assert exc_info.value.code == "invalid_client"


# ---------------------------------------------------------------------------
# act-claim extraction tests (via verify_jwt_for_audience)
# ---------------------------------------------------------------------------


def _make_rsa_keypair(kid: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key("RSA", 2048, options={"kid": kid}, is_private=True)


def _mint_token(
    private_key: Any,
    *,
    sub: str = "user-123",
    act: dict[str, Any] | None = None,
    audience: str = _AUDIENCE,
) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "iss": _ISSUER,
            "aud": audience,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
            "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
            "tenant_role": "operator",
        }
        if act is not None:
            payload["act"] = act
        header = {"alg": "RS256", "kid": private_key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _mock_discovery_and_jwks(
    mock_router: respx.MockRouter,
    jwks: dict[str, Any],
) -> None:
    mock_router.get(_DISCOVERY_URL).mock(
        return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
    )
    mock_router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))


class TestActClaimExtraction:
    """Verify ``Operator.actor_sub`` is populated from the ``act`` JWT claim."""

    @pytest.mark.asyncio
    async def test_delegation_token_populates_actor_sub(self) -> None:
        """A token with ``act: {sub: 'agent-42'}`` sets Operator.actor_sub."""
        key = _make_rsa_keypair("k1")
        jwks = {"keys": [key.as_dict(is_private=False)]}
        token = _mint_token(key, sub="user-123", act={"sub": "agent-42"})

        with respx.mock as mock:
            _mock_discovery_and_jwks(mock, jwks)
            operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=_AUDIENCE)

        assert operator.sub == "user-123"
        assert operator.actor_sub == "agent-42"

    @pytest.mark.asyncio
    async def test_direct_user_token_has_no_actor_sub(self) -> None:
        """A token without ``act`` leaves Operator.actor_sub as None."""
        key = _make_rsa_keypair("k2")
        jwks = {"keys": [key.as_dict(is_private=False)]}
        token = _mint_token(key, sub="user-456")

        with respx.mock as mock:
            _mock_discovery_and_jwks(mock, jwks)
            operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=_AUDIENCE)

        assert operator.sub == "user-456"
        assert operator.actor_sub is None

    @pytest.mark.asyncio
    async def test_malformed_act_claim_not_a_dict_treated_as_absent(self) -> None:
        """``act`` that is not a JSON object is silently ignored."""
        key = _make_rsa_keypair("k3")
        jwks = {"keys": [key.as_dict(is_private=False)]}
        # act is a string, not a dict
        token = _mint_token(key, sub="user-789", act="not-a-dict")  # type: ignore[arg-type]

        with respx.mock as mock:
            _mock_discovery_and_jwks(mock, jwks)
            operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=_AUDIENCE)

        assert operator.actor_sub is None

    @pytest.mark.asyncio
    async def test_act_claim_empty_sub_treated_as_absent(self) -> None:
        """``act: {sub: ''}`` is treated as absent — no empty actor_sub."""
        key = _make_rsa_keypair("k4")
        jwks = {"keys": [key.as_dict(is_private=False)]}
        token = _mint_token(key, sub="user-000", act={"sub": ""})

        with respx.mock as mock:
            _mock_discovery_and_jwks(mock, jwks)
            operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=_AUDIENCE)

        assert operator.actor_sub is None
