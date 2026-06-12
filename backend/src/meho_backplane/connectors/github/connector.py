# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GitHubRestConnector — substrate for the gh-rest typed connector (G3.11-T1).

Hand-rolled :class:`HttpConnector` subclass that dispatches the github.com
REST API surface under the registry triple ``(product="gh", version="3",
impl_id="gh-rest")``. The registry's ``version`` is the digit-prefixed
slot the dispatcher's connector-id parser requires (regex
``^[0-9][A-Za-z0-9._]*$`` in :func:`parse_connector_id` — see
``backend/src/meho_backplane/operations/_lookup.py``); the GitHub REST API
itself is referred to as **v3** in github.com's documentation. So
``connector_id="gh-rest-3"`` parses back to ``("gh", "3", "gh-rest")``
losslessly, and the operator-visible label ("GitHub REST v3") still
matches the upstream API contract.

This Task ships the substrate only — the catalog entry that makes ~700
ingested ops dispatchable (T3 #1223) and the first L1 composite
``gh.composite.pr_status_summary`` (T4 #1224) land separately.
``register_operations`` is therefore intentionally empty here.

Auth shape
----------

The connector boundary accepts ``target.auth_model="shared_service_
account"`` (or ``None`` for legacy rows). The **upstream credential
protocol** — App-installation vs PAT — is picked by inspecting the
Vault payload's field shape, **not** by widening the target enum. This
mirrors :class:`VmwareRestConnector` (the target carries the identity
model; the connector reads ``username`` + ``password`` and never
surfaces a protocol on ``auth_model``). G0.16-T2 (#1304) reconciled
the contract after a v0.8.0-cycle dogfood caught the original
auth_model-driven routing rejecting every target with a 422 enum
violation.

* **App installation-token path** — canonical machine-identity. Vault
  payload carries ``app_id`` + ``private_key`` + ``installation_id``.
  The connector reads the secret under the operator's identity (G3.9
  precedent), mints a 10-minute RS256 JWT, and exchanges it for an
  installation token via
  ``POST /app/installations/{id}/access_tokens``. The installation
  token is cached for 50 minutes (10-minute safety margin before the
  1-hour upstream expiry) so a typical operator session of ~50
  fingerprint / dispatch calls pays the mint round-trip exactly once.
* **Fine-grained PAT path** — fallback. Vault payload carries
  ``token`` (and no App fields). The PAT reads directly from Vault and
  passes through as the bearer credential. Documented but not
  first-class.

Per-target cache scoping mirrors vmware-rest: keyed on the tenant-unique
``(tenant_id, target.id)`` tuple (#1642/#1672) so two same-named targets
in different tenants never share a cached token,
serialised under a single :class:`asyncio.Lock` so two concurrent
first-use callers don't double-mint. The cache fast-path additionally
rejects an empty operator JWT (defense-in-depth — the loader's
:func:`load_basic_credentials` already does this; the cache enforces the
same invariant so a primed token from an authenticated caller cannot
leak to a system-initiated caller per
``docs/architecture/connector-auth.md`` § "Cache scoping under
``shared_service_account``"). Fingerprint and probe paths synthesise a
system operator with a non-empty placeholder JWT so the cache fast-path
admits them but the live Vault loader still fails closed.

Fingerprint shape
-----------------

:meth:`fingerprint` calls either ``GET /user/installations`` (the
default, operator-style — lists every installation the App can access)
or ``GET /repos/{owner}/{repo}`` (when ``target.host`` carries
``api.github.com/repos/<owner>/<repo>``) — and returns a
:class:`FingerprintResult` whose ``extras`` carries the installation
metadata the operator wants on first-day onboarding: ``app_slug``,
``installation_id``, ``installation_account``, ``target_type``,
``permissions``. The installation-token mint itself populates most of
this material from the mint response, so a single round-trip on cold
cache reveals enough for the operator to confirm the App is reachable.

Out of scope (per T1)
---------------------

* The Layer-2 ingest catalog entry — T3 (#1223) ships
  ``backend/src/meho_backplane/operations/ingest/catalog/gh-v3.yaml``.
  Until then, :meth:`register_operations` is a no-op classmethod the
  lifespan can call cheaply.
* The L1 composite ``gh.composite.pr_status_summary`` — T4 (#1224).
* ``requires_approval=true`` annotations on write ops — T5 (#1225).
* GitHub Enterprise Server (GHES) — github.com only for v0.x; GHES is
  a different ``host`` + a future override.
* Webhooks / push-to-meho — separate G2.x infrastructure scope.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.github.session import (
    DEFAULT_GITHUB_API_URL,
    GitHubAmbiguousVaultPayloadError,
    GitHubAppCredentials,
    GitHubAppNotInstalledError,
    GitHubCredentialError,
    GitHubCredentialsLoader,
    GitHubInstallationTokenMintError,
    GitHubJWTMintError,
    GitHubPATCredentials,
    GitHubRateLimitedError,
    GitHubTargetLike,
    InstallationToken,
    exchange_jwt_for_installation_token,
    load_github_credentials_from_vault,
    mint_github_app_jwt,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["GitHubRestConnector"]

_log = structlog.get_logger(__name__)


class GitHubRestConnector(HttpConnector):
    """GitHub REST connector for github.com — App or PAT auth.

    Registered under the v2 triple ``(product="gh", version="3",
    impl_id="gh-rest")``. The v1 wildcard entry (``("gh", "", "")``,
    written by :func:`register_connector` at module import time) gives
    G0.15-T6 wildcard-resolver tie-break coverage so a target carrying
    ``(product=gh, version=None)`` still resolves to this connector.

    Class-level constants and attributes:

    * :attr:`product` ``= "gh"``
    * :attr:`version` ``= "3"`` — the registry slot version. GitHub
      labels its REST API as **v3**; the dispatcher's connector-id
      parser pins ``version`` to the digit-prefix shape (the regex
      ``^[0-9][A-Za-z0-9._]*$`` rejects ``"v3"``), so the registry
      stores ``"3"`` and the upstream "v3" name lives in docs.
    * :attr:`impl_id` ``= "gh-rest"``
    * :attr:`priority` ``= 1`` — outranks any auto-shim that may register
      against the same triple from a future generic-OpenAPI ingest path.
    """

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    priority = 1

    # vSphere-style class constant for the API base. Subclassed connectors
    # for GHES would override this (the GHES instance's base URL is the
    # organisation's GHES host with a ``/api/v3`` suffix), but T1 ships
    # github.com only.
    _BASE_URL = DEFAULT_GITHUB_API_URL

    def __init__(
        self,
        *,
        credentials_loader: GitHubCredentialsLoader | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the connector with optional injected loader + http client.

        ``credentials_loader`` defaults to a router that picks
        :func:`load_github_app_credentials_from_vault` /
        :func:`load_github_pat_credentials_from_vault` by the target's
        ``auth_model``. Unit tests inject a stub returning canned creds.

        ``http_client`` is reserved for the dispatch path — the
        installation-token mint accepts its own injectable client
        through :func:`exchange_jwt_for_installation_token`, but
        production code lets the connector own one (created lazily via
        :meth:`HttpConnector._http_client` per target). The ``__init__``
        parameter is kept for parity with the test seam K8s exposes;
        passing ``None`` is the production shape.
        """
        super().__init__()
        # Per-target installation-token cache. Keyed on the tenant-unique
        # ``(tenant_id, target.id)`` tuple (``target_cache_key``, #1642/#1672)
        # so two same-named targets in different tenants never share a
        # cached token; the value's ``expires_at_monotonic`` drives
        # cache-validity checks.
        self._installation_tokens: dict[tuple[str, str], InstallationToken] = {}
        # Per-target PAT cache — same shape and tenant-unique key but the
        # token never expires from MEHO's side (the PAT carries its own
        # GitHub-set TTL). Caching keeps the Vault read off the hot path.
        self._pat_tokens: dict[tuple[str, str], str] = {}
        self._token_lock = asyncio.Lock()
        # The injected http client is reserved for a future bring-your-
        # own-client test path; production uses the inherited per-target
        # pool from :class:`HttpConnector`.
        self._injected_http_client = http_client
        self._credentials_loader: GitHubCredentialsLoader = (
            credentials_loader if credentials_loader is not None else _default_loader
        )

    # ------------------------------------------------------------------
    # Auth surface
    # ------------------------------------------------------------------

    async def auth_headers(
        self,
        target: GitHubTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        """Return ``Authorization: Bearer <token>`` for *target*.

        Accepts ``target.auth_model="shared_service_account"`` (or
        ``None`` for legacy rows that predate the column default).
        Anything else (``per_user``, ``impersonation``, a typo) raises
        :exc:`NotImplementedError` naming the target and the requested
        mode — the same fail-closed shape :class:`VmwareRestConnector`
        uses at its boundary.

        The **upstream credential protocol** (App-installation vs PAT)
        is picked downstream of this check, by
        :func:`load_github_credentials_from_vault` inspecting the
        Vault payload's field shape — see the module docstring for
        the rationale (G0.16-T2 reconciliation, #1304).

        Raises :class:`VaultCredentialsReadError` when
        ``operator.raw_jwt`` is empty (defence in depth — the loader's
        own guard already rejects this; raising before the cache lookup
        ensures a primed token cannot leak to a system-initiated
        caller).
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        if not _is_acceptable_auth_model(getattr(target, "auth_model", None)):
            auth_model = getattr(target, "auth_model", None)
            raise NotImplementedError(
                f"GitHubRestConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._auth_token(target, operator)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _base_url(self, target: GitHubTargetLike) -> str:
        """Return ``https://api.github.com`` (port is ignored for github.com).

        Overrides :meth:`HttpConnector._base_url` because GitHub's REST
        surface is mounted at a fixed host irrespective of the
        ``target.host`` value (which may carry repo coordinates — see
        :meth:`fingerprint`). A future GHES override returns the
        per-target GHES base URL here.
        """
        del target  # github.com base URL is the only supported value in T1
        return self._BASE_URL

    # ------------------------------------------------------------------
    # Credential / token plumbing
    # ------------------------------------------------------------------

    async def _auth_token(
        self,
        target: GitHubTargetLike,
        operator: Operator,
    ) -> str:
        """Return the live bearer token for *target* — App or PAT.

        Lock-serialised so cold-cache callers don't double-load the
        same secret; warm-cache lookups (either the installation-token
        dict or the PAT dict) bypass the loader entirely. The loader
        runs only when neither cache holds a live entry, then the
        returned :class:`GitHubAppCredentials` / :class:`GitHubPATCredentials`
        instance dispatches into the App-mint path or the PAT
        passthrough.

        Raises :class:`GitHubAmbiguousVaultPayloadError` (from the
        loader) when the Vault payload carries neither shape; the four
        T11 App-path envelopes (``GitHubAppNotInstalledError`` /
        ``GitHubJWTMintError`` / ``GitHubInstallationTokenMintError``
        / ``GitHubRateLimitedError``) when the App-mint round-trip
        itself fails.
        """
        cache_key = target_cache_key(target)
        async with self._token_lock:
            now = time.monotonic()
            cached_app = self._installation_tokens.get(cache_key)
            if cached_app is not None and cached_app.expires_at_monotonic > now:
                return cached_app.token
            cached_pat = self._pat_tokens.get(cache_key)
            if cached_pat is not None:
                return cached_pat

            creds = await self._credentials_loader(target, operator)
            if isinstance(creds, GitHubAppCredentials):
                return await self._mint_and_cache_installation_token(target, creds)
            if isinstance(creds, GitHubPATCredentials):
                self._pat_tokens[cache_key] = creds.token
                _log.info(
                    "github_pat_token_loaded",
                    target=target.name,
                    host=target.host,
                )
                return creds.token
            raise GitHubCredentialError(
                f"github_credential_error: loader returned "
                f"{type(creds).__name__} for target {target.name!r}; "
                f"expected GitHubAppCredentials or GitHubPATCredentials. "
                f"This is a loader-configuration bug. See "
                f"docs/codebase/connectors-github.md."
            )

    async def _mint_and_cache_installation_token(
        self,
        target: GitHubTargetLike,
        creds: GitHubAppCredentials,
    ) -> str:
        """Mint a fresh installation token for *target* and cache it.

        Extracted from :meth:`_auth_token` so the JWT-sign +
        installation-token-exchange round-trip is one named operation
        the caller can audit independently. Cache lifetime is
        :data:`INSTALLATION_TOKEN_CACHE_SECONDS` (50 minutes; 10-minute
        safety margin before the 1-hour upstream expiry).

        Raises :class:`GitHubAppNotInstalledError`,
        :class:`GitHubJWTMintError`,
        :class:`GitHubInstallationTokenMintError`, or
        :class:`GitHubRateLimitedError` per the four T11 envelopes.
        """
        jwt_token = mint_github_app_jwt(
            creds.app_id,
            creds.private_key_pem,
        )
        installation = await exchange_jwt_for_installation_token(
            jwt_token=jwt_token,
            installation_id=creds.installation_id,
            api_base_url=self._BASE_URL,
        )
        self._installation_tokens[target_cache_key(target)] = installation
        _log.info(
            "github_installation_token_minted",
            target=target.name,
            host=target.host,
            installation_id=creds.installation_id,
            upstream_expires_at=installation.upstream_expires_at,
        )
        return installation.token

    # ------------------------------------------------------------------
    # Required Connector ABC methods
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: GitHubTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Probe GitHub to return installation metadata for *target*.

        Two shapes by ``target.host``:

        * ``api.github.com`` (or any value without a ``/`` after the
          host) → ``GET /user/installations`` lists every installation
          the App can reach. The first installation lands on
          ``extras['installation_account']`` /
          ``extras['installation_id']`` etc.; the count appears at
          ``extras['installations_count']``.
        * ``api.github.com/repos/<owner>/<repo>`` →
          ``GET /repos/{owner}/{repo}`` returns the repo's metadata for
          a targeted-repo fingerprint.

        ``operator`` (optional, G0.16-T4 #1306) is forwarded to the
        credentials loader so the per-target Vault read happens under
        the operator's identity, matching the dispatch path. ``None``
        falls back to a system operator whose placeholder JWT fails
        closed at the live Vault round-trip (the wire format is still
        exercised; the system-call carve-out holds).

        Transport / credential failures land as
        ``reachable=False`` with ``extras['error']`` carrying the
        error class + message (same pattern as
        :meth:`VmwareRestConnector.fingerprint`). The structured
        ``error_code`` from the T11 envelope (when one fired) is
        surfaced separately under ``extras['error_code']``.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        repo_path = _extract_repo_path(target.host)
        if repo_path is None:
            probe_method = "GET /user/installations"
            path = "/user/installations"
        else:
            probe_method = f"GET /repos/{repo_path}"
            path = f"/repos/{repo_path}"

        try:
            payload = await self._get_json(target, path, operator=eff_operator)
        except GitHubCredentialError as exc:
            return FingerprintResult(
                vendor="github",
                product="gh",
                reachable=False,
                probed_at=probed_at,
                probe_method=probe_method,
                extras={
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_code": exc.code,
                },
            )
        except (httpx.HTTPError, OSError, VaultCredentialsReadError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="github",
                product="gh",
                reachable=False,
                probed_at=probed_at,
                probe_method=probe_method,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        if repo_path is None:
            return _fingerprint_from_installations(payload, probed_at, probe_method)
        return _fingerprint_from_repo(payload, probed_at, probe_method)

    async def probe(self, target: GitHubTargetLike) -> ProbeResult:
        """Lightweight reachability check.

        Delegates to :meth:`fingerprint` since the GitHub installation
        probe is already a single round-trip — same pattern as
        :class:`VmwareRestConnector`.
        """
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error", "unreachable")),
            probed_at=fp.probed_at,
        )

    async def execute(
        self,
        target: GitHubTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Stub dispatcher — every op_id resolves to ``unknown_op`` until T3 lands.

        T1 ships the substrate only. The catalog entry (T3 #1223)
        populates ``endpoint_descriptor`` with ~700 ingested ops; until
        then, every op_id returns the structured ``unknown_op`` shape
        the dispatcher emits everywhere else so the operator sees the
        same response shape whether the connector is mounted or
        unmounted in the dispatch path.
        """
        del target, params
        # Import lazily to avoid pulling the operations package's
        # transitive imports (embedding service, ONNX runtime) into a
        # pure-Python introspection test.
        from meho_backplane.operations._errors import result_unknown_op

        return result_unknown_op(op_id=op_id, known_op_count=0, duration_ms=0.0)

    @classmethod
    async def register_operations(cls) -> None:
        """Intentional no-op — T1 ships zero typed ops.

        The L2 catalog entry (T3 #1223, canonicalised by T8 #1242)
        registers the ingested ops via the
        ``meho connector ingest --catalog gh/3`` path; this
        classmethod stays a stub the lifespan can call cheaply. Lands
        in the registrar list via the package ``__init__`` so the
        symmetry with vault / kubernetes registration is preserved
        once T3 fills in the body.
        """
        return None

    async def aclose(self) -> None:
        """Drop cached tokens + close the inherited httpx pool."""
        async with self._token_lock:
            self._installation_tokens.clear()
            self._pat_tokens.clear()
        await super().aclose()


# ---------------------------------------------------------------------------
# Default credential-loader router
# ---------------------------------------------------------------------------


async def _default_loader(
    target: GitHubTargetLike,
    operator: Operator,
) -> GitHubAppCredentials | GitHubPATCredentials:
    """Single Vault read + payload-shape inspection (App vs PAT).

    Used when :class:`GitHubRestConnector` is constructed without an
    explicit ``credentials_loader``. Delegates to
    :func:`load_github_credentials_from_vault` which fetches the
    Vault payload once and picks the upstream protocol from the
    field shape — :class:`GitHubAppCredentials` when ``app_id`` +
    ``private_key`` + ``installation_id`` are all present;
    :class:`GitHubPATCredentials` when ``token`` is present (and the
    App fields are not); :class:`GitHubAmbiguousVaultPayloadError`
    when neither shape matches.

    The target's ``auth_model`` is not inspected here — the boundary
    that gates per_user / impersonation rows lives on
    :meth:`GitHubRestConnector.auth_headers`. G0.16-T2 (#1304)
    reconciled the historical "auth_model-driven routing" with the
    target-side enum (``shared_service_account`` only).
    """
    return await load_github_credentials_from_vault(target, operator)


def _is_acceptable_auth_model(value: object) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT model or unset.

    Mirrors the predicate :class:`VmwareRestConnector` uses at its
    boundary (the vmware-rest precedent the G0.16-T2 reconciliation
    aligned the gh-rest connector with). Accepts the enum member, the
    equivalent string, and ``None`` (the legacy-row sentinel for
    targets that predate the column-default backfill). Any other
    value (``"per_user"``, ``"impersonation"``, a typo, an int) is
    rejected by the caller.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


# ---------------------------------------------------------------------------
# Fingerprint shape helpers
# ---------------------------------------------------------------------------


def _extract_repo_path(host: str) -> str | None:
    """Return ``owner/repo`` when *host* carries repo coordinates.

    Accepts ``api.github.com/repos/owner/repo``,
    ``api.github.com/owner/repo``, and ``owner/repo`` shapes (the last
    is the operator-friendly form the v0.x onboarding doc uses). Returns
    ``None`` for any bare host (no slash after the FQDN) — the
    fingerprint falls back to ``GET /user/installations``.
    """
    if "/" not in host:
        return None
    # Strip any ``api.github.com`` / ``api.github.com/repos`` prefix; the
    # tail is ``owner/repo`` (or longer for nested URLs we ignore).
    parts = host.split("/", maxsplit=3)
    if len(parts) == 2:
        # Bare ``owner/repo`` — the operator-friendly form the v0.x
        # onboarding doc uses (no FQDN prefix).
        return f"{parts[0]}/{parts[1]}"
    # parts[0] is the host portion (api.github.com); the rest is path.
    if len(parts) < 3:
        return None
    if parts[1] == "repos" and len(parts) >= 4:
        # api.github.com/repos/owner/repo
        return f"{parts[2]}/{parts[3]}"
    # api.github.com/owner/repo
    return f"{parts[1]}/{parts[2]}"


def _fingerprint_from_installations(
    payload: Any,
    probed_at: datetime,
    probe_method: str,
) -> FingerprintResult:
    """Build a :class:`FingerprintResult` from ``GET /user/installations``.

    GitHub returns ``{"total_count": N, "installations": [...]}``. The
    first installation populates the operator-visible ``extras`` fields;
    the count appears as ``installations_count`` so the operator can
    confirm "yes my App reaches multiple installations" without a
    second call.
    """
    installations = (payload.get("installations") if isinstance(payload, dict) else None) or []
    total = (payload.get("total_count") if isinstance(payload, dict) else None) or len(
        installations
    )
    extras: dict[str, Any] = {"installations_count": int(total)}
    if installations:
        first = installations[0]
        account = first.get("account") if isinstance(first, dict) else None
        extras.update(
            {
                "installation_id": first.get("id") if isinstance(first, dict) else None,
                "installation_account": account.get("login") if isinstance(account, dict) else None,
                "target_type": first.get("target_type") if isinstance(first, dict) else None,
                "app_slug": first.get("app_slug") if isinstance(first, dict) else None,
                "permissions": first.get("permissions") if isinstance(first, dict) else None,
            }
        )
    return FingerprintResult(
        vendor="github",
        product="gh",
        version="v3",
        reachable=True,
        probed_at=probed_at,
        probe_method=probe_method,
        extras=extras,
    )


def _fingerprint_from_repo(
    payload: Any,
    probed_at: datetime,
    probe_method: str,
) -> FingerprintResult:
    """Build a :class:`FingerprintResult` from ``GET /repos/{owner}/{repo}``.

    Returns the repo's identity (``full_name``, ``id``, ``private``)
    plus the owner's account type — sufficient for an operator to
    confirm "yes my App is installed on this repo" without a second
    call. Inspired by vmware-rest's ``GET /api/about`` shape.
    """
    if not isinstance(payload, dict):
        return FingerprintResult(
            vendor="github",
            product="gh",
            reachable=False,
            probed_at=probed_at,
            probe_method=probe_method,
            extras={"error": f"unexpected response shape: {type(payload).__name__}"},
        )
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
    return FingerprintResult(
        vendor="github",
        product="gh",
        version="v3",
        reachable=True,
        probed_at=probed_at,
        probe_method=probe_method,
        extras={
            "repo_full_name": payload.get("full_name"),
            "repo_id": payload.get("id"),
            "repo_private": payload.get("private"),
            "owner_login": owner.get("login") if owner else None,
            "owner_type": owner.get("type") if owner else None,
            "default_branch": payload.get("default_branch"),
        },
    )


# Re-exports kept so ``mypy --strict`` doesn't flag unused imports — these
# names appear in this module's docstring and in the package-level
# ``__init__`` ``__all__`` for backwards-compatible surface. Listed here
# so a future maintainer who refactors imports doesn't accidentally
# trim them.
_ = (
    GitHubAmbiguousVaultPayloadError,
    GitHubAppNotInstalledError,
    GitHubInstallationTokenMintError,
    GitHubJWTMintError,
    GitHubRateLimitedError,
)
