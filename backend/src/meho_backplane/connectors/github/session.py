# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GitHub App + PAT credential loaders for :class:`GitHubRestConnector`.

The connector supports two **upstream credential protocols** —
selected by inspecting the Vault payload, not by surfacing the protocol
on the target row's ``auth_model``:

* **App installation token** — the canonical machine-identity path. The
  loader reads the App ID + RSA private key (PEM) + installation ID from
  Vault under the operator's identity, mints a short-lived (≤10-minute)
  RS256 JWT signed with the private key, and exchanges that JWT for an
  **installation token** via
  ``POST /app/installations/{id}/access_tokens``. The installation token
  has a 1-hour TTL per GitHub's documentation; the credential cache
  holds it for up to 50 minutes (10-minute safety margin) before
  re-minting.

* **Fine-grained PAT** — the fallback path for environments where a
  GitHub App is not viable (e.g. operator-scoped read-only access during
  a v0.x dogfood). The loader reads a fine-grained Personal Access Token
  from Vault and returns it directly. PATs are bearer tokens with no
  on-the-fly minting; the TTL is whatever GitHub's PAT expiry policy
  enforces (operator-set).

Why the target's ``auth_model`` is not the discriminator
========================================================

The target row carries the **identity model** the backplane uses
(``shared_service_account`` — the App or PAT is one service identity
shared across operators). The **upstream credential protocol**
(App-installation vs PAT) is an implementation detail of *how the
connector talks to github.com*; surfacing it on ``auth_model`` would
either widen the operator-facing enum (making operators choose between
``github-app`` and ``github-pat`` at target-registration time, on a
value that's redundant with what Vault already holds) or — as G0.16-T2
caught — fragment the connector boundary against the target schema
when the two sides disagree. So the connector takes the same shape as
vmware-rest: the target's ``auth_model`` says "shared service
account"; the connector inspects the Vault payload to pick the
protocol.

The two loaders below remain individually exported for
backwards-compatible test injection; production code routes through
:func:`load_github_credentials_from_vault` which reads the secret once,
inspects the field shape, and picks the right loader.

All loaders are injectable callables (the same shape vmware-rest's
:class:`VsphereSessionLoader` uses) so production deploys, unit tests,
and the future operator runbook can substitute alternative resolvers
(e.g. a future GitHub Enterprise Server target with a different App
installation endpoint).

The four T11-compliant error envelopes for the failure modes named in
the task body live on :class:`GitHubCredentialError` / its subclasses,
each carrying a stable ``code`` attribute matching the
``docs/codebase/error-message-shape.md`` convention (``github_*``).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

import httpx
import jwt
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    VaultCredentialsReadError,
    load_basic_credentials,
    load_vault_secret_data,
)

__all__ = [
    "DEFAULT_GITHUB_API_URL",
    "GITHUB_APP_AUTH_MODEL",
    "GITHUB_PAT_AUTH_MODEL",
    "INSTALLATION_TOKEN_CACHE_SECONDS",
    "JWT_TTL_SECONDS",
    "GitHubAmbiguousVaultPayloadError",
    "GitHubAppCredentials",
    "GitHubAppNotInstalledError",
    "GitHubCredentialError",
    "GitHubCredentialsLoader",
    "GitHubInstallationTokenMintError",
    "GitHubJWTMintError",
    "GitHubPATCredentials",
    "GitHubRateLimitedError",
    "GitHubTargetLike",
    "InstallationToken",
    "load_github_app_credentials_from_vault",
    "load_github_credentials_from_vault",
    "load_github_pat_credentials_from_vault",
    "mint_github_app_jwt",
]


_log = structlog.get_logger(__name__)

#: Default github.com REST API base URL. GitHub Enterprise Server (GHES)
#: support is explicitly out of scope per the G3.11 Initiative body, but
#: keeping the constant separate from :class:`GitHubRestConnector` keeps
#: the eventual GHES override a one-line change in the per-target shape.
DEFAULT_GITHUB_API_URL: Final[str] = "https://api.github.com"

#: Internal protocol marker for the GitHub App installation-token path.
#:
#: Kept as a module-level constant for tests that wire the connector's
#: internal credential router and for the few places (composite
#: pre-flight, error messages) that name the path back to the operator.
#: **Not** a target ``auth_model`` value any more — see this module's
#: top-level docstring. The connector boundary now picks between this
#: protocol and the PAT protocol by inspecting the Vault payload.
GITHUB_APP_AUTH_MODEL: Final[str] = "github-app"

