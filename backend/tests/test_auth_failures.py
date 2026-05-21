# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Comprehensive JWT failure-mode tests (Task #25 — JWT half).

This module proves :func:`meho_backplane.auth.jwt.verify_jwt` rejects
every adversarial input shape with a structured 401 — never a 500, never
a leaked claim value, never a 200 with a bypass. Each row in the issue
body's failure-mode table maps to one or more tests below.

Failure modes covered (rows from issue body, JWT half).

Most decode-stage failures now surface a *specific* code per G0.9.1-T12
(Initiative #772 / Task #797). The diagnostic value (expected audience,
expected issuer, claim name, exception class) lives in the structlog
event — never in the unauthenticated 401 body — mirroring the existing
``malformed_tenant_claim`` body-vs-log split.

* Missing ``Authorization`` header → 401 ``missing_token``
* ``Authorization`` without ``Bearer `` prefix → 401 ``missing_token``
* Unparseable JWT (random bytes) → 401 ``invalid_token`` (structural)
* JWT signed by an unknown key → 401 ``signature_verification_failed``
* JWT with a tampered signature → 401 ``signature_verification_failed``
* JWT signed under a tampered payload → 401 ``signature_verification_failed``
* JWT expired (``exp`` in the past, beyond leeway) → 401 ``token_expired``
* JWT not yet valid (``nbf`` in the future, beyond leeway) → 401
  ``token_not_yet_valid``
* JWT with wrong audience (``aud`` != configured) → 401 ``invalid_audience``
* JWT with wrong issuer (``iss`` != configured) → 401 ``invalid_issuer``
* JWT missing required claim (``sub``) → 401 ``missing_sub``
* JWT with the wrong algorithm (``HS256`` when only ``RS256`` accepted)
  → 401 ``invalid_token`` (structural — authlib rejects pre-claims)
* JWT with the ``none`` algorithm → 401 ``invalid_token`` (structural)
* JWT with a missing ``kid`` header → 401 ``invalid_token``
* JWT with a ``kid`` that doesn't exist in the JWKS even after refresh
  → 401 ``invalid_token`` (and exactly one forced JWKS refresh ran)
* JWKS endpoint unreachable → 401 ``jwks_unavailable``
* JWKS endpoint returns malformed body → 401 ``jwks_unavailable``

Each test asserts:

1. The HTTP status code.
2. The exact ``{"detail": "<reason>"}`` body shape.
3. No leaked exception message — the reason string is one of the
   centrally-defined tokens (``missing_token`` / ``invalid_token`` /
   ``invalid_audience`` / ``invalid_issuer`` / ``missing_sub`` /
   ``token_expired`` / ``token_not_yet_valid`` /
   ``signature_verification_failed`` / ``jwks_unavailable``).

Test isolation re-uses the fixture pattern Task #22 established: respx
intercepts the OIDC discovery + JWKS HTTP calls, RSA fixture keys mint
test JWTs locally. The ``conftest.py`` autouse sweep guarantees no
captured-log line carries the bearer token across the entire suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_DISCOVERY_URL,
    DEFAULT_ISSUER,
    DEFAULT_JWKS_URL,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var the Settings model reads and reset the cache.

    The Vault knobs are populated even though this file does not exercise
    Vault — :class:`Settings` validates them at construction time and
    every code path that reaches :func:`get_settings` (including
    :func:`verify_jwt`) needs them present.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Reset the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app exposing one verify_jwt-protected route.

    The route returns the operator dict; its body never runs in any
    failure test (the dependency raises before reaching it).
    """
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


def test_missing_authorization_header() -> None:
    """No ``Authorization`` header → 401 ``missing_token``."""
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_authorization_without_bearer_prefix_returns_401() -> None:
    """``Authorization: Basic ...`` is rejected as ``missing_token``.

    The dependency does not attempt to decode anything that does not
    start with the literal string ``Bearer ``; this is the contract
    Task #22 pinned and the security review depends on.
    """
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_bearer_prefix_with_only_whitespace_returns_401() -> None:
    """``Authorization: Bearer    `` (no token) → ``missing_token``."""
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"Authorization": "Bearer    "})
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


# ---------------------------------------------------------------------------
# Unparseable / structurally-broken tokens
# ---------------------------------------------------------------------------


def test_unparseable_random_bytes_returns_invalid_token() -> None:
    """A random-bytes ``Bearer`` value rejects as ``invalid_token``.

    The JWKS itself is reachable (so ``jwks_unavailable`` is the wrong
    discriminant); the failure has to come from authlib's decode pass.
    """
    client = TestClient(_build_app())
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, {"keys": []})
        response = client.get(
            "/whoami",
            headers={"Authorization": "Bearer total-garbage-not-a-jwt-at-all"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_two_segment_token_returns_invalid_token() -> None:
    """A two-segment ``Bearer`` value (no signature) rejects as ``invalid_token``.

    JWS compact serialisation has exactly three dot-separated segments.
    A two-segment ``header.payload`` string is structurally invalid;
    authlib's decoder raises and the dependency must surface 401.
    """
    client = TestClient(_build_app())
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, {"keys": []})
        response = client.get(
            "/whoami",
            headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


# ---------------------------------------------------------------------------
# Signature failures
# ---------------------------------------------------------------------------


def test_token_signed_by_unknown_key_returns_signature_verification_failed() -> None:
    """A JWT signed by a key NOT in the JWKS surfaces ``signature_verification_failed``.

    Mints a token under a fresh keypair, then publishes a JWKS that
    contains a *different* key with the same kid. authlib's signature
    verification must reject; the dependency must surface 401 with the
    specific G0.9.1-T12 code (was ``invalid_token`` pre-T12).
    """
    signing_key = make_rsa_keypair("kid-A")
    published_key = make_rsa_keypair("kid-A")  # same kid, different key material
    token = mint_token(signing_key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(published_key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "signature_verification_failed"}


def test_tampered_signature_returns_signature_verification_failed() -> None:
    """Flipping bytes in the signature segment surfaces ``signature_verification_failed``.

    The header + payload claim values are all valid; only the signature
    bytes are corrupted. Verifies the signature-check step actually
    runs — a buggy implementation that trusted the header would 200.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    head, _, tail = token.rpartition(".")
    tampered = f"{head}.{'A' * len(tail)}"

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {tampered}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "signature_verification_failed"}


