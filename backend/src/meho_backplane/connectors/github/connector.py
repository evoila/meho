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

The connector supports two ``target.auth_model`` values, selected at the
:meth:`auth_headers` boundary:

* ``github-app`` — canonical machine-identity path. The connector reads
  App ID + RSA private key + installation ID from Vault under the
  operator's identity (G3.9 precedent), mints a 10-minute RS256 JWT, and
  exchanges it for an installation token via
  ``POST /app/installations/{id}/access_tokens``. The installation token
  is cached for 50 minutes (10-minute safety margin before the 1-hour
  upstream expiry) so a typical operator session of ~50 fingerprint /
  dispatch calls pays the mint round-trip exactly once.
* ``github-pat`` — fallback. A fine-grained Personal Access Token reads
  directly from Vault and passes through as the bearer credential.
  Documented but not first-class — T6 (#1226) explains the picker.

Per-target cache scoping mirrors vmware-rest: keyed on ``target.name``,
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
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.github.session import (
    DEFAULT_GITHUB_API_URL,
    GITHUB_APP_AUTH_MODEL,
    GITHUB_PAT_AUTH_MODEL,
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
    load_github_app_credentials_from_vault,
    load_github_pat_credentials_from_vault,
    mint_github_app_jwt,
)
from meho_backplane.connectors.schemas import (
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
        # Per-target installation-token cache. Keyed on target.name; the
        # value's ``expires_at_monotonic`` drives cache-validity checks.
        self._installation_tokens: dict[str, InstallationToken] = {}
        # Per-target PAT cache — same shape but the token never expires
        # from MEHO's side (the PAT carries its own GitHub-set TTL).
        # Caching keeps the Vault read off the hot path.
        self._pat_tokens: dict[str, str] = {}
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

        Selects between the GitHub App and PAT paths by the target's
        ``auth_model``. Any other value (``shared_service_account``,
        ``per_user``, ``impersonation``, ``None``, a typo) raises
        :exc:`NotImplementedError` naming the target and the requested
        mode — the same fail-closed shape :class:`VmwareRestConnector`
        uses at its boundary.

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
        auth_model = getattr(target, "auth_model", None)
        if auth_model == GITHUB_APP_AUTH_MODEL:
            token = await self._installation_token(target, operator)
            return {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        if auth_model == GITHUB_PAT_AUTH_MODEL:
            pat = await self._pat_token(target, operator)
            return {
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        raise NotImplementedError(
            f"GitHubRestConnector only supports auth_model="
            f"{GITHUB_APP_AUTH_MODEL!r} or {GITHUB_PAT_AUTH_MODEL!r}; "
            f"target {target.name!r} requested auth_model={auth_model!r}"
        )

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
    # GitHub App credential / token plumbing
    # ------------------------------------------------------------------

    async def _installation_token(
        self,
        target: GitHubTargetLike,
        operator: Operator,
    ) -> str:
        """Return the cached installation token; mint on cold cache.

        The lock serialises cold-cache callers; warm-cache reads take
        the fast path. The 50-minute cache window is bounded by
        :data:`INSTALLATION_TOKEN_CACHE_SECONDS`.

        Raises :class:`GitHubAppNotInstalledError`,
        :class:`GitHubJWTMintError`,
        :class:`GitHubInstallationTokenMintError`, or
        :class:`GitHubRateLimitedError` per the four T11 envelopes.
        """
        async with self._token_lock:
            cached = self._installation_tokens.get(target.name)
            now = time.monotonic()
            if cached is not None and cached.expires_at_monotonic > now:
                return cached.token

            creds = await self._credentials_loader(target, operator)
            if not isinstance(creds, GitHubAppCredentials):
                raise GitHubCredentialError(
                    f"github_credential_error: loader returned "
                    f"{type(creds).__name__} for target {target.name!r} "
                    f"with auth_model={GITHUB_APP_AUTH_MODEL!r}; expected "
                    f"GitHubAppCredentials. This is a loader-configuration "
                    f"bug. See docs/codebase/connectors-github.md."
                )

            jwt_token = mint_github_app_jwt(
                creds.app_id,
                creds.private_key_pem,
            )
            installation = await exchange_jwt_for_installation_token(
                jwt_token=jwt_token,
                installation_id=creds.installation_id,
                api_base_url=self._BASE_URL,
            )
            self._installation_tokens[target.name] = installation
            _log.info(
                "github_installation_token_minted",
                target=target.name,
                host=target.host,
                installation_id=creds.installation_id,
                upstream_expires_at=installation.upstream_expires_at,
            )
            return installation.token

    async def _pat_token(
        self,
        target: GitHubTargetLike,
        operator: Operator,
    ) -> str:
        """Return the cached PAT; read from Vault on cold cache.

        PATs do not expire on MEHO's side (the GitHub-set expiry is
        opaque to the connector), so cache lifetime is the connector
        instance lifetime. Operators rotating a PAT must restart the
        backplane or evict the cache via :meth:`aclose` followed by a
        fresh dispatch.
        """
        async with self._token_lock:
            cached = self._pat_tokens.get(target.name)
            if cached is not None:
                return cached
            creds = await self._credentials_loader(target, operator)
            if not isinstance(creds, GitHubPATCredentials):
                raise GitHubCredentialError(
                    f"github_credential_error: loader returned "
                    f"{type(creds).__name__} for target {target.name!r} "
                    f"with auth_model={GITHUB_PAT_AUTH_MODEL!r}; expected "
                    f"GitHubPATCredentials. This is a loader-configuration "
                    f"bug. See docs/codebase/connectors-github.md."
                )
            self._pat_tokens[target.name] = creds.token
            _log.info(
                "github_pat_token_loaded",
                target=target.name,
                host=target.host,
            )
            return creds.token

    # ------------------------------------------------------------------
    # Required Connector ABC methods
    # ------------------------------------------------------------------

    async def fingerprint(self, target: GitHubTargetLike) -> FingerprintResult:
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

        The fingerprint path synthesises a system operator (empty
        ``raw_jwt`` placeholder) so the wire format is exercised but
        the live Vault read still fails closed — same shape
        :meth:`VmwareRestConnector.fingerprint` uses.

        Transport / credential failures land as
        ``reachable=False`` with ``extras['error']`` carrying the
        error class + message (same pattern as
        :meth:`VmwareRestConnector.fingerprint`). The structured
        ``error_code`` from the T11 envelope (when one fired) is
        surfaced separately under ``extras['error_code']``.
        """
        probed_at = datetime.now(UTC)
        operator = synthesise_system_operator()
        repo_path = _extract_repo_path(target.host)
        if repo_path is None:
            probe_method = "GET /user/installations"
            path = "/user/installations"
        else:
            probe_method = f"GET /repos/{repo_path}"
            path = f"/repos/{repo_path}"

        try:
            payload = await self._get_json(target, path, operator=operator)
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

        The L2 catalog entry (T3 #1223) registers the ingested ops via
        the ``meho connector ingest --catalog gh/v3`` path; this
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
    """Pick the Vault loader matching ``target.auth_model``.

    Used when :class:`GitHubRestConnector` is constructed without an
    explicit ``credentials_loader``. Raises
    :exc:`NotImplementedError` for any unsupported ``auth_model`` —
    same boundary :meth:`GitHubRestConnector.auth_headers` enforces,
    surfaced earlier here for clarity in error attribution.
    """
    auth_model = getattr(target, "auth_model", None)
    if auth_model == GITHUB_APP_AUTH_MODEL:
        return await load_github_app_credentials_from_vault(target, operator)
    if auth_model == GITHUB_PAT_AUTH_MODEL:
        return await load_github_pat_credentials_from_vault(target, operator)
    raise NotImplementedError(
        f"GitHubRestConnector default loader requires auth_model="
        f"{GITHUB_APP_AUTH_MODEL!r} or {GITHUB_PAT_AUTH_MODEL!r}; "
        f"target {target.name!r} requested auth_model={auth_model!r}"
    )


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
    GitHubAppNotInstalledError,
    GitHubInstallationTokenMintError,
    GitHubJWTMintError,
    GitHubRateLimitedError,
)
