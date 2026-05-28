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
    # ``iat`` is backdated by 60s per GitHub's documented JWT recipe
    # to absorb forward clock skew. See docs URL on
    # mint_github_app_jwt.
    assert decoded["iat"] == int(now) - 60
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


@respx.mock
async def test_exchange_jwt_raises_mint_failed_on_non_json_2xx_body() -> None:
    """Non-JSON 2xx body → :class:`GitHubInstallationTokenMintError`.

    No raw ``ValueError`` escapes — the failure must travel through the
    documented T11 envelope shape.
    """
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            201,
            text="<html><body>upstream proxy returned HTML</body></html>",
        )
    )
    with pytest.raises(GitHubInstallationTokenMintError) as excinfo:
        await exchange_jwt_for_installation_token(
            jwt_token="signed.jwt.value",
            installation_id="42",
        )
    assert "non-JSON 2xx body" in str(excinfo.value)
    assert excinfo.value.status_code == 201
    # The original ``ValueError`` is preserved as the chained cause.
    assert isinstance(excinfo.value.__cause__, ValueError)


# ---------------------------------------------------------------------------
# load_github_credentials_from_vault — G0.16-T2 #1304 payload-shape discriminator
# ---------------------------------------------------------------------------
#
# These tests pin the post-G0.16-T2 contract that replaces the historical
# ``auth_model``-driven routing: the connector reads the Vault secret
# once and picks the upstream credential protocol from the field shape.
# The target's ``auth_model`` is no longer the discriminator; the helper
# does not inspect it (the boundary check lives on
# :meth:`GitHubRestConnector.auth_headers`).


from collections.abc import Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402  — kept local; only loader tests need it
from uuid import UUID  # noqa: E402

from meho_backplane.auth.operator import Operator, TenantRole  # noqa: E402
from meho_backplane.connectors.github.session import (  # noqa: E402
    GitHubAmbiguousVaultPayloadError,
    GitHubAppCredentials,
    GitHubPATCredentials,
    load_github_credentials_from_vault,
)
from meho_backplane.settings import get_settings  # noqa: E402

# Local helper reuses the same Vault fake the vmware-rest auth tests use.
from tests._vault_fakes import install_fake_client  # noqa: E402


@pytest.fixture(autouse=True)
def _gh_loader_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time.

    Mirrors :file:`test_connectors_vmware_rest_auth.py` — the loader's
    :func:`vault_client_for_operator` eagerly reads ``KEYCLOAK_*`` /
    ``VAULT_*`` via :func:`get_settings`, so the fixture pins values
    before the cache is built and clears the cache on teardown.
    Confined to this module's loader tests because the JWT-mint /
    installation-token-exchange tests above don't reach the settings
    surface (they unit-test the wire format with respx only).
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass(frozen=True)
class _GhFakeTarget:
    """Minimum :class:`GitHubTargetLike` shape the loader reads."""

    name: str
    host: str = "api.github.com"
    secret_ref: str | None = "targets/github/evoila-bosnia-gh"
    auth_model: str | None = "shared_service_account"
    port: int | None = None


def _operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


_GH_PEM_FIXTURE = _generate_rsa_pem()


@pytest.mark.asyncio
async def test_load_github_credentials_picks_app_path_on_app_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault payload carrying ``app_id`` + ``private_key`` + ``installation_id`` → App."""
    install_fake_client(
        monkeypatch,
        secret={
            "app_id": "3898656",
            "private_key": _GH_PEM_FIXTURE,
            "installation_id": "136396725",
        },
    )

    creds = await load_github_credentials_from_vault(_GhFakeTarget(name="t"), _operator())

    assert isinstance(creds, GitHubAppCredentials)
    assert creds.app_id == "3898656"
    assert creds.installation_id == "136396725"
    assert creds.private_key_pem == _GH_PEM_FIXTURE


@pytest.mark.asyncio
async def test_load_github_credentials_picks_pat_path_on_pat_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault payload carrying only ``token`` → PAT."""
    install_fake_client(monkeypatch, secret={"token": "ghp_fine_grained_pat_value"})

    creds = await load_github_credentials_from_vault(_GhFakeTarget(name="t"), _operator())

    assert isinstance(creds, GitHubPATCredentials)
    assert creds.token == "ghp_fine_grained_pat_value"


@pytest.mark.asyncio
async def test_load_github_credentials_prefers_app_when_both_shapes_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator wrote both shapes — App wins (the documented preferred path)."""
    install_fake_client(
        monkeypatch,
        secret={
            "app_id": "3898656",
            "private_key": _GH_PEM_FIXTURE,
            "installation_id": "136396725",
            "token": "ghp_should_be_ignored",
        },
    )

    creds = await load_github_credentials_from_vault(_GhFakeTarget(name="t"), _operator())

    assert isinstance(creds, GitHubAppCredentials)


@pytest.mark.asyncio
async def test_load_github_credentials_raises_ambiguous_envelope_when_neither_shape_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault payload missing every documented field → typed structured error."""
    install_fake_client(
        monkeypatch,
        secret={
            # Partial App shape — has the App ID but neither the
            # private_key nor the installation_id, so the App branch
            # does not match (the helper requires all three).
            "app_id": "3898656",
        },
    )

    with pytest.raises(GitHubAmbiguousVaultPayloadError) as excinfo:
        await load_github_credentials_from_vault(_GhFakeTarget(name="t"), _operator())

    msg = str(excinfo.value)
    assert excinfo.value.code == "github_ambiguous_vault_payload"
    # Names the target so operators can find the misconfigured row.
    assert "'t'" in msg
    # Names the required shape for each path so the operator knows
    # which field set to populate.
    assert "app_id" in msg
    assert "private_key" in msg
    assert "installation_id" in msg
    assert "'token'" in msg
    # Names the field(s) present (App ID only here) so the operator can
    # see what they wrote vs what's required, without echoing values.
    assert "['app_id']" in msg


@pytest.mark.asyncio
async def test_load_github_credentials_fails_closed_on_empty_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """System-initiated call (empty raw_jwt) errors before touching Vault.

    Inherits :func:`load_vault_secret_data`'s fail-closed contract —
    confirms the gh-rest loader does not silently fall back to a
    backplane identity on a missing operator JWT.
    """
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

    fake = install_fake_client(monkeypatch, secret={"token": "ghp_x"})

    with pytest.raises(VaultCredentialsReadError):
        await load_github_credentials_from_vault(_GhFakeTarget(name="t"), _operator(raw_jwt=""))

    # Vault was never touched.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