def test_tampered_payload_returns_signature_verification_failed() -> None:
    """Modifying a payload byte (without resigning) surfaces ``signature_verification_failed``.

    Different attack from a tampered signature: here an attacker tries
    to pretend they are a different ``sub`` while keeping the original
    signature. The signature no longer matches the modified payload
    and verification fails.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, sub="op-1")
    parts = token.split(".")
    assert len(parts) == 3
    # Flip a byte deep inside the payload segment — keep the structural
    # framing valid so authlib reaches the signature check rather than
    # bailing out on a base64 decode error first.
    payload_seg = parts[1]
    if len(payload_seg) >= 6:
        flipped = (
            payload_seg[: len(payload_seg) // 2]
            + ("A" if payload_seg[len(payload_seg) // 2] != "A" else "B")
            + payload_seg[len(payload_seg) // 2 + 1 :]
        )
        parts[1] = flipped
    tampered = ".".join(parts)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {tampered}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "signature_verification_failed"}


# ---------------------------------------------------------------------------
# Claim-value failures
# ---------------------------------------------------------------------------


def test_expired_token_beyond_leeway_returns_token_expired() -> None:
    """``exp`` in the past, beyond the configured leeway → ``token_expired``.

    Default leeway is 30s; ``expires_in=-600`` puts the token 10 minutes
    in the past — well outside the tolerance window. G0.9.1-T12 promotes
    this from the opaque ``invalid_token`` v0.3.1 surfaced.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, expires_in=-600)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "token_expired"}


def test_not_yet_valid_token_beyond_leeway_returns_token_not_yet_valid() -> None:
    """``nbf`` in the future beyond leeway → ``token_not_yet_valid``.

    ``not_before_offset=600`` puts ``nbf`` 10 minutes ahead of now,
    again well past the 30-second leeway window. Captures the symmetric
    side of the clock-skew defence. Surfaced as ``token_not_yet_valid``
    (a separate code from ``token_expired`` because the remediation is
    different — clock-skew on the *issuing* side rather than rotation).
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, not_before_offset=600)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "token_not_yet_valid"}


def test_wrong_audience_returns_invalid_audience() -> None:
    """``aud`` mismatch (not the configured client id) → ``invalid_audience``.

    Defends against tokens minted for a different OIDC client in the
    same Keycloak realm — they would be cryptographically valid but
    must not authorise this backplane. G0.9.1-T12 promotes this from
    the opaque ``invalid_token`` v0.3.1 surfaced (consumer Addendum II
    Wall #2).
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, audience="some-other-client")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_audience"}


def test_wrong_issuer_returns_invalid_issuer() -> None:
    """``iss`` mismatch (not the configured Keycloak realm) → ``invalid_issuer``.

    Defends against an attacker presenting a token from a different
    realm even if its signature happens to validate against the
    cached JWKS by accident.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, issuer="https://attacker.test/realms/meho")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_issuer"}


def test_missing_sub_claim_returns_missing_sub() -> None:
    """A token without ``sub`` → ``missing_sub`` (G0.9.1-T12).

    OIDC core §2 / RFC 9068 §2.2.1 make ``sub`` REQUIRED on access
    tokens. The decoder now marks ``sub`` as essential so authlib
    surfaces the failure as a ``MissingClaimError`` during decode and
    :func:`_classify_decode_error` returns the specific ``missing_sub``
    code instead of letting the claim drop through to the generic
    fallback (consumer Addendum II Wall #3).
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, omit_sub=True)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "missing_sub"}


# ---------------------------------------------------------------------------
# Algorithm-confusion attacks
# ---------------------------------------------------------------------------


