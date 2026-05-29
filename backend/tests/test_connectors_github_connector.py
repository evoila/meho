# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`GitHubRestConnector` (G3.11-T1 #1221).

Coverage:

* **Dual registration** — the package registers both the v1 wildcard
  ``("gh", "", "")`` and the v2 versioned ``("gh", "3", "gh-rest")``
  entries per G0.15-T6.
* **Installation-token caching** — a second :meth:`fingerprint` call
  within the 50-minute window does NOT re-mint the JWT or call the
  installation-token endpoint (the acceptance-criteria assertion).
* **PAT fallback** — a ``GitHubPATCredentials`` loader return (chosen
  by the Vault-payload-shape discriminator G0.16-T2 #1304 introduced)
  reads a Vault-stored token and skips the JWT exchange entirely.
* **Auth-model gating** — ``target.auth_model`` accepts
  ``shared_service_account`` (and ``None`` for legacy rows); anything
  else (``per_user``, ``impersonation``) raises
  :exc:`NotImplementedError` at :meth:`auth_headers`.
* **Fingerprint shape** — the ``GET /user/installations`` and
  ``GET /repos/{owner}/{repo}`` paths populate the documented
  ``extras`` fields.
* **T11 error envelopes** — a 404 / 403 rate-limit / 500 / bad PEM
  surface as ``reachable=False`` with the structured ``error_code``.

The test seam uses an injected ``credentials_loader`` returning canned
:class:`GitHubAppCredentials` / :class:`GitHubPATCredentials` so the
tests never touch Vault — same shape :class:`VmwareRestConnector` tests
use.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github import (
    DEFAULT_GITHUB_API_URL,
    GitHubAppCredentials,
    GitHubPATCredentials,
    GitHubRestConnector,
    GitHubTargetLike,
)
from meho_backplane.connectors.registry import (
    all_connectors,
    all_connectors_v2,
)
from meho_backplane.connectors.schemas import AuthModel


def _generate_rsa_pem() -> str:
    """Same helper as :mod:`test_connectors_github_session` — fresh 2048-bit RSA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Minimal :class:`Operator` for tests; non-empty placeholder JWT."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass(frozen=True)
class _FakeTarget:
    """Structural :class:`GitHubTargetLike` for the test seam.

    ``auth_model`` defaults to :data:`AuthModel.SHARED_SERVICE_ACCOUNT`'s
    string value — the only model the gh-rest connector accepts at
    its boundary after G0.16-T2 (#1304). The App-vs-PAT discriminator
    lives in the injected loader's return type, not on the target.
    """

    name: str
    host: str = "api.github.com"
    port: int | None = None
    secret_ref: str | None = "targets/github/test"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


@pytest.fixture
def app_pem() -> str:
    """Per-test RSA PEM so signed JWTs round-trip through PyJWT."""
    return _generate_rsa_pem()


@pytest.fixture
def app_creds(app_pem: str) -> GitHubAppCredentials:
    return GitHubAppCredentials(
        app_id="100000",
        private_key_pem=app_pem,
        installation_id="42",
    )


@pytest.fixture
def operator() -> Operator:
    return _make_operator()


@pytest.fixture
def connector_with_app_loader(app_creds: GitHubAppCredentials) -> Iterator[GitHubRestConnector]:
    """Connector with an injected loader that returns the App creds."""

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return app_creds

    conn = GitHubRestConnector(credentials_loader=loader)
    yield conn


# ---------------------------------------------------------------------------
# Dual registration (G0.15-T6 mandatory pattern)
# ---------------------------------------------------------------------------


def test_github_connector_registers_v1_wildcard_and_v2_versioned() -> None:
    """The package import wires BOTH ``("gh", "", "")`` and ``("gh", "3", "gh-rest")``."""
    # Force import; safe to call multiple times — duplicate-registration
    # would raise at import time, not on this call.
    import meho_backplane.connectors.github  # noqa: F401

    v1 = all_connectors()
    v2 = all_connectors_v2()

    assert v1.get("gh") is GitHubRestConnector
    assert v2.get(("gh", "", "")) is GitHubRestConnector
    assert v2.get(("gh", "3", "gh-rest")) is GitHubRestConnector


def test_github_connector_class_metadata() -> None:
    """Class-level attributes match the documented registry triple."""
    assert GitHubRestConnector.product == "gh"
    # Registry version is "3" (digit-prefix shape the connector-id parser
    # requires); the GitHub-API-side label "v3" lives in docs / fingerprint
    # extras only.
    assert GitHubRestConnector.version == "3"
    assert GitHubRestConnector.impl_id == "gh-rest"
    assert GitHubRestConnector.priority == 1


# ---------------------------------------------------------------------------
# Installation-token caching (acceptance criterion)
# ---------------------------------------------------------------------------


@respx.mock
async def test_second_fingerprint_does_not_remint_within_cache_window(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """A second fingerprint within 50 minutes reuses the cached token.

    Asserts:
    * The installation-token mint endpoint is hit **exactly once**.
    * The ``GET /user/installations`` endpoint is hit twice (one per
      fingerprint call) — only the token itself caches; the
      installation list re-probes on demand.
    """
    mint_route = respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_installtoken_redacted",
                "expires_at": "2099-01-01T00:00:00Z",
                "permissions": {"contents": "read"},
            },
        )
    )
    installations_route = respx.get(
        f"{DEFAULT_GITHUB_API_URL}/user/installations",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "installations": [
                    {
                        "id": 42,
                        "app_slug": "meho-gh-app",
                        "target_type": "Organization",
                        "account": {"login": "evoila"},
                        "permissions": {"contents": "read"},
                    }
                ],
            },
        )
    )

    target = _FakeTarget(name="github-main")

    fp1 = await connector_with_app_loader.fingerprint(target)
    fp2 = await connector_with_app_loader.fingerprint(target)

    assert fp1.reachable is True
    assert fp2.reachable is True
    # Token mint runs exactly once across both fingerprints.
    assert mint_route.call_count == 1
    # The /user/installations probe runs per-fingerprint (no caching of
    # the wire response itself — only the token caches).
    assert installations_route.call_count == 2


# ---------------------------------------------------------------------------
# PAT fallback path
# ---------------------------------------------------------------------------


@respx.mock
async def test_pat_fallback_path_skips_jwt_exchange_entirely() -> None:
    """A ``GitHubPATCredentials`` loader return reads a PAT and never calls the mint endpoint.

    The target keeps ``auth_model="shared_service_account"`` — the
    App-vs-PAT discriminator is the loader's return *type*, not the
    target row's enum (G0.16-T2 #1304 reconciliation).
    """

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubPATCredentials:
        del target, op
        return GitHubPATCredentials(token="ghp_pat_redacted")

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(name="github-pat-target")

    # If the connector accidentally tried to mint, this absence would
    # raise an unmocked-route error from respx.
    installations_route = respx.get(
        f"{DEFAULT_GITHUB_API_URL}/user/installations",
    ).mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 0, "installations": []},
        )
    )

    fp = await conn.fingerprint(target)
    assert fp.reachable is True
    assert installations_route.call_count == 1
    # Confirm the Bearer token is the PAT verbatim.
    bearer = installations_route.calls[0].request.headers["Authorization"]
    assert bearer == "Bearer ghp_pat_redacted"


# ---------------------------------------------------------------------------
# Auth-model gating
# ---------------------------------------------------------------------------


async def test_auth_headers_rejects_unsupported_auth_model(
    app_creds: GitHubAppCredentials,
    operator: Operator,
) -> None:
    """``auth_model="per_user"`` raises :exc:`NotImplementedError`.

    Mirrors the vmware-rest rejection shape: the gh-rest connector
    only supports the ``shared_service_account`` identity model
    (G0.16-T2 #1304). The App-vs-PAT discriminator is in the Vault
    payload, not the target enum, so the error message names
    ``shared_service_account`` (the accepted target value) rather
    than the historical protocol markers.
    """

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return app_creds

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(name="bad", auth_model="per_user")
    with pytest.raises(NotImplementedError) as excinfo:
        await conn.auth_headers(target, operator)
    msg = str(excinfo.value)
    assert "'bad'" in msg
    assert "per_user" in msg
    assert "'shared_service_account'" in msg


async def test_auth_headers_accepts_shared_service_account(
    app_creds: GitHubAppCredentials,
    operator: Operator,
) -> None:
    """``auth_model="shared_service_account"`` flows through to the loader.

    This is the headline G0.16-T2 (#1304) acceptance — the target
    shape RDC's ``evoila-bosnia-gh`` registers (App credentials in
    Vault + ``shared_service_account`` on the row) now reaches the
    loader instead of bouncing off the connector boundary with the
    historical ``NotImplementedError``.
    """

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return app_creds

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(
        name="evoila-bosnia-gh",
        auth_model=AuthModel.SHARED_SERVICE_ACCOUNT.value,
    )
    with respx.mock:
        respx.post(
            f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
        ).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_live", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        headers = await conn.auth_headers(target, operator)
    assert headers["Authorization"] == "Bearer ghs_live"
    assert headers["Accept"] == "application/vnd.github+json"


async def test_auth_headers_accepts_legacy_none_auth_model(
    app_creds: GitHubAppCredentials,
    operator: Operator,
) -> None:
    """``auth_model=None`` is accepted as the legacy-row sentinel.

    Mirrors :class:`VmwareRestConnector`'s contract — pre-G0.3 rows
    whose ``auth_model`` column has not yet been populated still
    resolve cleanly. The column default is
    ``shared_service_account`` from G0.3 onward; the ``None`` carve-
    out preserves backwards-compatibility for fixtures and
    hand-constructed :class:`Target` instances in tests.
    """

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return app_creds

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(name="legacy-row", auth_model=None)
    with respx.mock:
        respx.post(
            f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
        ).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_legacy", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        headers = await conn.auth_headers(target, operator)
    assert headers["Authorization"] == "Bearer ghs_legacy"


async def test_auth_headers_rejects_empty_operator_jwt(
    app_creds: GitHubAppCredentials,
) -> None:
    """An operator with ``raw_jwt=""`` cannot read per-target vendor credentials."""
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return app_creds

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(name="any")
    operator = _make_operator(raw_jwt="")
    with pytest.raises(VaultCredentialsReadError) as excinfo:
        await conn.auth_headers(target, operator)
    assert "no operator JWT" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Fingerprint shape — repo coordinates
# ---------------------------------------------------------------------------


@respx.mock
async def test_fingerprint_with_repo_coordinates_uses_repos_endpoint(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """``host="api.github.com/repos/evoila/meho"`` hits ``GET /repos/evoila/meho``."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    repo_route = respx.get(
        f"{DEFAULT_GITHUB_API_URL}/repos/evoila/meho",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1234567,
                "full_name": "evoila/meho",
                "private": False,
                "default_branch": "main",
                "owner": {"login": "evoila", "type": "Organization"},
            },
        )
    )
    target = _FakeTarget(
        name="github-evoila-meho",
        host="api.github.com/repos/evoila/meho",
    )
    fp = await connector_with_app_loader.fingerprint(target)
    assert fp.reachable is True
    assert repo_route.called
    assert fp.extras["repo_full_name"] == "evoila/meho"
    assert fp.extras["repo_id"] == 1234567
    assert fp.extras["owner_login"] == "evoila"
    assert fp.extras["owner_type"] == "Organization"
    assert fp.extras["default_branch"] == "main"


