# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcfLogsConnector -- HttpConnector subclass for VCF Operations for Logs (vRLI) 9.x.

Skeleton-only -- session-token auth + fingerprint + probe + the G0.6
dispatch shim. Operations arrive in #834 via G0.7 spec ingestion against
``vcf-logs-9.0/openapi.yaml``.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vcf_logs.__init__` under
``(product="vrli", version="9.0", impl_id="vrli-rest")`` -- the
dispatch-canonical product :func:`parse_connector_id` derives from the
``vrli-rest`` impl_id, so an operator target carrying the natural
``product="vrli"`` token (what ``meho connector list`` emits) resolves
*this* connector rather than an auto-shim. G0.26-T4 (#1798) brought the
identity into round-trip compliance, retiring the historical
``product="vcf-logs"`` namespace split.

Auth contract -- verified against the consumer wrapper
``scripts/vcf-logs.sh`` (2026-05-21 snapshot) and the vRLI 9.x REST API
documentation. The wrapper is the authoritative contract for the field
shapes the appliance actually accepts:

* Login endpoint: ``POST /api/v2/sessions`` -- ``vcf-logs.sh`` lines
  103-121 + 192-208 (probe path).
* Request body: JSON
  ``{"username": ..., "password": ..., "provider": "Local"}`` with
  ``Content-Type: application/json`` -- ``vcf-logs.sh`` lines 124-130.
  The provider field defaults to ``"Local"`` (``vcf-logs.sh`` line 95);
  ``"ActiveDirectory"`` and ``"vIDM"`` are the documented alternatives,
  but only ``Local`` + ``ActiveDirectory`` are supported in v0.2
  (per #369).
* Response body: ``{"sessionId": "<token>", "ttl": <seconds>}`` --
  ``vcf-logs.sh`` lines 142-147 extracts ``.sessionId``.
* Downstream auth header: ``Authorization: Bearer <sessionId>`` --
  ``vcf-logs.sh`` lines 257-261 (op invocations) + lines 210-213 (probe
  GET /api/v2/version).
* Version endpoint: ``GET /api/v2/version`` -- documented unauthenticated
  per the issue body; the wrapper sends Bearer defensively but the
  appliance accepts the call without it.

References
----------

* Issue: https://github.com/evoila/meho/issues/830
* Wrapper: https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-logs.sh
* vRLI API: https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT`
(or ``None`` for pre-G0.3 targets). :meth:`auth_headers` rejects any
other value with a clear :exc:`NotImplementedError` naming both the
target and the requested mode -- same posture the NSX, SDDC Manager,
and VCF Automation precedents established.

Session-expiry retry-once contract
----------------------------------

On a session-expiry status from a downstream call,
:meth:`_get_json_with_session_retry` invalidates the cached session
token, re-establishes via ``POST /api/v2/sessions``, and retries the
original call **once**. vRLI signals an expired session two ways and the
connector recovers from both (:data:`_SESSION_EXPIRED_STATUSES`):

* **440** -- vRLI's own ``trait.authenticated.440``: *"the session ID has
  expired; obtain a new session ID from ``/api/v2/sessions``"*. This is
  the recoverable case the appliance emits once its in-memory session
  idle-times out -- the one that bites scheduled / long-running consumers.
* **401** -- ``trait.authenticated.401``: missing/invalid
  ``Authorization`` header or session ID.

A second session-expiry status (440 or 401) raises :exc:`RuntimeError`
naming the target -- the wrapper's posture: re-login once on
session-expiry, not a retry loop. Same shape the NSX precedent
established.

Session lifecycle
-----------------

vRLI's session has a documented TTL (default 30 days but operator-tunable
via ``/api/v2/sessions/`` config) and also idle-expires; the connector
does NOT proactively refresh. The session-expiry retry layer above
re-establishes on demand -- a 440 (idle-expired session) on the next call
triggers a re-login + retry rather than failing until restart.
:meth:`aclose` clears the in-memory token + credentials caches and tears
down the httpx pool but does NOT issue a DELETE-revoke -- same posture
NSX takes (revoke-on-close is v0.2.next).

Operations
----------