def test_hs256_algorithm_returns_invalid_token() -> None:
    """A JWT minted with ``alg: HS256`` is rejected when only RS256 is accepted.

    This is the canonical algorithm-confusion attack (CVE-2016-10555 /
    auth0/node-jsonwebtoken family): an attacker who can read the
    public key tries to convince the verifier to accept HMAC-with-the-
    public-key. The fix is to pin the accepted algorithm list at the
    decoder (which Task #22 does — ``_ACCEPTED_ALGORITHMS = ('RS256',)``).
    The test signs with a symmetric secret and presents the resulting
    HS256 token; the verify_jwt decoder must reject.
    """
    key_rsa = make_rsa_keypair("kid-A")
    # Mint a HS256 token with the RSA's kid so the decoder pulls the
    # right key from JWKS — but the alg-mismatch must trip first.
    hs_token = mint_token(
        b"some-symmetric-secret-bytes",
        kid="kid-A",
        algorithm="HS256",
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key_rsa))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {hs_token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


def test_none_algorithm_returns_invalid_token() -> None:
    """A JWT minted with ``alg: none`` (no signature) is rejected.

    The ``none`` algorithm is the original JWT footgun: a verifier that
    honours it accepts any unsigned token as authentic. authlib's
    ``JsonWebToken(['RS256'])`` decoder must refuse to dispatch to the
    none-handler; the dependency surfaces 401.
    """
    key = make_rsa_keypair("kid-A")
    none_token = mint_token("", algorithm="none")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {none_token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}


# ---------------------------------------------------------------------------
# Kid-rotation failure paths
# ---------------------------------------------------------------------------


def test_kid_not_in_jwks_after_refresh_returns_invalid_token() -> None:
    """A ``kid`` that the JWKS does not list — even after a forced refresh —
    rejects as ``invalid_token`` and triggers exactly one extra JWKS fetch.

    Pins the bounded-retry contract from Task #22: the dependency may
    refresh JWKS once on a kid miss but must not infinite-loop if the
    miss persists.

    Construction: sign the token with ``signing_key`` whose public half
    is *never* published, then header-stamp the token with a ``kid`` that
    also isn't in the JWKS. Publish a JWKS containing only ``other_key``
    so the keyset cannot resolve either by kid lookup or by trial decode.
    authlib raises :class:`ValueError`, which the dependency interprets
    as a kid miss → forced JWKS refresh → second miss → 401.
    """
    signing_key = make_rsa_keypair("kid-S")
    other_key = make_rsa_keypair("kid-O")
    # Sign with kid-S but advertise an unknown kid in the header.
    token = mint_token(signing_key, kid="kid-not-in-jwks")

    with respx.mock as mock_router:
        # JWKS only carries kid-O; neither the token's advertised kid
        # nor the actual signing key is present.
        discovery_route, jwks_route = mock_discovery_and_jwks(
            mock_router,
            public_jwks(other_key),
        )
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}
    # Initial JWKS fetch + exactly one forced refresh = 2 hits each.
    assert discovery_route.call_count == 2
    assert jwks_route.call_count == 2


# ---------------------------------------------------------------------------
# JWKS unreachable / malformed
# ---------------------------------------------------------------------------


def test_discovery_unreachable_returns_jwks_unavailable() -> None:
    """OIDC discovery 5xx maps to ``jwks_unavailable`` (separate from
    invalid_token so operators can tell credential vs dependency apart)."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)

    with respx.mock as mock_router:
        mock_router.get(DEFAULT_DISCOVERY_URL).mock(
            return_value=httpx.Response(503),
        )
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}


def test_jwks_endpoint_unreachable_returns_jwks_unavailable() -> None:
    """JWKS endpoint 5xx (after discovery succeeded) → ``jwks_unavailable``."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)

    with respx.mock as mock_router:
        mock_router.get(DEFAULT_DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={"issuer": DEFAULT_ISSUER, "jwks_uri": DEFAULT_JWKS_URL},
            ),
        )
        mock_router.get(DEFAULT_JWKS_URL).mock(return_value=httpx.Response(502))
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}


def test_jwks_endpoint_returns_malformed_json() -> None:
    """JWKS body without a ``keys`` array → ``jwks_unavailable``.

    Captures the contract that JWKS coercion checks the document shape;
    a 200 response that isn't a valid JWKS must not silently bypass
    the cache layer's expectations.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)

    with respx.mock as mock_router:
        mock_router.get(DEFAULT_DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={"issuer": DEFAULT_ISSUER, "jwks_uri": DEFAULT_JWKS_URL},
            ),
        )
        mock_router.get(DEFAULT_JWKS_URL).mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"}),
        )
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}


def test_discovery_doc_missing_jwks_uri_returns_jwks_unavailable() -> None:
    """A discovery document without ``jwks_uri`` → ``jwks_unavailable``.

    Pins the validation Task #22's :func:`_resolve_jwks_uri` performs:
    a 200 response from Keycloak that omits the field is treated as a
    dependency failure, never as an opportunity to skip JWKS verification.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)

    with respx.mock as mock_router:
        mock_router.get(DEFAULT_DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={"issuer": DEFAULT_ISSUER},  # ``jwks_uri`` deliberately absent
            ),
        )
        client = TestClient(_build_app())
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}