#: Internal protocol marker for the fine-grained PAT path. Same
#: not-a-target-``auth_model`` caveat as :data:`GITHUB_APP_AUTH_MODEL`.
GITHUB_PAT_AUTH_MODEL: Final[str] = "github-pat"

#: GitHub App JWTs are accepted only when ``exp - iat <= 600`` seconds
#: (GitHub's documented cap is 10 minutes; some clock-skew safety margin
#: is recommended in operator docs). We pick 540 seconds (9 minutes) so a
#: ~30-second clock skew between MEHO and api.github.com still produces a
#: valid token at the receiver.
JWT_TTL_SECONDS: Final[int] = 540

#: Installation tokens are valid for 1 hour. Cache for 50 minutes so the
#: connector re-mints before expiry without thrashing on every dispatch.
#: A 10-minute safety margin tolerates clock skew + long-running ops that
#: started just before expiry.
INSTALLATION_TOKEN_CACHE_SECONDS: Final[int] = 50 * 60

# Field names the GitHub-App credential loader expects in Vault. App ID
# is the numeric identifier of the GitHub App (an integer encoded as a
# string in the Vault secret); private_key is the RSA PEM (multiline
# string). installation_id pins which of the App's installations this
# target addresses — an App installed across multiple repos/orgs has
# one installation per target.
_GH_APP_FIELDS: Final[tuple[str, str, str]] = (
    "app_id",
    "private_key",
    "installation_id",
)

# Field name the PAT loader expects in Vault. The token value is the
# bearer credential — no exchange, no minting.
_GH_PAT_FIELD: Final[str] = "token"


@runtime_checkable
class GitHubTargetLike(Protocol):
    """Minimum target shape :class:`GitHubRestConnector` reads.

    Structural Protocol — any concrete ``Target`` in
    :mod:`meho_backplane.targets` that exposes these attributes satisfies
    it unchanged. ``auth_model`` carries the identity-model selector the
    backplane recognises (``shared_service_account``, or ``None`` for
    legacy rows). The **upstream credential protocol** (App-installation
    vs PAT) is picked by inspecting the Vault payload — not by the
    ``auth_model`` value. See the module docstring for the rationale.

    ``host`` carries either ``api.github.com`` (the default, for
    organisation-wide or operator-style ``GET /user/installations``
    fingerprinting) or ``api.github.com/repos/<owner>/<repo>`` (a
    target scoped to a specific repository — the issue body's
    ``GET /repos/{owner}/{repo}`` fingerprint variant). The connector
    splits ``host`` at the first ``/`` so the bare API URL drives the
    httpx base URL and the trailing path (if any) drives the
    fingerprint endpoint selection.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


@dataclass(frozen=True, slots=True)
class GitHubAppCredentials:
    """The Vault-stored material for a single GitHub App installation.

    All three fields land in the Vault secret as plain strings — App ID
    is numeric but stored as a string so the Vault KV-v2 ``data`` dict
    round-trips uniformly. The connector coerces back to ``str`` (used
    verbatim in the JWT ``iss`` claim).
    """

    app_id: str
    private_key_pem: str
    installation_id: str


@dataclass(frozen=True, slots=True)
class GitHubPATCredentials:
    """The Vault-stored material for a fine-grained PAT.

    Single field because PATs are self-contained bearer tokens — no App
    ID, no installation ID, no minting. The token is opaque to MEHO; the
    connector forwards it as ``Authorization: Bearer <token>`` on every
    call.
    """

    token: str


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """A live GitHub App installation token plus its cache validity.

    ``token`` is the opaque string GitHub mints from the JWT exchange.
    ``expires_at_monotonic`` is the absolute :func:`time.monotonic`
    timestamp after which the token is considered stale by the
    connector's cache (50 minutes after mint, per
    :data:`INSTALLATION_TOKEN_CACHE_SECONDS`). The dataclass is frozen
    so a cache hit cannot be tampered with by a concurrent caller.

    Stored separately from the bare token so cache validity decisions
    don't need to round-trip GitHub's ``expires_at`` ISO timestamp from
    the mint response (which we keep as ``upstream_expires_at`` for
    observability, but the cache decision is monotonic-clock-driven to
    avoid wall-clock-jump bugs).
    """

    token: str
    expires_at_monotonic: float
    upstream_expires_at: str | None = None


# A callable that resolves ``(target, operator)`` to GitHub App creds.
# Injectable so unit tests can supply canned credentials without
# touching Vault.
GitHubCredentialsLoader = Callable[
    [GitHubTargetLike, Operator],
    Awaitable["GitHubAppCredentials | GitHubPATCredentials"],
]
"""Async callable resolving ``(target, operator)`` to credentials.