This module ships zero operations -- the G0.6 dispatch shim
:meth:`execute` exists for ABC compatibility but operations land in
the ``endpoint_descriptor`` table via #834's spec ingestion. Until
then, the connector is registered and discoverable but
``execute(target, op_id, ...)`` against any ``op_id`` will resolve to
"unknown operation" at the dispatcher layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import SESSION_SCHEME_SPECS
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import (
    CredentialsCache,
    SessionLoginError,
    is_acceptable_auth_model,
    vcf_session_login,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.profile import split_version
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcf_logs.profile import VRLI_EXECUTION_PROFILE
from meho_backplane.connectors.vcf_logs.session import (
    VcfCredentialsLoader,
    VcfLogsTargetLike,
    load_credentials_from_vault,
)

# Re-export ``SessionLoginError`` so callers that catch the helper's
# structured failure don't have to reach into the shared module.
__all__ = ["SessionLoginError", "VcfLogsConnector"]

_log = structlog.get_logger(__name__)

# vRLI session-establish endpoint. POST with JSON body
# ``{username, password, provider}``; success returns 200 with a JSON
# body carrying ``sessionId`` + ``ttl``. Per the consumer wrapper at
# https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-logs.sh
# Sourced from the reviewed ``vrli_session`` ExecutionProfile (G0.28-T8
# #1974) via the named ``session_login`` scheme's vetted login-path builder
# so the typed connector and a profiled connector share one declaration of
# the session endpoint rather than two literals that could drift.
_SESSION_CREATE_PATH = SESSION_SCHEME_SPECS[VRLI_EXECUTION_PROFILE.auth.scheme].login_path(
    VRLI_EXECUTION_PROFILE.auth
)

# Unauthenticated version endpoint for fingerprint + probe. Returns JSON
# ``{version, releaseName}`` where ``version`` is e.g.
# ``"9.0.0.0.21761695"`` (dot-separated). The wrapper splits the value
# at dots and reports ``parts[0:3]`` as the public version + ``parts[4]``
# as the build; we mirror that shape in :class:`FingerprintResult`. The
# path comes from the profile's fingerprint recipe (single source of truth).
_VERSION_PATH = VRLI_EXECUTION_PROFILE.fingerprint.path

# Default vRLI identity-source name. Matches the wrapper's
# ``PROVIDER="Local"`` default; ``ActiveDirectory`` / ``vIDM`` are
# permitted alternatives a target may declare via
# :attr:`VcfLogsTargetLike.provider`. The profile's ``session_login``
# scheme hardcodes ``"Local"``; the typed connector keeps the per-target
# override the declarative scheme deliberately does not model.
_DEFAULT_PROVIDER = "Local"

# Downstream statuses that mean "the cached session token is no longer
# accepted; re-login and retry once" -- vRLI's ``trait.authenticated.440``
# (session expired, "obtain a new session ID") and ``.401`` (missing or
# invalid Authorization). Both feed
# :meth:`VcfLogsConnector._get_json_with_session_retry`; see the module
# "Session-expiry retry-once contract" docstring above for why 440 is the
# case that bites (#1909). Sourced from the profile's ``expiry_statuses``
# (G0.28-T7 #1973) so the typed connector and the dispatcher's auth-class
# arm narrow the same closed set from one declaration.
_SESSION_EXPIRED_STATUSES: frozenset[int] = VRLI_EXECUTION_PROFILE.expiry_statuses


def _vrli_payload_builder(
    provider: str,
) -> Callable[[str, str], dict[str, Any]]:
    """Return a payload-builder closure binding *provider* into the body.

    The shared :func:`vcf_session_login` helper's
    :data:`SessionPayloadBuilder` is ``(username, password) -> dict``;
    vRLI also needs ``provider``, so we close over the per-target value
    at call time. Lifting this to a module helper keeps the
    :meth:`_session_token` body uncluttered.
    """

    def _build(username: str, password: str) -> dict[str, Any]:
        return {
            "username": username,
            "password": password,
            "provider": provider,
        }

    return _build


def _parse_vrli_version(version_full: Any) -> tuple[str | None, str | None, str | None]:
    """Render a vRLI ``version`` string as ``(public, build, patch)``.

    The ``(public, build)`` split is the profile's ``vrli_five_part`` named
    splitter (G0.28-T6 #1972) — the same vetted parser a profiled vRLI
    connector uses, so the typed connector and the profile cannot disagree
    on the public version / build for any input. ``patch`` (``parts[3]``)
    is the one component the named splitter deliberately drops; it stays
    bespoke typed-only enrichment surfaced in ``fingerprint``'s ``extras``,
    mirroring the wrapper at ``vcf-logs.sh`` lines 226-251.

    Returns ``(None, None, None)`` if *version_full* is not a non-empty
    string -- the appliance returning a malformed response shouldn't
    crash the fingerprint round-trip (the splitter is equally tolerant).
    """
    version, build = split_version(
        VRLI_EXECUTION_PROFILE.fingerprint.version_splitter,
        version_full if isinstance(version_full, str) else None,
    )
    if version is None:
        return None, None, None
    parts = version_full.split(".")
    patch = parts[3] if len(parts) > 3 else None
    return version, build, patch


def _extract_session_id(resp: httpx.Response) -> str | None:
    """Pull ``sessionId`` out of the vRLI session-create response body.

    The session-create POST returns ``{"sessionId": "<token>",
    "ttl": <seconds>}``; the wrapper at ``vcf-logs.sh`` line 142 extracts
    ``.sessionId`` via ``jq`` and aborts when the field is empty or
    ``null``. The shared :func:`vcf_session_login` helper treats a
    ``None``-or-empty return from this extractor as
    :exc:`SessionLoginError`, so this function never has to raise
    itself -- it returns ``None`` for the missing / null / empty case
    and lets the helper produce the consistent target-named error.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("sessionId")
    if not isinstance(value, str) or not value:
        return None
    return value


class VcfLogsConnector(HttpConnector):
    """vRLI 9.x REST connector with session-token Bearer auth.

    Per-target session token cached in :attr:`_session_tokens`;
    per-target credentials cached in :attr:`_credentials` (the shared
    :class:`CredentialsCache` instance). The :attr:`priority` is set to
    ``1`` so a future ``GenericRestConnector`` auto-shim that somehow
    registers for the same triple loses the registry's tie-break ladder.

    Profile-derived auth + fingerprint (G0.28-T8 #1974)
    ===================================================

    The connector's declarative auth + fingerprint surfaces are sourced
    from the reviewed
    :data:`~meho_backplane.connectors.vcf_logs.profile.VRLI_EXECUTION_PROFILE`
    rather than hand-coded literals: the session-create path comes from the
    profile's ``session_login`` scheme spec, the version endpoint + the
    ``(public, build)`` split come from the profile's fingerprint recipe
    (the ``vrli_five_part`` named splitter), and the session-expiry status
    set is the profile's ``expiry_statuses`` (``{401, 440}``). This makes
    the profile the single source of truth shared with a profiled vRLI
    connector — the capstone of Initiative #1965, with per-method dispatch
    parity proven in the integration lane (``tests/integration/
    test_connectors_vrli_profile_parity.py``).

    Two surfaces stay typed-only because the declarative profile cannot
    model them: the per-target ``provider`` (``ActiveDirectory`` / ``vIDM``;
    the ``session_login`` scheme hardcodes ``"Local"``) and the fingerprint
    ``extras`` (``release_name`` / ``version_full`` / ``patch``). The
    ``ResultHandle`` large-result path is the connector-agnostic JSONFlux
    dispatch mechanism, not connector code, and is untouched.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # round-trips through the dispatcher's parse_connector_id contract:
    # ``"vrli-rest-9.0"`` -> (``"vrli"``, ``"9.0"``, ``"vrli-rest"``), so
    # the registered ``product`` equals the dispatch-canonical token the
    # connector listing emits and an ingested op dispatches here rather
    # than to a shadowing auto-shim (G0.26-T4 #1798).
    product = "vrli"
    version = "9.0"
    impl_id = "vrli-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: VcfCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._credentials = CredentialsCache(
            credentials_loader if credentials_loader is not None else load_credentials_from_vault,
            product_label="vrli",
        )

    async def auth_headers(self, target: VcfLogsTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <session_id>"}`` for the request.

        Lazily establishes the session on first call against *target*;
        subsequent calls reuse the cached token. The full ``operator`` is
        threaded into :meth:`_session_token` so the live credentials
        loader (the default
        :func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`)
        reads the per-target KV-v2 secret under the operator's Vault
        Identity entity via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator` --
        the locked Option A decision.

        Raises :exc:`NotImplementedError` (with ``target.name`` and the
        requested mode in the message) if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcfLogsConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: VcfLogsTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, establishing on first use.

        The lock serialises concurrent first-use callers for one target;
        the cache fast-path means subsequent callers are bounded only by
        the lock acquisition itself. The slow
        ``POST /api/v2/sessions`` call runs under the lock so two
        concurrent first-use callers against the same target don't both
        pay the round-trip cost.

        Credentials are sourced from the shared :class:`CredentialsCache`
        which raises :exc:`RuntimeError` naming the target if the loader
        returns a dict missing ``"username"`` or ``"password"``. The
        login round-trip itself delegates to the shared
        :func:`vcf_session_login` helper, which wraps non-2xx responses
        in :exc:`SessionLoginError` and chains the underlying
        :exc:`httpx.HTTPStatusError`. Caller catches both as
        :exc:`RuntimeError` (``SessionLoginError`` is a
        ``RuntimeError`` subclass) for parity with the NSX precedent's
        error shape.

        ``operator`` is forwarded to the shared
        :class:`CredentialsCache` so the live default loader reads the
        per-target Vault secret under the operator's Vault Identity
        entity (G3.10-T2's live read). Injected test loaders accept the
        same ``(target, operator)`` pair.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`
        and the sibling check in :class:`CredentialsCache.get`. The primary
        fail-closed gate against empty ``raw_jwt`` is the loader's
        ``vault_client_for_operator`` / ``load_basic_credentials`` call
        chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return a cached vRLI bearer
        to an unauthenticated caller via a cache hit. :meth:`auth_headers`
        enforces only the ``auth_model`` boundary (rejects ``per_user`` /
        ``impersonation`` under ``shared_service_account`` scoping).
        Raised before the cache lookup so a primed token from an
        authenticated caller cannot leak to a system-initiated caller.
        See ``docs/architecture/connector-auth.md`` § "Cache scoping under
        ``shared_service_account``" for the contract.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None:
                return cached
            creds = await self._credentials.get(target, operator)
            provider = getattr(target, "provider", None) or _DEFAULT_PROVIDER
            client = await self._http_client(target)
            token = await vcf_session_login(
                client,
                _SESSION_CREATE_PATH,
                username=creds["username"],
                password=creds["password"],
                target_name=target.name,
                payload_builder=_vrli_payload_builder(provider),
                token_extractor=_extract_session_id,
                request_headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            self._session_tokens[cache_key] = token
            _log.info(
                "vrli_session_established",
                target=target.name,
                host=target.host,
                provider=provider,
            )
            return token

    async def _invalidate_session(self, target: VcfLogsTargetLike) -> None:
        """Drop the cached session token for *target*.

        Called by :meth:`_get_json_with_session_retry` on a session-expiry
        status (440 or 401) from a downstream call so the subsequent
        :meth:`_session_token` re-issues ``POST /api/v2/sessions`` from a
        clean state. Holds the lock so a concurrent re-establish doesn't
        race with the invalidation. Credentials cache is left intact -- a
        440/401 means the *session token* expired or was rejected, not
        that the credentials are wrong.
        """
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def _get_json_with_session_retry(
        self,
        target: VcfLogsTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *path* with single session-expiry -> re-login -> retry-once recovery.

        Wraps the inherited :meth:`HttpConnector._get_json` (which
        carries tenacity's connection-error + 5xx retry decorator);
        invokes it via ``super()._get_json`` is unnecessary -- this
        method just calls ``_get_json`` directly and the retry policy
        on the base method runs transparently.

        On a **session-expiry** status (:data:`_SESSION_EXPIRED_STATUSES`
        -- vRLI's ``440`` *or* ``401``) from the inherited call,
        invalidates the cached session token and re-tries once. ``440`` is
        the case that bites in practice: it is vRLI's own
        ``trait.authenticated.440`` -- *"the session ID has expired;
        obtain a new session ID from ``/api/v2/sessions``"* -- emitted once
        the appliance idle-times out the session. The cached token is
        stale, not the credential, so a re-login recovers it. A second
        session-expiry status (440 or 401) raises :exc:`RuntimeError`
        naming the target -- the wrapper's posture: re-login once on
        session-expiry, not a retry loop. Same shape the NSX precedent
        established.
        """
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _SESSION_EXPIRED_STATUSES:
                raise
            await self._invalidate_session(target)
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SESSION_EXPIRED_STATUSES:
                raise RuntimeError(
                    f"vrli session re-login failed for target {target.name!r}: "
                    f"GET {path} returned HTTP {status_code} after refresh"
                ) from exc
            raise

    async def fingerprint(
        self,
        target: VcfLogsTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from unauthenticated ``GET /api/v2/version``.

        The version endpoint is unauthenticated -- the wrapper's probe
        mode auths first defensively but the appliance accepts the
        call without a session token. The connector goes the cleaner
        route: an unauthenticated client request against
        ``/api/v2/version``, so a vRLI with valid TLS but broken
        credentials still produces a reachable fingerprint reporting
        the version + build. This is the same posture vSphere /
        Automation take for their unauthenticated version probes.

        Failure shape mirrors NSX: ``reachable=False`` with
        ``extras["error"]`` carrying ``"<ExcType>: <message>"`` so the
        operator's first ``meho connector fingerprint`` against an
        unreachable vRLI gets a structured response rather than a
        stack trace.

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — vRLI's
        version endpoint is unauthenticated, so the route operator
        plays no role here.
        """
        del operator  # unused — unauthenticated probe, no Vault read
        probed_at = datetime.now(UTC)
        probe_method = f"GET {_VERSION_PATH}"
        try:
            client = await self._http_client(target)
            resp = await client.get(
                _VERSION_PATH,
                headers={"Accept": "application/json"},
                # #2002: honour the per-target TLS SNI / cert-verify name
                # override on this unauthenticated fingerprint round-trip
                # too, so a vRLI that pins its cert to an FQDN while
                # demanding ``Host: <IP>`` is reachable with
                # ``verify_tls=true``. Empty dict (the default) leaves the
                # SNI / verify name derived from ``base_url`` as before.
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="vrli",
                reachable=False,
                probed_at=probed_at,
                probe_method=probe_method,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        version_full = payload.get("version") if isinstance(payload, dict) else None
        release_name = payload.get("releaseName") if isinstance(payload, dict) else None
        version, build, patch = _parse_vrli_version(version_full)
        return FingerprintResult(
            vendor="vmware",
            product="vrli",
            version=version,
            build=build,
            reachable=True,
            probed_at=probed_at,
            probe_method=probe_method,
            extras={
                "release_name": release_name,
                "version_full": version_full,
                "patch": patch,
            },
        )

    async def probe(self, target: VcfLogsTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` rather than running a
        separate probe path. Same posture the NSX / VCF Automation
        precedents established: one unauthenticated round-trip covers
        both reachability and (transitively) "the appliance is
        responding on /api/v2", which is enough for the boolean
        ``ok``. Probe-time auth-challenge is intentionally not run --
        a target whose creds are wrong still has reachable=true on
        the version endpoint, and that's the right answer for "is the
        appliance up".
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
        target: VcfLogsTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`NsxConnector.execute` /
        :meth:`VcfAutomationConnector.execute`'s shape: the connector's
        ABC :meth:`Connector.execute` predates the G0.6 operator-aware
        dispatch path, so this shim exists for pre-G0.6 callers.
        Post-G0.6 callers (``/api/v1/operations/call``, MCP
        ``call_operation``, the CLI verbs once #838 lands) construct a
        real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly.

        The shim synthesises a minimal :class:`Operator` carrying a
        nil-UUID tenant_id + a fixed system sentinel ``sub``; the
        connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"vrli-rest-9.0"`` -> (product=``"vrli"``,
        version=``"9.0"``, impl_id=``"vrli-rest"``).
        """
        # Lazy import -- meho_backplane.operations.dispatch transitively
        # imports the connector registry which imports this module at
        # package import time; deferring keeps that initialisation
        # order stable.
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vrli-rest-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def aclose(self) -> None:
        """Clear cached session tokens + credentials, then tear down the httpx pool.

        No DELETE-revoke is issued -- vRLI's session has a documented
        idle timeout, and a per-target network call during lifespan
        shutdown is more risk than benefit (same posture the NSX
        precedent established). Token cache is cleared so a
        post-aclose reuse of the same connector instance (e.g. a test
        that builds one connector across two contexts) starts clean;
        credentials cache is cleared so secrets don't outlive the
        connector instance.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await self._credentials.clear()
        await super().aclose()
