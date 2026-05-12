# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the OAuth 2.1 resource-server chain on ``/mcp`` (G0.5-T2, #247).

Covers the auth-side acceptance criteria on issue #247:

* Missing ``Authorization`` header → 401 + RFC 9728 §5.1 ``WWW-Authenticate``
  header pointing at ``/.well-known/oauth-protected-resource``.
* JWT with the chassis audience (``KEYCLOAK_AUDIENCE``, not the MCP
  resource URI) → 401: audience binding per RFC 8707 §2 is enforced
  distinct from the HTTP API.
* JWT with the MCP resource URI in ``aud`` → 200 + a valid JSON-RPC
  response (``ping`` round-trips).
* Malformed ``Authorization`` shape (``Basic …`` instead of
  ``Bearer …``) → 401 + WWW-Authenticate.
* The ``WWW-Authenticate`` value uses the exact RFC 9728 shape:
  ``Bearer resource_metadata="<absolute-url>"``.

Test strategy mirrors :mod:`tests.test_api_v1_health` — RSA keypair
fixture, JWKS document, ``respx`` mocks of the OIDC discovery + JWKS
endpoints. The bearer tokens are signed locally with the fixture key
and the JWKS document is served from the same key, so the chassis
verify chain accepts them as if they came from a real Keycloak realm.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebToken

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, DEFAULT_TENANT_ROLE
from ._oidc_jwt_helpers import ISSUER as _CHASSIS_ISSUER
from ._oidc_jwt_helpers import JWKS_URL as _JWKS_URL
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL: str = "https://meho.test"
_MCP_RESOURCE_URI: str = f"{_BACKPLANE_URL}/mcp"
_CHASSIS_AUDIENCE: str = "meho-backplane"


def _mint_mcp_token(
    private_key: Any,
    *,
    audience: str = _MCP_RESOURCE_URI,
    sub: str = "mcp-op-42",
    tenant_id: str = DEFAULT_TENANT_ID,
    tenant_role: str = DEFAULT_TENANT_ROLE,
) -> str:
    """Mint a JWT signed by *private_key* with the given audience.

    Local-only helper because the shared :func:`mint_token` in
    :mod:`_oidc_jwt_helpers` hard-codes the chassis audience. Adding an
    ``audience`` knob there would land in :mod:`test_api_v1_health`'s
    helper module; keeping a local mint helper here keeps the change
    contained to the MCP test suite.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "iss": _CHASSIS_ISSUER,
            "aud": audience,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
            "tenant_id": tenant_id,
            "tenant_role": tenant_role,
            "name": "MCP Test",
            "email": "mcp-test@example.com",
        }
        header = {"alg": "RS256", "kid": private_key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


@pytest.fixture(autouse=True)
def _mcp_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the MCP-relevant env vars around every test in this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _CHASSIS_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _CHASSIS_AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("MCP_RESOURCE_URI", "")  # exercise the fallback derivation
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test.

    Without this guard a token from one test could be accepted by
    another via stale-cache hits, which would mask the audience-
    rejection assertions.
    """
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def keypair() -> Any:
    """RSA-2048 keypair used to sign every token in this module."""
    return _make_rsa_keypair(kid="mcp-auth-test-kid")


@pytest.fixture
def jwks(keypair: Any) -> dict[str, Any]:
    """JWKS document containing the public half of :func:`keypair`."""
    return _public_jwks(keypair)


@pytest.fixture
def client() -> TestClient:
    """Plain :class:`TestClient` — no dependency overrides in this module.

    The full auth chain runs end-to-end against the respx-mocked
    Keycloak. That is the whole point of this test file.
    """
    return TestClient(app)


# ---------------------------------------------------------------------------
# 401 contract: missing or malformed Bearer credential
# ---------------------------------------------------------------------------


def test_missing_authorization_returns_401_with_www_authenticate(
    client: TestClient,
) -> None:
    """No ``Authorization`` header → 401 + RFC 9728 §5.1 header.

    The ``resource_metadata`` parameter MUST be the absolute URL of the
    backplane's RFC 9728 metadata document so the client can fetch it
    and learn the authorisation server.
    """
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )

    assert response.status_code == 401
    www_auth = response.headers.get("www-authenticate", "")
    assert www_auth.startswith("Bearer")
    assert f'resource_metadata="{_BACKPLANE_URL}/.well-known/oauth-protected-resource"' in www_auth


