# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the GitHub session module (G3.11-T1 #1221).

Covers the four T11-compliant error envelopes named in the issue body:

* ``github_app_not_installed`` — 404 from the installation-token mint.
* ``github_jwt_mint_failed`` — PyJWT cannot sign (malformed PEM, etc.).
* ``github_installation_token_mint_failed`` — non-2xx, non-404 on mint.
* ``github_rate_limited`` — ``X-RateLimit-Remaining: 0`` on a 403 / 429.

Plus the happy path: JWT mint with a real RSA key, installation-token
exchange + cache validity, and the PAT fallback shape.
"""

from __future__ import annotations

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from meho_backplane.connectors.github.session import (
    DEFAULT_GITHUB_API_URL,
    INSTALLATION_TOKEN_CACHE_SECONDS,
    JWT_TTL_SECONDS,
    GitHubAppNotInstalledError,
    GitHubInstallationTokenMintError,
    GitHubJWTMintError,
    GitHubRateLimitedError,
    exchange_jwt_for_installation_token,
    mint_github_app_jwt,
)


def _generate_rsa_pem() -> str:
    """Build a fresh 2048-bit RSA key in PEM (PKCS8) so ``jwt.encode`` succeeds.

    Using a real key (rather than a hard-coded fixture) keeps the test
    immune to cryptography-library API changes; the key never leaves the
    test process.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("ascii")


# ---------------------------------------------------------------------------
# mint_github_app_jwt
# ---------------------------------------------------------------------------


def test_mint_github_app_jwt_returns_decodable_rs256_token() -> None:
    """Happy path — encoded JWT carries the documented claims + algorithm."""
    pem = _generate_rsa_pem()
    now = 1_700_000_000.0
    token = mint_github_app_jwt("123456", pem, now=now)
    # Decode without signature verification — we only assert claim shape.
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "123456"
    assert decoded["iat"] == int(now)
    # exp - iat must equal the documented TTL; GitHub enforces ≤10 min.
    assert decoded["exp"] - decoded["iat"] == JWT_TTL_SECONDS
    # Header advertises RS256 (the GitHub-mandated algorithm).
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"


def test_mint_github_app_jwt_raises_t11_envelope_on_malformed_pem() -> None:
    """Malformed PEM → :class:`GitHubJWTMintError` with the T11 code prefix."""
    with pytest.raises(GitHubJWTMintError) as excinfo:
        mint_github_app_jwt("123", "not-a-pem")
    assert excinfo.value.code == "github_jwt_mint_failed"
    msg = str(excinfo.value)
    assert "github_jwt_mint_failed" in msg
    # T11 three-clause rule: app_id (operator-known) is named, the PEM is not.
    assert "'123'" in msg
    assert "docs/cross-repo/github-app-credential.md" in msg


# ---------------------------------------------------------------------------
# exchange_jwt_for_installation_token — happy path + cache shape
# ---------------------------------------------------------------------------


@respx.mock
async def test_exchange_jwt_returns_installation_token_with_monotonic_expiry() -> None:
    """Happy path — 201 mint response yields a cached token + expiry."""
    route = respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_installtoken_redacted",
                "expires_at": "2026-05-27T13:00:00Z",
                "permissions": {"contents": "read"},
            },
        )
    )
    now_monotonic = 12345.0
    result = await exchange_jwt_for_installation_token(
        jwt_token="signed.jwt.value",
        installation_id="42",
        now=now_monotonic,
    )
    assert result.token == "ghs_installtoken_redacted"
    assert result.upstream_expires_at == "2026-05-27T13:00:00Z"
    assert result.expires_at_monotonic == now_monotonic + INSTALLATION_TOKEN_CACHE_SECONDS
    assert route.called
    # Request carries the Bearer JWT + the documented Accept + API-version headers.
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer signed.jwt.value"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# T11 envelopes: 404, rate-limit, non-2xx
# ---------------------------------------------------------------------------


@respx.mock
async def test_exchange_jwt_raises_app_not_installed_on_404() -> None:
    """404 → :class:`GitHubAppNotInstalledError` with the T11 code."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/99/access_tokens",
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubAppNotInstalledError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="99",
        )
    assert excinfo.value.code == "github_app_not_installed"
    msg = str(excinfo.value)
    assert "github_app_not_installed" in msg
    assert "'99'" in msg
    assert "docs/cross-repo/github-app-credential.md" in msg


@respx.mock
async def test_exchange_jwt_raises_rate_limited_when_remaining_is_zero() -> None:
    """403 + ``X-RateLimit-Remaining: 0`` → :class:`GitHubRateLimitedError`."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1700001234",
            },
            json={"message": "API rate limit exceeded"},
        )
    )
    with pytest.raises(GitHubRateLimitedError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert excinfo.value.code == "github_rate_limited"
    assert excinfo.value.reset_at == 1700001234
    msg = str(excinfo.value)
    assert "github_rate_limited" in msg
    assert "X-RateLimit-Remaining: 0" in msg


@respx.mock
async def test_exchange_jwt_does_not_treat_non_zero_remaining_403_as_rate_limit() -> None:
    """403 without ``X-RateLimit-Remaining: 0`` → mint-failed (not rate-limit)."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "4999"},
            json={"message": "Resource not accessible"},
        )
    )
    with pytest.raises(GitHubInstallationTokenMintError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert excinfo.value.code == "github_installation_token_mint_failed"
    assert excinfo.value.status_code == 403


@respx.mock
async def test_exchange_jwt_raises_mint_failed_on_500() -> None:
    """5xx → :class:`GitHubInstallationTokenMintError`."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(GitHubInstallationTokenMintError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert excinfo.value.code == "github_installation_token_mint_failed"
    assert excinfo.value.status_code == 500
    msg = str(excinfo.value)
    assert "github_installation_token_mint_failed" in msg


@respx.mock
async def test_exchange_jwt_raises_mint_failed_on_transport_error() -> None:
    """Network failure → :class:`GitHubInstallationTokenMintError` (status_code=None)."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(side_effect=httpx.ConnectError("DNS failure"))
    with pytest.raises(GitHubInstallationTokenMintError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert excinfo.value.status_code is None
    assert "transport failure" in str(excinfo.value)


@respx.mock
async def test_exchange_jwt_raises_mint_failed_when_token_missing_from_response() -> None:
    """Malformed 2xx (no ``token`` key) → :class:`GitHubInstallationTokenMintError`."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(return_value=httpx.Response(201, json={"expires_at": "2026-05-27"}))
    with pytest.raises(GitHubInstallationTokenMintError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert "missing a 'token' field" in str(excinfo.value)
