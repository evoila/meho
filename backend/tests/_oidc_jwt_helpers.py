# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared OIDC / JWT minting helpers for the test suite.

The same four ingredients — a fixture issuer URL pair, an
RSA-2048 keypair generator, a JWKS document builder, a basic-payload
JWT minter, and a respx stub for the discovery + JWKS pair — are used
by ``test_api_v1_health.py``, ``test_audit_middleware.py``, and
``test_migration_rollback.py`` (the suites that exercise the FastAPI
``verify_jwt`` dependency end-to-end without going through Keycloak).

``test_auth_jwt.py`` keeps its own richer ``_mint_token`` because the
JWT-validation suite needs knobs the integration tests never reach
for (``extra_claims``, custom claim names, ``not_before_offset``,
issuer / audience overrides for negative paths). Pulling those into
the shared helper would bloat the integration call sites for no
benefit.

Why a private module under ``tests/`` instead of a pytest fixture:
the helpers are stateless pure functions; making them fixtures would
add boilerplate (``def test_x(make_rsa_keypair, mint_token, ...)``)
and force a test parameter for every helper call. Direct imports
keep the call sites short and let mypy resolve signatures without
the fixture-injection indirection.
"""

from __future__ import annotations

import time
import warnings
from typing import Any

import httpx
import respx

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

ISSUER: str = "https://keycloak.test/realms/meho"
AUDIENCE: str = "meho-backplane"
DISCOVERY_URL: str = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL: str = f"{ISSUER}/protocol/openid-connect/certs"

DEFAULT_TENANT_ID: str = "00000000-0000-0000-0000-00000000a0a0"
DEFAULT_TENANT_ROLE: str = "operator"


def make_rsa_keypair(kid: str) -> Any:
    """Generate a fresh RSA-2048 keypair tagged with *kid*."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key(
            "RSA",
            2048,
            options={"kid": kid},
            is_private=True,
        )


def public_jwks(*keys: Any) -> dict[str, list[dict[str, Any]]]:
    """Build a JWKS document containing the public half of each key."""
    return {"keys": [k.as_dict(is_private=False) for k in keys]}


def mint_token(
    private_key: Any,
    *,
    sub: str = "op-42",
    name: str | None = "Damir",
    email: str | None = "damir@example.com",
    tenant_id: str = DEFAULT_TENANT_ID,
    tenant_role: str = DEFAULT_TENANT_ROLE,
    audience: str | None = None,
    capabilities: list[str] | None = None,
) -> str:
    """Mint a happy-path JWT signed by *private_key*.

    Defaults match the integration suites' baseline operator. Pass
    overrides for ``sub`` / ``name`` / ``email`` when the test asserts
    on those fields downstream (e.g. audit-row read-back).

    ``audience`` defaults to the chassis ``KEYCLOAK_AUDIENCE`` so the
    existing call sites (chassis ``/api/v1/health`` integration tests)
    don't change. The MCP acceptance suite passes the canonical MCP
    resource URI here so the same minter can produce tokens for both
    audiences without forking a parallel helper.

    ``capabilities`` populates the ``capabilities`` JWT claim the backend's
    ``_extract_capabilities`` reads onto ``Operator.capabilities`` (G4.5-T1
    add-on / G4.6-T3 per-collection entitlement). ``None`` omits the claim
    entirely so pre-capability call sites are unchanged.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "iss": ISSUER,
            "aud": audience if audience is not None else AUDIENCE,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
            "tenant_id": tenant_id,
            "tenant_role": tenant_role,
        }
        if name is not None:
            payload["name"] = name
        if email is not None:
            payload["email"] = email
        if capabilities is not None:
            payload["capabilities"] = capabilities
        header = {"alg": "RS256", "kid": private_key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def mock_discovery_and_jwks(
    mock_router: respx.MockRouter,
    jwks: dict[str, Any],
) -> None:
    """Stub the OIDC discovery endpoint and the JWKS endpoint on *mock_router*."""
    mock_router.get(DISCOVERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"issuer": ISSUER, "jwks_uri": JWKS_URL},
        ),
    )
    mock_router.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=jwks),
    )