@respx.mock
async def test_fingerprint_with_bare_owner_repo_uses_repos_endpoint(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """``host="evoila/meho"`` (operator-friendly bare form) also hits ``GET /repos/evoila/meho``.

    The docstring on :func:`_extract_repo_path` documents three accepted
    shapes — ``api.github.com/repos/owner/repo``,
    ``api.github.com/owner/repo``, and bare ``owner/repo``. This test
    pins the bare form, which is what the v0.x onboarding doc tells
    operators to configure on ``target.host``.
    """
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    repo_route = respx.get(
        f"{DEFAULT_GITHUB_API_URL}/repos/evoila/meho",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1234567,
                "full_name": "evoila/meho",
                "private": False,
                "default_branch": "main",
                "owner": {"login": "evoila", "type": "Organization"},
            },
        )
    )
    target = _FakeTarget(name="github-evoila-meho", host="evoila/meho")
    fp = await connector_with_app_loader.fingerprint(target)
    assert fp.reachable is True
    assert repo_route.called
    assert fp.extras["repo_full_name"] == "evoila/meho"


# ---------------------------------------------------------------------------
# T11 error envelopes surfaced on fingerprint
# ---------------------------------------------------------------------------


@respx.mock
async def test_fingerprint_surfaces_app_not_installed_envelope(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """404 on mint → ``reachable=False`` + ``error_code="github_app_not_installed"``."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    target = _FakeTarget(name="github-broken")
    fp = await connector_with_app_loader.fingerprint(target)
    assert fp.reachable is False
    assert fp.extras["error_code"] == "github_app_not_installed"
    assert "github_app_not_installed" in fp.extras["error"]


@respx.mock
async def test_fingerprint_surfaces_rate_limited_envelope(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """403 + ``X-RateLimit-Remaining: 0`` → ``error_code="github_rate_limited"``."""
    respx.post(
        f"{DEFAULT_GITHUB_API_URL}/app/installations/42/access_tokens",
    ).mock(
        return_value=httpx.Response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1700001234",
            },
        )
    )
    target = _FakeTarget(name="github-rate-limited")
    fp = await connector_with_app_loader.fingerprint(target)
    assert fp.reachable is False
    assert fp.extras["error_code"] == "github_rate_limited"


@respx.mock
async def test_fingerprint_surfaces_jwt_mint_failed_envelope(operator: Operator) -> None:
    """A malformed PEM in the loader's creds surfaces as JWT-mint-failed."""

    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubAppCredentials:
        del target, op
        return GitHubAppCredentials(
            app_id="100000",
            private_key_pem="not-a-real-pem",
            installation_id="42",
        )

    conn = GitHubRestConnector(credentials_loader=loader)
    target = _FakeTarget(name="github-bad-key")
    fp = await conn.fingerprint(target)
    assert fp.reachable is False
    assert fp.extras["error_code"] == "github_jwt_mint_failed"


# ---------------------------------------------------------------------------
# execute() is a stub until T3 ships the catalog
# ---------------------------------------------------------------------------


async def test_execute_returns_unknown_op_until_catalog_lands(
    connector_with_app_loader: GitHubRestConnector,
) -> None:
    """T1 ships zero typed ops; ``execute`` returns the structured ``unknown_op``."""
    target = _FakeTarget(name="anything")
    result = await connector_with_app_loader.execute(
        target,
        "gh.pr.get",
        {"owner": "evoila", "repo": "meho", "pull_number": 754},
    )
    assert result.status == "error"
    assert "unknown_op" in (result.error or "")


# ---------------------------------------------------------------------------
# Loader-shape mismatch — returning neither App nor PAT credentials
# ---------------------------------------------------------------------------


async def test_loader_returning_unknown_credentials_class_raises_t11(
    operator: Operator,
) -> None:
    """A loader returning neither App nor PAT credentials raises the base envelope.

    After G0.16-T2 (#1304) the App-vs-PAT path is picked by the
    returned credentials class type (not by ``target.auth_model``), so
    "loader returned a PAT for an App target" is no longer a wrong-
    shape — it's the PAT path. What *is* still wrong-shape is a
    loader that returns something that's neither :class:`GitHub
    AppCredentials` nor :class:`GitHubPATCredentials` — a loader-
    configuration bug surfaced via the base ``github_credential_
    error`` T11 envelope.
    """
    from meho_backplane.connectors.github import GitHubCredentialError

    class _UnknownCreds:
        """Stand-in for a misconfigured custom loader return value."""

    async def loader(target: GitHubTargetLike, op: Operator) -> object:
        del target, op
        return _UnknownCreds()

    # The loader's annotated return type intentionally lies about its
    # shape (``object`` rather than the union) so this regression test
    # exercises the connector's runtime defence even when the test seam
    # accepts a type-checker-evading stub.
    conn = GitHubRestConnector(credentials_loader=loader)  # type: ignore[arg-type]
    target = _FakeTarget(name="any")
    with pytest.raises(GitHubCredentialError) as excinfo:
        await conn.auth_headers(target, operator)
    assert excinfo.value.code == "github_credential_error"
    assert "_UnknownCreds" in str(excinfo.value)
