# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the JWT validation primitive.

Coverage matrix (per Task #22 acceptance criteria):

* Happy path: a JWT signed by a fixture key is accepted; the resulting
  :class:`Operator` carries the expected ``sub`` / ``name`` / ``email``
  / ``raw_jwt`` fields.
* JWKS cache hit: two consecutive verifies issue exactly one JWKS fetch.
* JWKS cache miss + refresh: clearing the cache forces a refetch.
* Kid rotation: when the first JWKS response lacks the JWT's ``kid``,
  the dependency refetches JWKS once and succeeds on retry.
* Audience mismatch → 401.
* Issuer mismatch → 401.
* Expired token → 401.
* Tampered signature → 401.
* Missing / malformed Authorization header → 401.

Failure-mode coverage beyond these basics is the responsibility of
Task #25 (G2.2-T4); this file ships a happy-path-plus-sanity-check
suite so #22 can land in isolation.

Test fixture strategy: the suite mints its own RSA key pair, builds a
JWKS document around the public half, and uses ``respx`` to intercept
both the OIDC discovery URL and the ``jwks_uri`` returned by it. That
keeps the test self-contained — no network, no test-double Keycloak
container, no fixture key files in the repo.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth.jwt import (
    clear_jwks_cache,
    keycloak_readiness_probe,
    verify_jwt,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var the Settings model reads and reset the cache.

    Settings are cached per-process via ``functools.lru_cache``; without
    a per-test reset, an env-var change wouldn't propagate. The Vault
    knobs are pinned here even though this file does not exercise the
    Vault client — :class:`Settings` validates them at construction
    time, so any path that calls :func:`get_settings` (including
    :func:`verify_jwt`) needs them populated.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _make_rsa_keypair(kid: str) -> Any:
    """Generate a fresh RSA-2048 keypair with the requested ``kid``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key(
            "RSA",
            2048,
            options={"kid": kid},
            is_private=True,
        )


def _public_jwks(*keys: Any) -> dict[str, list[dict[str, Any]]]:
    """Build a JWKS document containing the public half of each key."""
    return {"keys": [k.as_dict(is_private=False) for k in keys]}


def _mint_token(
    private_key: Any,
    *,
    sub: str = "op-42",
    name: str | None = "Damir Topić",
    email: str | None = "damir@example.com",
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    expires_in: int = 3600,
    not_before_offset: int = 0,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a JWT signed by *private_key*, returning the compact form."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
            "nbf": now + not_before_offset,
        }
        if name is not None:
            payload["name"] = name
        if email is not None:
            payload["email"] = email
        if extra_claims:
            payload.update(extra_claims)
        header = {
            "alg": "RS256",
            "kid": private_key.as_dict()["kid"],
            "typ": "JWT",
        }
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _mock_discovery_and_jwks(
    mock_router: respx.MockRouter,
    jwks: dict[str, Any],
) -> tuple[respx.Route, respx.Route]:
    """Stub the OIDC discovery endpoint and the JWKS endpoint."""
    discovery_route = mock_router.get(_DISCOVERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": _ISSUER,
                "jwks_uri": _JWKS_URL,
            },
        ),
    )
    jwks_route = mock_router.get(_JWKS_URL).mock(
        return_value=httpx.Response(200, json=jwks),
    )
    return discovery_route, jwks_route


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app exposing one verify_jwt-protected route."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, Any]:
        return {
            "sub": operator.sub,
            "name": operator.name,
            "email": operator.email,
            "raw_jwt": operator.raw_jwt,
        }

    return app


# ---------------------------------------------------------------------------
# Header-shape failures (no token reaches verification)
# ---------------------------------------------------------------------------


def test_missing_authorization_header_returns_401() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_non_bearer_authorization_returns_401() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_empty_bearer_token_returns_401() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"Authorization": "Bearer    "})
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_unparseable_bearer_token_returns_401() -> None:
    client = TestClient(_build_app())
    with respx.mock(assert_all_called=False) as mock_router:
        # JWKS is technically reachable; the token itself is garbage.
        _mock_discovery_and_jwks(mock_router, {"keys": []})
        response = client.get(
            "/whoami",
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_jwt_returns_operator_with_claims() -> None:
    """A well-signed token yields an Operator carrying the verified claims."""
    key = _make_rsa_keypair("kid-A")
    jwks = _public_jwks(key)
    token = _mint_token(key, sub="op-1", name="Alice", email="alice@example.com")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, jwks)
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "sub": "op-1",
        "name": "Alice",
        "email": "alice@example.com",
        "raw_jwt": token,
    }


def test_valid_jwt_without_optional_claims_yields_none_fields() -> None:
    """``name`` / ``email`` are optional; absence yields explicit ``None``."""
    key = _make_rsa_keypair("kid-A")
    jwks = _public_jwks(key)
    token = _mint_token(key, sub="op-7", name=None, email=None)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, jwks)
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "op-7"
    assert body["name"] is None
    assert body["email"] is None


# ---------------------------------------------------------------------------
# JWKS cache behaviour
# ---------------------------------------------------------------------------


def test_jwks_cache_hit_avoids_second_fetch() -> None:
    """Two consecutive verifies share one JWKS fetch."""
    key = _make_rsa_keypair("kid-A")
    jwks = _public_jwks(key)
    token = _mint_token(key)

    with respx.mock as mock_router:
        discovery_route, jwks_route = _mock_discovery_and_jwks(mock_router, jwks)
        client = TestClient(_build_app())
        first = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        second = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert discovery_route.call_count == 1
    assert jwks_route.call_count == 1


def test_jwks_cache_miss_after_clear_triggers_refresh() -> None:
    """Explicitly clearing the cache forces the next verify to refetch JWKS."""
    key = _make_rsa_keypair("kid-A")
    jwks = _public_jwks(key)
    token = _mint_token(key)

    with respx.mock as mock_router:
        discovery_route, jwks_route = _mock_discovery_and_jwks(mock_router, jwks)
        client = TestClient(_build_app())
        first = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        clear_jwks_cache()
        second = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert discovery_route.call_count == 2
    assert jwks_route.call_count == 2


def test_kid_rotation_triggers_jwks_refresh_and_succeeds() -> None:
    """A token signed by a kid that's missing from the cached JWKS forces a refetch.

    First verify primes the cache with ``kid-A``. We then mint a token
    against ``kid-B`` and swap the JWKS endpoint to return the
    ``kid-B``-only key set. The dependency must observe the kid miss,
    refresh the cache, and succeed on retry.
    """
    key_a = _make_rsa_keypair("kid-A")
    key_b = _make_rsa_keypair("kid-B")
    token_a = _mint_token(key_a)
    token_b = _mint_token(key_b)

    with respx.mock as mock_router:
        discovery_route, jwks_route = _mock_discovery_and_jwks(
            mock_router,
            _public_jwks(key_a),
        )
        client = TestClient(_build_app())

        # Prime the cache with the kid-A keyset.
        first = client.get("/whoami", headers={"Authorization": f"Bearer {token_a}"})
        assert first.status_code == 200

        # Rotate the JWKS endpoint to return only the kid-B key.
        jwks_route.mock(return_value=httpx.Response(200, json=_public_jwks(key_b)))

        second = client.get("/whoami", headers={"Authorization": f"Bearer {token_b}"})

    assert second.status_code == 200
    # First verify: 1 discovery + 1 jwks. Second verify: cache hit, then
    # kid miss, then forced refresh = 1 more discovery + 1 more jwks.
    assert discovery_route.call_count == 2
    assert jwks_route.call_count == 2


# ---------------------------------------------------------------------------
# Validation failures (sanity check; full coverage in Task #25)
# ---------------------------------------------------------------------------


def test_audience_mismatch_returns_401() -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, audience="wrong-audience")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_issuer_mismatch_returns_401() -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, issuer="https://attacker.test/realms/meho")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_expired_token_returns_401() -> None:
    key = _make_rsa_keypair("kid-A")
    # expires_in negative → exp is in the past, beyond the leeway window
    token = _mint_token(key, expires_in=-600)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_tampered_signature_returns_401() -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key)
    # Flip the last 10 chars of the signature segment.
    head, _, tail = token.rpartition(".")
    tampered = f"{head}.{'A' * len(tail)}"

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {tampered}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


# ---------------------------------------------------------------------------
# JWKS unreachable
# ---------------------------------------------------------------------------


def test_jwks_unreachable_returns_401() -> None:
    """When discovery / JWKS fetch fails the dependency yields 401.

    The token itself is well-formed; only the network is broken. We
    distinguish ``jwks_unavailable`` from ``invalid_token`` so operators
    chasing 401s can tell whether they're looking at a credential issue
    or a dependency issue.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key)

    with respx.mock as mock_router:
        mock_router.get(_DISCOVERY_URL).mock(return_value=httpx.Response(503))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


def test_readiness_probe_passes_when_jwks_fetchable() -> None:
    key = _make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        result = keycloak_readiness_probe()

    assert result.name == "keycloak"
    assert result.ok is True
    assert result.detail == "jwks_fetched"


def test_readiness_probe_fails_when_discovery_unreachable() -> None:
    with respx.mock as mock_router:
        mock_router.get(_DISCOVERY_URL).mock(return_value=httpx.Response(503))
        result = keycloak_readiness_probe()

    assert result.name == "keycloak"
    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("jwks_fetch_failed:")


def test_readiness_probe_fails_when_jwks_malformed() -> None:
    with respx.mock as mock_router:
        mock_router.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL},
            ),
        )
        mock_router.get(_JWKS_URL).mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"}),
        )
        result = keycloak_readiness_probe()

    assert result.ok is False
    assert result.detail == "jwks_malformed"