def test_basic_auth_scheme_returns_401_with_www_authenticate(
    client: TestClient,
) -> None:
    """An ``Authorization: Basic …`` shape is rejected as missing-Bearer."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# Audience binding (RFC 8707 §2)
# ---------------------------------------------------------------------------


def test_token_for_chassis_audience_is_rejected_at_mcp(
    client: TestClient,
    keypair: Any,
    jwks: dict[str, Any],
) -> None:
    """A JWT issued for ``KEYCLOAK_AUDIENCE`` does not grant ``/mcp`` access.

    The chassis HTTP API and the MCP route have distinct audiences per
    RFC 8707 §2; replaying a chassis token at ``/mcp`` is the canonical
    confused-deputy attack the binding rule defends against.
    """
    with respx.mock as router:
        _mock_discovery_and_jwks(router, jwks)
        chassis_token = _mint_mcp_token(keypair, audience=_CHASSIS_AUDIENCE)
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            headers={"Authorization": f"Bearer {chassis_token}"},
        )

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


def test_token_with_mcp_audience_is_accepted(
    client: TestClient,
    keypair: Any,
    jwks: dict[str, Any],
) -> None:
    """A JWT with ``aud == MCP_RESOURCE_URI`` traverses the chain to the dispatcher.

    End-to-end happy path: token validated, ``ping`` dispatched, JSON-RPC
    response returned with the correct id and an empty ``result`` body.
    """
    with respx.mock as router:
        _mock_discovery_and_jwks(router, jwks)
        token = _mint_mcp_token(keypair)
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 4
    assert body["result"] == {}


# ---------------------------------------------------------------------------
# WWW-Authenticate header shape (RFC 9728 §5.1)
# ---------------------------------------------------------------------------


def test_www_authenticate_header_has_rfc9728_shape(client: TestClient) -> None:
    """Header value is exactly ``Bearer resource_metadata="<url>"``.

    The MCP client parses this header to discover the metadata URL; a
    syntactically different shape (missing quotes, wrong parameter
    name) breaks the spec-conforming discovery flow.
    """
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "ping"},
    )

    www_auth = response.headers["www-authenticate"]
    # Format: `Bearer resource_metadata="<url>"`. Quote the URL so clients
    # tolerate parameters with `/` and `:` (RFC 9728 §5.1 example).
    expected = f'Bearer resource_metadata="{_BACKPLANE_URL}/.well-known/oauth-protected-resource"'
    assert www_auth == expected


# ---------------------------------------------------------------------------
# Expiry handling — token validation chain end-to-end
# ---------------------------------------------------------------------------


def test_expired_token_returns_401(
    client: TestClient,
    keypair: Any,
    jwks: dict[str, Any],
) -> None:
    """Expired ``exp`` claim → 401, regardless of audience correctness.

    Mints a token with ``exp`` 10 seconds in the past (well outside the
    configured 30-second leeway). The chassis chain raises
    :class:`authlib.jose.errors.ExpiredTokenError`, which maps to
    ``invalid_token`` and 401 — and the MCP wrapper adds the
    ``WWW-Authenticate`` header.
    """
    with respx.mock as router:
        _mock_discovery_and_jwks(router, jwks)
        # Hand-mint an expired token (the helper assumes 1h validity).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            jwt = JsonWebToken(["RS256"])
            now = int(time.time())
            payload: dict[str, Any] = {
                "sub": "expired-op",
                "iss": _CHASSIS_ISSUER,
                "aud": _MCP_RESOURCE_URI,
                "iat": now - 7200,
                "exp": now - 60,  # expired
                "nbf": now - 7200,
                "tenant_id": DEFAULT_TENANT_ID,
                "tenant_role": DEFAULT_TENANT_ROLE,
            }
            header = {
                "alg": "RS256",
                "kid": keypair.as_dict()["kid"],
                "typ": "JWT",
            }
            token_b: bytes | str = jwt.encode(header, payload, keypair)
            token = token_b.decode("ascii") if isinstance(token_b, bytes) else token_b

        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 6, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# JWKS unreachable — 401 (degrades gracefully, not 5xx)
# ---------------------------------------------------------------------------


def test_jwks_unreachable_returns_401(
    client: TestClient,
    keypair: Any,
) -> None:
    """A Keycloak JWKS endpoint that times out / 5xxs surfaces as 401.

    The chassis chain maps ``httpx.HTTPError`` from the JWKS fetch to
    ``_http_401("jwks_unavailable")``. The MCP wrapper then adds the
    WWW-Authenticate header — a client retrying after rediscovery is
    the correct UX even when the failure mode is server-side.
    """
    with respx.mock as router:
        # Mount the discovery endpoint but fail the JWKS hit.
        router.get(f"{_CHASSIS_ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={"issuer": _CHASSIS_ISSUER, "jwks_uri": _JWKS_URL},
            ),
        )
        router.get(_JWKS_URL).mock(return_value=httpx.Response(503))
        token = _mint_mcp_token(keypair)
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 7, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# Settings resolution: explicit MCP_RESOURCE_URI overrides the BACKPLANE_URL derivation
# ---------------------------------------------------------------------------


def test_explicit_mcp_resource_uri_is_the_required_audience(
    keypair: Any,
    jwks: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MCP_RESOURCE_URI`` overrides the ``{BACKPLANE_URL}/mcp`` default.

    Operators with non-default MCP mounts set this explicitly; the
    audience-binding rule then expects tokens to carry the explicit
    value in their ``aud`` claim. A token minted against the
    *derived* default URI is rejected.
    """
    monkeypatch.setenv("MCP_RESOURCE_URI", "https://meho.test/api/mcp")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        with respx.mock as router:
            _mock_discovery_and_jwks(router, jwks)
            # Token minted against the *derived* default URI — should be
            # rejected because the operator has overridden to /api/mcp.
            stale_token = _mint_mcp_token(keypair, audience=_MCP_RESOURCE_URI)
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 8, "method": "ping"},
                headers={"Authorization": f"Bearer {stale_token}"},
            )
            assert response.status_code == 401

            # Token minted against the explicit override — accepted.
            clear_jwks_cache()
            ok_token = _mint_mcp_token(
                keypair,
                audience="https://meho.test/api/mcp",
            )
            ok_response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                headers={"Authorization": f"Bearer {ok_token}"},
            )
            assert ok_response.status_code == 200
            assert ok_response.json()["result"] == {}
    finally:
        get_settings.cache_clear()