The return type is a union because the connector selects between
:class:`GitHubAppCredentials` and :class:`GitHubPATCredentials` by
inspecting the Vault payload's field shape. Production uses
:func:`load_github_credentials_from_vault` (which performs the
single Vault read + protocol selection); tests inject stubs.
"""


# ---------------------------------------------------------------------------
# T11-compliant error envelopes
# ---------------------------------------------------------------------------


class GitHubCredentialError(Exception):
    """Base for GitHub credential / token errors per T11 message-shape.

    Subclasses carry a stable ``code`` (``snake_case``, ``github_*``
    prefix) the connector lifts into structured logs / audit rows. The
    ``__str__`` shape follows the convention's three-clause template:
    *"<code>: <values>. <remediation>. See <doc>."* The doc reference
    on the base class points at the GitHub-connector codebase doc which
    catalogues every code; subclasses optionally append a more specific
    doc URL.
    """

    code: str = "github_credential_error"


class GitHubAppNotInstalledError(GitHubCredentialError):
    """GitHub App credentials valid, but the App is not installed on target.

    Surfaces as ``GET /app/installations/{id}`` 404 from the JWT-bearing
    request, OR as 404 from the installation-token mint call when the
    ``installation_id`` does not name a real installation.
    """

    code = "github_app_not_installed"


class GitHubJWTMintError(GitHubCredentialError):
    """RS256 JWT signing failed — typically a malformed private key PEM.

    PyJWT's :func:`jwt.encode` raises broadly for "key not loadable",
    "unsupported algorithm", etc.; this wrapper normalises every such
    failure into one error with a remediation-bearing message.
    """

    code = "github_jwt_mint_failed"


class GitHubInstallationTokenMintError(GitHubCredentialError):
    """``POST /app/installations/{id}/access_tokens`` failed.

    Covers any non-2xx response that is not a 404 (which maps to
    :class:`GitHubAppNotInstalledError`) and any transport-level
    failure on the mint call. The original status code (when known)
    lands on the error's ``status_code`` attribute for log
    attribution.
    """

    code = "github_installation_token_mint_failed"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAmbiguousVaultPayloadError(GitHubCredentialError):
    """The Vault secret carries neither a full App field set nor a PAT.

    Raised by :func:`load_github_credentials_from_vault` when the secret
    payload at ``target.secret_ref`` does not match either of the two
    documented shapes:

    * App: all of ``app_id``, ``private_key``, ``installation_id``
      populated;
    * PAT: ``token`` populated.

    The error message names which fields *were* present (without
    echoing any value) and which fields *are required* for each
    protocol, so the operator can correct the Vault payload without
    a second round-trip through the runbook.
    """

    code = "github_ambiguous_vault_payload"


class GitHubRateLimitedError(GitHubCredentialError):
    """Upstream rate-limit hit (``X-RateLimit-Remaining: 0``).

    Both the primary and secondary rate-limit shapes (per GitHub's REST
    rate-limit documentation) surface here. The ``reset_at`` field
    carries the Unix timestamp from the ``X-RateLimit-Reset`` header
    (when present) so the connector / operator can decide whether to
    wait or fail.
    """

    code = "github_rate_limited"

    def __init__(self, message: str, *, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


# ---------------------------------------------------------------------------
# JWT minting
# ---------------------------------------------------------------------------


def mint_github_app_jwt(
    app_id: str,
    private_key_pem: str,
    *,
    now: float | None = None,
    ttl_seconds: int = JWT_TTL_SECONDS,
) -> str:
    """Mint a 10-minute-bound RS256 JWT for the GitHub App.

    Returns the encoded JWT string for use as ``Authorization: Bearer
    <jwt>`` on ``GET /app/*`` calls (specifically the installation-token
    mint endpoint). The claims are the three GitHub documents: ``iat``
    (issued-at), ``exp`` (expiry), ``iss`` (the App ID as a string).

    ``now`` is injectable for the cache-test path (the unit test mocks
    the clock to assert "second fingerprint call does not re-mint" —
    see :data:`INSTALLATION_TOKEN_CACHE_SECONDS`).

    Raises :class:`GitHubJWTMintError` when PyJWT cannot sign the
    payload — malformed PEM, wrong algorithm for the key shape, etc.
    The error message names the App ID (operator-known) but not the key
    material (per the T11 info-leak rule).
    """
    # Backdate ``iat`` by 60s per GitHub's documented JWT recipe:
    # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
    # GitHub recommends ``iat = now - 60`` to absorb forward clock
    # skew between the caller and GitHub's clock. ``JWT_TTL_SECONDS``
    # already trims ``exp - iat`` below the 10-minute cap to absorb
    # backward skew on the other end, but that only protects the
    # expiry side. Without this backdate, a clock running fast on
    # this host would produce a JWT GitHub rejects as
    # "issued-at in the future".
    iat = int(now if now is not None else time.time()) - 60
    exp = iat + ttl_seconds
    payload: dict[str, object] = {"iat": iat, "exp": exp, "iss": app_id}
    try:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    except Exception as exc:  # PyJWT raises broadly; normalise to T11.
        raise GitHubJWTMintError(
            f"github_jwt_mint_failed: cannot sign JWT for app_id={app_id!r} — "
            f"check the Vault-stored RSA private key (PEM) is well-formed and "
            f"matches the App's configured public key. See "
            f"docs/cross-repo/github-app-credential.md for the credential "
            f"custody recipe. Underlying error: {type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Default Vault-backed loaders
# ---------------------------------------------------------------------------


async def load_github_app_credentials_from_vault(
    target: GitHubTargetLike,
    operator: Operator,
    *,
    mount: str = DEFAULT_KV_MOUNT,
) -> GitHubAppCredentials:
    """Read ``target.secret_ref`` and return App ID + private key + installation ID.

    Delegates to the shared :func:`load_basic_credentials` helper (G3.9-T2
    precedent) so the operator-context Vault read, the no-secret-in-logs
    discipline, and the two-phase error contract are defined once for
    every REST connector. The helper takes a custom ``fields`` tuple so
    we ask for ``("app_id", "private_key", "installation_id")`` instead
    of the default ``("username", "password")``.

    The structured-log event carries only field *names* + target /
    host — never a credential value. The returned dataclass is ephemeral
    in-memory state and must not enter any log event,
    :class:`OperationResult`, or durable artifact.
    """
    creds = await load_basic_credentials(
        target,
        operator,
        fields=_GH_APP_FIELDS,
        mount=mount,
    )
    return GitHubAppCredentials(
        app_id=creds["app_id"],
        private_key_pem=creds["private_key"],
        installation_id=creds["installation_id"],
    )


async def load_github_pat_credentials_from_vault(
    target: GitHubTargetLike,
    operator: Operator,
    *,
    mount: str = DEFAULT_KV_MOUNT,
) -> GitHubPATCredentials:
    """Read ``target.secret_ref`` and return the bearer PAT.

    Same delegation pattern as the App loader — the shared helper
    enforces the operator-context Vault read, the no-secret-in-logs
    discipline, and the fail-closed empty-JWT carve-out.
    """
    creds = await load_basic_credentials(
        target,
        operator,
        fields=(_GH_PAT_FIELD,),
        mount=mount,
    )
    return GitHubPATCredentials(token=creds[_GH_PAT_FIELD])


async def load_github_credentials_from_vault(
    target: GitHubTargetLike,
    operator: Operator,
    *,
    mount: str = DEFAULT_KV_MOUNT,
) -> GitHubAppCredentials | GitHubPATCredentials:
    """Read the Vault secret once and pick App vs PAT by which fields it carries.

    Single Vault round-trip — replaces the historical "the target's
    ``auth_model`` picks which loader runs" routing. The connector
    boundary now uses the **shape of the Vault payload** as the
    discriminator:

    * All of ``app_id``, ``private_key``, ``installation_id`` populated
      → :class:`GitHubAppCredentials` (the App installation path).
    * ``token`` populated (when the full App field set is not
      present) → :class:`GitHubPATCredentials` (the PAT fallback path).
    * Neither set populated → :class:`GitHubAmbiguousVaultPayloadError`
      with a remediation-bearing message naming the field shape we
      looked for and which fields were present (no values echoed).

    This matches the operator-runbook documentation in
    ``docs/cross-repo/github-connector.md`` (the ``auth_model: shared_
    service_account`` row + Vault-payload discriminator) and aligns the
    gh-rest target shape with vmware-rest's
    ``shared_service_account``-on-target / payload-shape-on-Vault split.
    See G0.16-T2 for the reconciliation history.

    The structured-log event from :func:`load_vault_secret_data` carries
    the **set of field names** present in the secret, never a value;
    the App branch coerces each field to ``str`` (mirroring
    :func:`load_github_app_credentials_from_vault`).
    """
    secret_data = await load_vault_secret_data(target, operator, mount=mount)
    present_fields = set(secret_data.keys())

    app_fields_present = all(field in present_fields for field in _GH_APP_FIELDS)
    pat_field_present = _GH_PAT_FIELD in present_fields

    if app_fields_present:
        return GitHubAppCredentials(
            app_id=str(secret_data["app_id"]),
            private_key_pem=str(secret_data["private_key"]),
            installation_id=str(secret_data["installation_id"]),
        )
    if pat_field_present:
        return GitHubPATCredentials(token=str(secret_data[_GH_PAT_FIELD]))

    # Neither shape matches — fail closed with a structured error that
    # tells the operator exactly which fields to write into Vault.
    # ``sorted`` so the field list is diff-stable in error messages.
    raise GitHubAmbiguousVaultPayloadError(
        "github_ambiguous_vault_payload: Vault secret for target "
        f"{target.name!r} (secret_ref={target.secret_ref!r}) does not "
        "carry either credential-protocol shape the gh-rest connector "
        "supports. For the GitHub App path, populate all of "
        f"{list(_GH_APP_FIELDS)!r}; for the fine-grained PAT fallback, "
        f"populate {_GH_PAT_FIELD!r}. Fields present in the secret: "
        f"{sorted(present_fields)!r}. See docs/cross-repo/github-"
        "connector.md § 'App-vs-PAT credential picker' for the payload "
        "shape on each path."
    )


# ---------------------------------------------------------------------------
# Installation-token exchange
# ---------------------------------------------------------------------------


async def exchange_jwt_for_installation_token(
    *,
    jwt_token: str,
    installation_id: str,
    api_base_url: str = DEFAULT_GITHUB_API_URL,
    http_client: httpx.AsyncClient | None = None,
    now: float | None = None,
) -> InstallationToken:
    """``POST /app/installations/{id}/access_tokens`` and wrap the result.

    Builds an :class:`InstallationToken` whose
    ``expires_at_monotonic`` is set to ``now + INSTALLATION_TOKEN_CACHE_SECONDS``
    so the cache's validity decision is monotonic-clock-driven (wall-
    clock-jump immune). The upstream ISO timestamp (``expires_at`` from
    GitHub's response) lands on ``upstream_expires_at`` for observability.

    ``http_client`` is injectable for unit tests; production code
    constructs one against ``api_base_url`` and lets ``aclose`` clean it
    up. ``now`` is injectable for the cache-test path.

    Raises :class:`GitHubAppNotInstalledError` on 404 (the
    ``installation_id`` does not name an installation MEHO's App can
    reach), :class:`GitHubRateLimitedError` when the ``X-RateLimit-
    Remaining: 0`` header appears, and
    :class:`GitHubInstallationTokenMintError` for every other non-2xx
    or transport error.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    path = f"/app/installations/{installation_id}/access_tokens"

    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            base_url=api_base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        )
    try:
        try:
            resp = await http_client.post(path, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubInstallationTokenMintError(
                f"github_installation_token_mint_failed: transport failure "
                f"reaching {api_base_url}{path} — check network / TLS to "
                f"api.github.com. Underlying error: {type(exc).__name__}: {exc}"
            ) from exc

        _raise_for_rate_limit(resp)

        if resp.status_code == 404:
            raise GitHubAppNotInstalledError(
                f"github_app_not_installed: installation_id="
                f"{installation_id!r} is not reachable for this App — the "
                f"App may not be installed on the target repo/org, or the "
                f"installation_id in Vault is stale. Re-confirm the "
                f"installation per docs/cross-repo/github-app-credential.md "
                f"(install the App, copy the installation ID from the URL)."
            )
        if resp.status_code >= 400:
            body_excerpt = _excerpt(resp.text)
            raise GitHubInstallationTokenMintError(
                f"github_installation_token_mint_failed: "
                f"POST {api_base_url}{path} returned HTTP "
                f"{resp.status_code}. Body excerpt: {body_excerpt!r}. "
                f"See docs/cross-repo/github-app-credential.md for App "
                f"permission scoping; common causes are a revoked App "
                f"private key or a permission-mismatch between the App "
                f"manifest and the installation's granted permissions.",
                status_code=resp.status_code,
            )

        return _build_installation_token_from_response(
            resp=resp,
            api_base_url=api_base_url,
            path=path,
            now=now,
        )
    finally:
        if owns_client:
            await http_client.aclose()


def _build_installation_token_from_response(
    *,
    resp: httpx.Response,
    api_base_url: str,
    path: str,
    now: float | None,
) -> InstallationToken:
    """Parse a 2xx ``access_tokens`` response into an :class:`InstallationToken`.

    Extracted from :func:`exchange_jwt_for_installation_token` so the
    request / error-envelope / parse split stays under the
    code-quality function-size threshold. Surfaces all parse-time
    failures (non-JSON body, missing ``token`` field) through the
    documented ``github_installation_token_mint_failed`` T11 envelope.
    """
    try:
        payload = resp.json()
    except ValueError as exc:
        # A non-JSON 2xx body is a GitHub contract violation (an
        # upstream proxy returning HTML, a partial response, etc.).
        # Surface it through the same ``github_installation_token_mint_failed``
        # T11 envelope rather than letting ``ValueError`` escape — the
        # caller's failure handling is shape-stable on this code.
        body_excerpt = _excerpt(resp.text)
        raise GitHubInstallationTokenMintError(
            f"github_installation_token_mint_failed: "
            f"POST {api_base_url}{path} returned a non-JSON 2xx body. "
            f"Body excerpt: {body_excerpt!r}. Upstream contract violation.",
            status_code=resp.status_code,
        ) from exc
    token_value = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token_value, str) or not token_value:
        keys_or_type = (
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        )
        raise GitHubInstallationTokenMintError(
            f"github_installation_token_mint_failed: response from "
            f"POST {api_base_url}{path} is missing a 'token' field "
            f"(received keys: {keys_or_type}). This is a GitHub API "
            f"contract violation — file an issue and re-mint manually."
        )
    upstream_expires_at = payload.get("expires_at")
    upstream_expires_str: str | None = (
        upstream_expires_at if isinstance(upstream_expires_at, str) else None
    )
    now_monotonic = now if now is not None else time.monotonic()
    return InstallationToken(
        token=token_value,
        expires_at_monotonic=now_monotonic + INSTALLATION_TOKEN_CACHE_SECONDS,
        upstream_expires_at=upstream_expires_str,
    )


def _raise_for_rate_limit(resp: httpx.Response) -> None:
    """Inspect rate-limit headers and raise :class:`GitHubRateLimitedError` on exhaustion.

    GitHub returns 403 (primary) or 429 (secondary) when rate-limited;
    in both shapes the ``X-RateLimit-Remaining: 0`` header is the
    authoritative signal (the body wording varies across endpoints and
    is not safe to pattern-match). Per the GitHub REST rate-limit
    documentation, we read ``X-RateLimit-Reset`` for the reset timestamp
    when available; absence is non-fatal.
    """
    if resp.status_code not in (403, 429):
        return
    remaining_hdr = resp.headers.get("X-RateLimit-Remaining")
    if remaining_hdr is None or remaining_hdr.strip() != "0":
        return
    reset_hdr = resp.headers.get("X-RateLimit-Reset")
    reset_at: int | None = None
    if reset_hdr is not None:
        try:
            reset_at = int(reset_hdr)
        except ValueError:
            reset_at = None
    reset_clause = f" (resets at unix={reset_at})" if reset_at is not None else ""
    raise GitHubRateLimitedError(
        f"github_rate_limited: api.github.com returned HTTP "
        f"{resp.status_code} with X-RateLimit-Remaining: 0{reset_clause}. "
        f"Wait for the reset window or reduce per-target call volume. See "
        "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api "
        "for the rate-limit policy.",
        reset_at=reset_at,
    )


def _excerpt(body: str, *, limit: int = 200) -> str:
    """Truncate a response body for safe inclusion in an error message.

    GitHub's error bodies are typically small JSON envelopes, but a
    malformed proxy could return a multi-KB HTML page. Cap at 200 chars
    to keep the audit row readable; the structured log carries the full
    response body when needed for forensics.
    """
    if len(body) <= limit:
        return body
    return body[:limit] + "...<truncated>"


# Re-export :class:`VaultCredentialsReadError` for symmetric error
# handling at the connector layer — the loader's read-phase failures
# (empty operator JWT, unset ``secret_ref``, missing field) propagate
# verbatim from :func:`load_basic_credentials`. Listed in ``__all__``
# via the import side so callers can catch a single error class for
# loader-vs-network failures.
_ = VaultCredentialsReadError  # silence vulture / mark intent
_AuthModelLiteral = Literal["github-app", "github-pat"]  # documentation alias