# ---------------------------------------------------------------------------
# Regression: malformed claims, secret-leak, contract-coupling
# (B1 / B2 / M1 from PR #151 review iter-1)
# ---------------------------------------------------------------------------


def test_malformed_email_claim_returns_401() -> None:
    """B1 regression: a signature-valid JWT carrying a malformed ``email``
    claim must reject as 401 ``invalid_token`` — never as a 500.

    The Operator model uses pydantic's ``EmailStr`` validator; if the
    Keycloak realm is misconfigured and emits ``"not-an-email"``,
    pydantic raises ``ValidationError`` during ``Operator(...)``
    construction. The dependency must catch that and return 401, not
    propagate the exception as an unhandled 500.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-evil", email="not-an-email")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_operator_repr_does_not_leak_raw_jwt() -> None:
    """B2 regression: ``repr(Operator(...))`` must not contain the bearer
    token string, and must not even mention ``raw_jwt`` as a field name.

    structlog (wired in Task #24) calls ``repr()`` on bound non-primitive
    values when emitting JSON; an unrestricted default repr would dump
    every operator's full bearer token to stdout / log shippers. The
    field is excluded via ``Field(repr=False)``.
    """
    fake_token = "header.payload.signature-very-secret"
    op = Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt=fake_token,
    )

    text = repr(op)
    assert fake_token not in text, f"raw_jwt value leaked into repr: {text!r}"
    assert "raw_jwt" not in text, f"raw_jwt field name leaked into repr: {text!r}"
    # The field is still populated and accessible by name — we only
    # sanitised the default representation.
    assert op.raw_jwt == fake_token


def test_value_error_key_not_found_triggers_jwks_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 regression: any ``ValueError`` from the JWKS-decode helper
    triggers a single refresh-and-retry — independent of the message
    string.

    The kid-rotation contract used to depend on string-matching
    authlib's ``"Key not found"`` ValueError; an authlib version bump
    could silently change that message and break rotation. The fix
    drops the string match. To prove the new contract, we monkey-patch
    ``_decode_with_jwks`` to raise a ``ValueError`` with an *unrelated*
    message on the first call and to delegate to the real implementation
    on the second — and assert (a) the JWKS endpoint was hit twice
    (initial + forced refresh) and (b) the verify ultimately succeeds.
    """
    from meho_backplane.auth import jwt as jwt_module

    key = _make_rsa_keypair("kid-A")
    jwks = _public_jwks(key)
    token = _mint_token(key)

    real_decode = jwt_module._decode_with_jwks
    call_count = {"n": 0}

    def fake_decode(tok: str, ks: dict[str, Any], settings: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Message intentionally unrelated to "Key not found" — the
            # fix must refresh on *any* ValueError.
            raise ValueError("some opaque authlib internal message")
        return real_decode(tok, ks, settings)

    monkeypatch.setattr(jwt_module, "_decode_with_jwks", fake_decode)

    with respx.mock as mock_router:
        discovery_route, jwks_route = _mock_discovery_and_jwks(mock_router, jwks)
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    # Initial decode raised ValueError -> forced refresh -> retry succeeds.
    assert call_count["n"] == 2
    # The forced refresh re-hits both discovery and JWKS.
    assert discovery_route.call_count == 2
    assert jwks_route.call_count == 2
