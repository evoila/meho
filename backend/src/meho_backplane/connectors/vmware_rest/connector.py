# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VmwareRestConnector — hand-rolled HttpConnector subclass for vSphere REST.

Replaces the future :class:`GenericRestConnector` auto-shim that G0.7's
ingestion pipeline synthesises on first ingest of ``vcenter.yaml``. The
auto-shim makes the connector resolvable so ingestion can land the
``endpoint_descriptor`` rows; this class makes those ops dispatchable.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vmware_rest.__init__`. The auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`
once #408's pipeline lands in main) then no-ops on subsequent ingests
against the same ``(product="vmware", version="9.0",
impl_id="vmware-rest")`` triple.

Per-target sessions
-------------------

The class caches one ``vmware-api-session-id`` token per ``target.name``.
First call to :meth:`auth_headers` against a given target invokes the
:class:`VsphereSessionLoader` (default
:func:`load_session_credentials_from_vault`) for the service-account
credentials, then issues ``POST /api/session`` with HTTP basic auth. If
the modern endpoint responds with HTTP 404, the connector retries
against the legacy ``POST /rest/com/vmware/cis/session`` path before
declaring failure — real vCenter serves both, but the upstream
``vmware/vcsim`` simulator (used by the integration test in T8) wires
the handler under the legacy path only. The successful endpoint is
cached per-target so :meth:`aclose` revokes against the same path. The
JSON-string-body response (or legacy ``{"value": "<token>"}`` shape) is
the session token; subsequent calls reuse the cached value. Per-target
isolation is the load-bearing invariant: two targets must never share a
session token even if their names collide across tenants — the cache is
keyed on the tenant-unique ``(tenant_id, target.id)`` tuple via the
shared :func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
helper (#1642/#1672), so two same-named targets in different tenants
never collapse onto one cached session.

The session-establish flow runs under an :class:`asyncio.Lock` so two
concurrent first-use callers against the same target don't both POST to
``/api/session``. The lock is held only across the cache check + token
fetch + cache write; subsequent reads after the cache is populated take
the fast path under the same lock and exit immediately.

Session lifecycle
-----------------

vSphere's default idle timeout is ~5 minutes; the connector does not
proactively refresh tokens. The dispatcher's tenacity decorator on
:meth:`HttpConnector._request_json` retries connection errors and 5xx
responses but not 401 — a 401 from a subsequent call would surface to
the caller. Explicit 401-driven session refresh is intentionally
deferred to v0.2.next (per the task body's *Out of scope* section);
operator-facing dispatch sees re-authentication as a clean retry
through the dispatcher's caller-side retry path rather than a hidden
retry inside the connector.

:meth:`aclose` revokes every cached session via ``DELETE`` against the
endpoint that minted the token (modern ``/api/session`` for production,
legacy ``/rest/com/vmware/cis/session`` for vcsim-served targets) before
closing the per-target httpx clients. A revoke failure is logged and
proceeds — the operator-facing concern at shutdown is "tear down the
httpx pool"; an in-flight 5xx during DELETE doesn't block that.

Auth model gating
-----------------

The task body's *Session lifecycle* section locks v0.2 to
:attr:`AuthModel.SHARED_SERVICE_ACCOUNT`. :meth:`auth_headers` rejects
any other ``target.auth_model`` value with a clear :exc:`NotImplementedError`
that names both the target and the requested mode. ``None`` is accepted
because targets that predate G0.3's ``auth_model`` column legitimately
have no value — the column defaults to the shared-service-account model
once G0.3 ships, but until then ``None`` is the "no model declared,
fall back to v0.2 default" sentinel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import SESSION_TOKEN_OBJECT_KEY
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import session_establish_auth_error
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vmware_rest._mount import (
    SESSION_PATH_LEGACY,
    SESSION_PATH_MODERN,
    adapt_filter_params,
    api_mount_for_session_path,
    mounted_path,
)
from meho_backplane.connectors.vmware_rest.session import (
    VsphereSessionLoader,
    VsphereTargetLike,
    load_session_credentials_from_vault,
)

__all__ = ["VmwareRestConnector", "product_from_line_id"]

_log = structlog.get_logger(__name__)

# vmware-api-session-id header name per Broadcom's vSphere Automation
# API security schema (Basic / API-key / Bearer). The same header
# carries the session token across both vCenter REST (vcenter.yaml-
# sourced ops) and vi-json (vi-json.yaml-sourced ops once #503 lands),
# per docs/vcenter-9.0/MANIFEST.md. Lifted to a module constant so the
# revoke path in aclose() and the auth path in _session_token can't
# drift apart.
_SESSION_HEADER = "vmware-api-session-id"

# vSphere 8.0+'s /api/session POST returns the session token as a JSON
# string body (e.g. ``"abc123def456"``). Older 6.7/7.0 vCenter via the
# deprecated /rest/com/vmware/cis/session path returned
# ``{"value": "abc123def456"}``. The class's supported_version_range
# is ``">=8.5,<10.0"`` so the JSON-string shape is the load-bearing
# one, but :meth:`_extract_session_token` handles both defensively —
# vcsim has been known to swap between shapes between minor releases,
# and a defensive read here costs nothing. The object-shape key is the
# shared :data:`SESSION_TOKEN_OBJECT_KEY` so the typed and profiled
# (``session_login_basic``) extractors can't drift apart (#2047).
_SESSION_TOKEN_OBJECT_KEY = SESSION_TOKEN_OBJECT_KEY

# Session endpoints + the spec-relative-op → /api-or-/rest mount
# mapping live in ``._mount`` (extracted to keep this module within
# the code-quality size budget; see that module's docstring for the
# full modern-vs-legacy + vcsim rationale). ``SESSION_PATH_MODERN`` /
# ``SESSION_PATH_LEGACY`` drive session establishment + the
# ``aclose()`` revoke; ``mounted_path`` maps an ingested descriptor
# path onto the mount the target's established session selected.

# Verbs that go through HttpConnector._request_json's tenacity retry
# decorator. Non-idempotent verbs (POST / PUT / PATCH / DELETE) route
# through _post_json instead — see HttpConnector for the policy.
# Lifted here so :meth:`auth_headers`-level callers can introspect.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def product_from_line_id(line_id: str) -> str:
    """Map vCenter's ``product_line_id`` to the canonical product slug.

    ``GET /api/about`` returns a ``product_line_id`` like ``"vpx"`` for
    vCenter, ``"embeddedEsx"`` / ``"esx"`` for ESXi. The canonical
    fingerprint shape demands ``product="vcenter"`` / ``"esxi"`` per
    the consumer's wrapper contract. Unknown values fall through to
    the raw line_id so an ESXi-on-Arm or a future vCenter rebrand is
    still recorded faithfully rather than misclassified as
    ``"unknown"``.
    """
    if line_id == "vpx":
        return "vcenter"
    if line_id in ("embeddedEsx", "esx"):
        return "esxi"
    return line_id or "unknown"


def _extract_session_token(payload: Any, target_name: str) -> str:
    """Coerce a ``POST /api/session`` JSON response to the session token string.

    Handles the two shapes vSphere has shipped across recent releases:

    * **JSON string body** — vSphere 7.0+ modern ``/api/session`` returns
      the token as a JSON-quoted string. ``response.json()`` returns
      :class:`str`.
    * **JSON object body** — pre-7.0 ``/rest/com/vmware/cis/session``
      returned ``{"value": "<token>"}``. Some vcsim builds straddle the
      two shapes; supporting the legacy shape defensively keeps the
      integration test green across simulator versions.

    Anything else raises :exc:`RuntimeError` with the target name in
    the message so the operator can identify the misbehaving endpoint.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        value = payload.get(_SESSION_TOKEN_OBJECT_KEY)
        if isinstance(value, str):
            return value
    raise RuntimeError(
        f"unexpected /api/session response shape for target {target_name!r}: "
        f"got {type(payload).__name__} (expected str or "
        f"{{'{_SESSION_TOKEN_OBJECT_KEY}': str}})"
    )


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3
    targets). Any other value (``"per_user"``, ``"impersonation"``,
    a typo, an int) is rejected by the caller.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


class VmwareRestConnector(HttpConnector):
    """vSphere REST connector for vCenter 8.5+ / ESXi 8.5+ targets.

    Per-target session cached in ``self._session_tokens`` keyed on the
    tenant-unique ``(tenant_id, target.id)`` tuple (#1642/#1672); token
    established on first call to :meth:`auth_headers` via
    ``POST /api/session`` with HTTP basic (service-account creds from
    the injectable :class:`VsphereSessionLoader`); revoked on
    :meth:`aclose` via ``DELETE /api/session``.

    The :attr:`priority` is set to ``1`` so a future :class:`GenericRestConnector`
    auto-shim that somehow registers for the same triple (e.g. a stale
    ingest before this class's module imports) loses the registry's
    tie-break ladder. The auto-shim's idempotency check should prevent
    that case in practice; the priority is defence in depth.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"vmware-rest-9.0"`` -> (``"vmware"``, ``"9.0"``, ``"vmware-rest"``).
    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=8.5,<10.0"
    # Outranks the GenericRestConnector auto-shim's priority=0 if both
    # somehow register for the same triple; the idempotency check in
    # ensure_connector_class_registered should make this unreachable
    # in production, but a defence-in-depth tie-break keeps the
    # resolver behaviour deterministic if the check is ever bypassed.
    priority = 1

    def __init__(
        self,
        *,
        session_loader: VsphereSessionLoader | None = None,
    ) -> None:
        super().__init__()
        # Keyed on the tenant-unique ``(tenant_id, target.id)`` tuple
        # (``target_cache_key``) so two same-named targets in different
        # tenants never share a cached session (#1642/#1672).
        self._session_tokens: dict[tuple[str, str], str] = {}
        # Tracks which session endpoint minted each cached token so
        # :meth:`aclose` can DELETE against the same path. Production
        # vCenter serves both ``/api/session`` and the legacy
        # ``/rest/com/vmware/cis/session``; vcsim serves only the legacy
        # path. See ``SESSION_PATH_MODERN`` / ``SESSION_PATH_LEGACY``
        # for the rationale and source citations. Keyed on the same
        # tenant-unique tuple as ``_session_tokens``.
        self._session_paths: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: VsphereSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: VsphereTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"vmware-api-session-id": <token>}`` for the request.

        Lazily establishes the session on first call against *target*;
        subsequent calls reuse the cached token. The full ``operator`` is
        threaded to the :class:`VsphereSessionLoader` so the default
        loader (G3.9-T3's :func:`load_session_credentials_from_vault`)
        can read the service-account credentials from Vault under the
        operator's identity (``vault_client_for_operator(operator)``). An
        injected test loader receives the same ``(target, operator)``
        pair.

        Raises :exc:`NotImplementedError` (with ``target.name`` and the
        requested mode in the message) if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        Per-user and impersonation modes are deferred to v0.2.next.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VmwareRestConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {_SESSION_HEADER: token}

    async def mount_op_path(self, target: VsphereTargetLike, path: str, operator: Operator) -> str:
        """Map a spec-relative ingested-op *path* onto *target*'s live mount.

        Overrides the identity :meth:`HttpConnector.mount_op_path` hook
        the dispatcher calls for ``source_kind='ingested'`` ops. Ingested
        descriptors carry spec-relative paths (``/vcenter/vm``); the
        vCenter REST API is mounted at ``/api`` on modern vCenter and
        ``/rest`` on legacy vCenter / vcsim. Establishing the session is
        what records the live mount in :attr:`_session_paths` (the
        modern→legacy 404 fallback in :meth:`_session_token`); it's
        idempotent + cached, so calling it here costs nothing on the
        warm path and is what lets the *first* op against a legacy-only
        target (vcsim) mount correctly instead of defaulting to ``/api``
        and 404ing. The pure mapping — including the already-mounted
        pass-through — lives in :func:`._mount.mounted_path`.

        ``operator`` is the dispatch op's operator; it is forwarded to
        :meth:`_session_token` so a cold-cache session establish here
        authenticates under the same identity the subsequent transport
        call will.

        This is a dedicated dispatcher hook rather than a
        ``_request_json`` / ``_post_json`` override on purpose: those
        carry tenacity's ``@retry`` (and the ``.retry`` attribute that
        retry-aware tests + callers introspect), and ``fingerprint()``
        reaches ``GET /api/about`` through ``_get_json`` *pre-session*
        — overriding the transport methods would both strip ``.retry``
        and force a spurious session establish on the pre-auth probe.
        """
        await self._session_token(target, operator)
        session_path = self._session_paths.get(target_cache_key(target), SESSION_PATH_MODERN)
        return mounted_path(session_path, path)

    async def adapt_op_query(
        self,
        target: VsphereTargetLike,
        query: Mapping[str, Any] | None,
        operator: Operator,
    ) -> dict[str, Any] | None:
        """Key a ``filter.*`` query bucket off *target*'s live mount flavor.

        The composite sub-call seam (:func:`._read._read_sub_op`) and the
        typed-op listing legs (:func:`.typed_ops.host_usage_impl`,
        :func:`.typed_ops_host_network_uplinks.host_network_uplinks_impl`)
        author their query params in the legacy ``/rest`` style
        (``filter.datastores``, ``filter.hosts``, ...). Modern ``/api``
        vCenter 8.x returns HTTP 400 for that prefixed form and expects the
        bare parameter name; the legacy ``/rest`` mount (and ``vmware/vcsim``)
        requires the prefix. Resolve the live mount the same way
        :meth:`mount_op_path` does — off the established session — and
        delegate the pure key rewrite to :func:`._mount.adapt_filter_params`.

        The session establish is idempotent + cached (mirrors
        :meth:`mount_op_path`), so calling this right after a
        ``mount_op_path`` at the same call site costs nothing on the warm
        path; ``operator`` is forwarded so a cold-cache establish
        authenticates under the dispatch op's identity. Empty / ``None``
        query short-circuits to ``None`` (no session establish needed) so
        an unfiltered listing stays a bare, param-less GET.
        """
        if not query:
            return None
        await self._session_token(target, operator)
        session_path = self._session_paths.get(target_cache_key(target), SESSION_PATH_MODERN)
        return adapt_filter_params(api_mount_for_session_path(session_path), query)

    async def _session_token(self, target: VsphereTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, establishing one on first use.

        The lock serialises concurrent first-use for one target; the
        cache fast-path means subsequent callers are bounded only by
        the lock acquisition itself. The slow ``POST /api/session`` call
        runs under the lock so two concurrent first-use callers against
        the same target don't both pay the round-trip cost.

        Endpoint fallback: POSTs to the modern ``/api/session`` first;
        on HTTP 404 (only) falls back to ``/rest/com/vmware/cis/session``.
        Real vCenter serves both paths, so production targets succeed on
        the first attempt; the upstream ``vmware/vcsim`` simulator
        registers only the legacy path (per ``govmomi/vapi/simulator``)
        and exercises the fallback. The successful path is recorded in
        ``self._session_paths`` so :meth:`aclose` DELETEs the matching
        endpoint. 401 / 403 / 5xx on the modern path are *not* retried
        on the legacy path — those are auth/server failures, not "this
        deployment doesn't have the modern endpoint".

        ``operator`` is forwarded to the
        :class:`VsphereSessionLoader` so the credential read runs under
        the operator's identity (G3.9-T3's live read). The default loader
        (:func:`load_session_credentials_from_vault`) performs that live
        operator-context Vault read; injected test loaders accept the
        same ``(target, operator)`` pair.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`.
        The primary fail-closed gate against empty ``raw_jwt`` is the
        loader's ``vault_client_for_operator`` / ``load_basic_credentials``
        call chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return a cached vSphere
        session token to an unauthenticated caller via a cache hit.
        :meth:`auth_headers` enforces only the ``auth_model`` boundary
        (rejects ``per_user`` / ``impersonation`` under
        ``shared_service_account`` scoping). Raised before the cache lookup
        so a primed token from an authenticated caller cannot leak to a
        system-initiated caller. See ``docs/architecture/connector-auth.md``
        § "Cache scoping under ``shared_service_account``" for the contract.
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
            return await self._establish_and_cache_session(target, operator, cache_key)

    async def _establish_and_cache_session(
        self,
        target: VsphereTargetLike,
        operator: Operator,
        cache_key: tuple[str, str],
    ) -> str:
        """Establish a fresh vSphere session for *target* and cache it.

        Called by :meth:`_session_token` under ``self._session_lock`` on a
        cold cache. Resolves credentials via the loader, POSTs to the modern
        session endpoint (falling back to the legacy path on a 404 only),
        and records the token and the endpoint that minted it against the
        tenant-unique *cache_key* — the same key the shared
        ``HttpConnector._clients`` pool now uses (evoila/meho#1682), so
        :meth:`aclose` can locate the per-target client directly by
        *cache_key* without a name reverse-map.
        """
        creds = await self._session_loader(target, operator)
        client = await self._http_client(target)
        try:
            username = creds["username"]
            password = creds["password"]
        except KeyError as exc:
            # Surface a clear error if the loader returned a dict
            # missing one of the two required keys — a typo in a
            # production loader implementation otherwise surfaces
            # as a confusing TypeError deep inside httpx's auth
            # builder.
            raise RuntimeError(
                f"vsphere session loader for target {target.name!r} returned "
                f"a dict missing required key {exc.args[0]!r}; need "
                "{'username': str, 'password': str}"
            ) from exc
        auth = (username, password)
        resp = await client.post(SESSION_PATH_MODERN, auth=auth)
        established_path = SESSION_PATH_MODERN
        if resp.status_code == 404:
            # Modern endpoint not served (vcsim, very old vCenter,
            # or a reverse-proxy that hasn't been updated). Try the
            # legacy path before declaring failure.
            resp = await client.post(SESSION_PATH_LEGACY, auth=auth)
            established_path = SESSION_PATH_LEGACY
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Wrap so the operator-facing message names the target;
            # httpx's default str() shows only the URL/status, which
            # loses the per-target identification the dispatcher's
            # audit row needs. The path in the message is the last
            # one attempted, which distinguishes a real 404 (legacy
            # also missing) from auth/server failure on the modern
            # path.
            message = (
                f"vsphere session establish failed for target {target.name!r}: "
                f"POST {established_path} returned HTTP {exc.response.status_code}"
            )
            # #2329: a 401 (rotated/stale password) / 403 (locked-out account)
            # at establish is an auth-class failure -- raise the structured
            # ``ConnectorAuthError`` the dispatcher maps to
            # ``connector_auth_failed`` (restage-the-credential remediation)
            # instead of the opaque ``connector_error: RuntimeError``. A real
            # 404 / 5xx keeps the bare RuntimeError shape.
            raise (
                session_establish_auth_error(exc, message=message, target=target)
                or RuntimeError(message)
            ) from exc
        token = _extract_session_token(resp.json(), target.name)
        self._session_tokens[cache_key] = token
        self._session_paths[cache_key] = established_path
        _log.info(
            "vsphere_session_established",
            target=target.name,
            host=target.host,
            session_path=established_path,
        )
        return token

    # #2396: vmware_rest deliberately exposes NO ``invalidate_credentials``
    # hook. It caches only the session token (evicted below); the
    # service-account credentials are re-read from Vault via ``_session_loader``
    # on every ``_establish_and_cache_session``, so a restage already converges
    # on the next cold-session dispatch with no credential cache to evict.
    async def invalidate_session(self, target: VsphereTargetLike) -> None:
        """Evict the cached session token + login path for *target*.

        The duck-typed recovery hook the generic-ingested dispatch path calls
        on an auth-class status (401 / vRLI's 440) before re-dispatching the
        op once (G0.29-T2 #2067). Dropping the cached token forces the next
        :meth:`_session_token` to miss the cache and re-run
        :meth:`_establish_and_cache_session`, which re-authenticates and
        re-runs the modern->legacy ``/api/session`` 404 fallback from a clean
        state -- the path that recovers vCenter's cold-401 (the freshly minted
        token expired server-side) without a backplane restart.

        Evicts under ``self._session_lock`` keyed on the tenant-unique
        ``target_cache_key(target)`` tuple, so the per-``(tenant_id,
        target.id)`` isolation (#1642/#1672/#1684) holds across eviction and
        re-establish: two same-named targets in different tenants never share
        or clobber each other's cache slot. The recorded login path is dropped
        alongside the token so the re-establish rediscovers the live endpoint.
        The credentials are not touched -- a 401/440 means the *session token*
        expired or was rejected, not that the service-account credential is
        wrong. The hook is a no-op when no token is cached.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            self._session_tokens.pop(cache_key, None)
            self._session_paths.pop(cache_key, None)

    async def fingerprint(
        self,
        target: VsphereTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /api/about``.

        The session token is fetched lazily by :meth:`auth_headers`
        (called transitively through :meth:`HttpConnector._request_json`).
        On transport or status failure, returns a non-reachable
        ``FingerprintResult`` whose ``extras["error"]`` carries the
        exception class + message — same pattern the K8s connector
        established for ``probe()`` failures, plumbed here through
        ``fingerprint()`` so the operator's first ``meho connector
        fingerprint`` call against an unreachable vCenter gets a
        structured response rather than a stack trace.

        ``operator`` (optional) is the request-scoped operator forwarded
        from the probe routes. When provided, the underlying
        :class:`VsphereSessionLoader` reads the per-target Vault secret
        under that identity — the same code path the dispatch surface
        uses. ``None`` falls back to a system operator whose placeholder
        JWT is rejected by the live Vault loader, preserving the
        fail-closed system-call carve-out. G0.16-T4 (#1306) converged
        probe + dispatch on this signature; pre-fix the probe path
        hard-coded the placeholder JWT and surfaced as the v0.8.0
        dogfood's ``malformed jwt: must have three parts`` finding.
        """
        probed_at = datetime.now(UTC)
        # Forward the route operator when present; fall back to the
        # system operator for background callers. The session loader's
        # fail-closed guard rejects the placeholder JWT at the live
        # Vault round-trip, so the system-call carve-out still holds
        # when no real operator is in scope.
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json(target, "/api/about", operator=eff_operator)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            # RuntimeError catches the session-establish failures from
            # :meth:`_session_token` so an unauthenticatable target
            # surfaces as a clean ``reachable=False`` fingerprint
            # rather than propagating the wrapped exception.
            return FingerprintResult(
                vendor="vmware",
                product="vcenter",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /api/about",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product=product_from_line_id(payload.get("product_line_id", "")),
            version=payload.get("version"),
            build=payload.get("build"),
            edition=payload.get("license_product_name"),
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /api/about",
            extras={
                "uuid": payload.get("instance_uuid"),
                "full_name": payload.get("full_name"),
                "product_line_id": payload.get("product_line_id"),
                "api_type": payload.get("api_type"),
                "os_type": payload.get("os_type"),
            },
        )

    async def probe(self, target: VsphereTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` rather than running a separate
        probe path. The chassis registry's readiness probe and the
        operator-facing ``meho connector probe`` both want a single
        boolean ``ok`` + a reason string; ``fingerprint`` already produces
        the right shape and the extra latency from the ``/api/about``
        payload parsing is negligible compared to the auth round-trip
        ``fingerprint`` already incurs.
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
        target: VsphereTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`VaultConnector.execute`'s shape: the connector's
        ABC :meth:`Connector.execute` predates the G0.6 operator-aware
        dispatch path, so this shim exists for pre-G0.6 callers (the
        chassis ``/api/v1/connectors/{product}/{op_id}`` route, any
        :func:`meho_backplane.connectors.resolver.resolve_connector`
        consumer that doesn't already construct an :class:`Operator`).

        Post-G0.6 callers (``/api/v1/operations/call``, MCP
        ``call_operation``, the CLI verbs from #511) construct a real
        :class:`Operator` and call :func:`meho_backplane.operations.dispatch`
        directly — they don't reach this method.

        The shim synthesises a minimal :class:`Operator` carrying a
        nil-UUID tenant_id + a fixed system sentinel ``sub``; typed-
        registrations are always ``tenant_id IS NULL`` in
        ``endpoint_descriptor`` so the dispatcher's tenant-scoped lookup
        falls through to the global row regardless of the synthesised
        value. The dispatcher's audit row records the synthesised
        identity; the real operator identity (when present) lands on
        the audit row written by :class:`AuditMiddleware` upstream of
        this call.
        """
        # Lazy import — meho_backplane.operations.dispatch transitively
        # imports the connector registry which imports this module at
        # package import time; deferring keeps that initialisation
        # order stable.
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vmware-rest-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        # Encode the connector's natural key as the dispatcher's
        # connector_id string per parse_connector_id's contract:
        # ``"vmware-rest-9.0"`` -> (product=``"vmware"``, version=``"9.0"``,
        # impl_id=``"vmware-rest"``).
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def host_usage(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.usage`` -- per-host CPU/memory load + hardware + maintenance.

        The first vmware **typed** op (``source_kind="typed"``): a bound
        method the dispatcher binds to this connector instance and invokes
        with ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        per-host ``summary.quickStats`` / ``summary.hardware`` /
        ``runtime.inMaintenanceMode`` directly on the connector session via
        PropertyCollector -- no ``dispatch_child``, no ingested descriptor
        -- so it works on a fresh boot with zero catalog ingest. The plain
        REST host summary reports only liveness, not load.

        Delegates to :func:`~meho_backplane.connectors.vmware_rest.typed_ops.host_usage_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"hosts": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops import host_usage_impl

        return await host_usage_impl(self, operator, target, params)

    async def host_network_uplinks(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.network_uplinks`` -- per-host pnic link state + uplinks.

        A ``source_kind="typed"`` op (#2258, re-shipped from the former
        ``vmware.composite.host.network_uplinks``): the dispatcher binds
        this method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Lists
        hosts then reads ``config.network.pnic`` +
        ``config.network.proxySwitch`` per host via PropertyCollector
        directly on the connector session -- no ``dispatch_child``, no
        ingested descriptor -- so it works on a fresh boot with zero
        catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks.host_network_uplinks_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"hosts": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks import (
            host_network_uplinks_impl,
        )

        return await host_network_uplinks_impl(self, operator, target, params)

    async def host_vsan_health(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.vsan_health`` -- per-cluster vSAN health roll-up.

        A ``source_kind="typed"`` op (#2258, re-shipped from the former
        ``vmware.composite.host.vsan_health``): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`).
        Queries ``VsanQueryVcClusterHealthSummary`` on the
        ``vsan-cluster-health-system`` singleton scoped to the target
        cluster's MoRef, directly on the connector session -- no
        ``dispatch_child``, no ingested descriptor -- so it works on a
        fresh boot with zero catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health.host_vsan_health_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns
        ``{"cluster": ..., "overall_health": ..., "groups": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
            host_vsan_health_impl,
        )

        return await host_vsan_health_impl(self, operator, target, params)

    async def vm_info(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.vm.info`` -- single-VM power / guest IP / Tools / heartbeat / usage.

        A ``source_kind="typed"`` incident-triage read (#2300): the
        dispatcher binds this method to the connector instance and invokes
        it with ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`).
        Reads the VirtualMachine managed object's ``runtime.powerState``,
        ``guest.*``, ``guestHeartbeatStatus``, and
        ``storage.perDatastoreUsage`` via PropertyCollector directly on the
        connector session -- no ``dispatch_child``, no ingested descriptor
        -- so it works on a fresh boot with zero catalog ingest. Addresses
        the VM by ``vm`` moid or ``name``.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_vm_info.vm_info_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns a single flat row.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import vm_info_impl

        return await vm_info_impl(self, operator, target, params)

    async def object_collect(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.object.collect`` -- bounded generic PropertyCollector read.

        A ``source_kind="typed"`` op (#2300): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        the caller-specified property paths off a single ``(type, moid)``
        object via PropertyCollector directly on the connector session --
        no ``dispatch_child``, no ingested descriptor -- so it works on a
        fresh boot with zero catalog ingest. Bounded by ``parameter_schema``
        (one object, no traversal, <=64 paths); an oversized request is a
        structured ``invalid_params`` error before the read is issued.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_object_collect.object_collect_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{type, moid, properties, missing}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_object_collect import (
            object_collect_impl,
        )

        return await object_collect_impl(self, operator, target, params)

    async def tasks_recent(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.tasks.recent`` -- recent vCenter Task objects.

        A ``source_kind="typed"`` op (#2300): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        ``TaskManager.recentTask`` then ``Task.info`` via PropertyCollector
        directly on the connector session -- no ``dispatch_child``, no
        ingested descriptor -- so it works on a fresh boot with zero
        catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent.tasks_recent_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"tasks": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
            tasks_recent_impl,
        )

        return await tasks_recent_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Revoke every cached session before closing the httpx pool.

        Issues ``DELETE`` against each per-target client at the session
        path recorded by :meth:`_session_token` (modern ``/api/session``
        for production vCenter, legacy ``/rest/com/vmware/cis/session``
        for targets where the modern path 404'd at establish time) before
        delegating to :meth:`HttpConnector.aclose`. A revoke failure
        (5xx, transport error, target unreachable at shutdown) is logged
        and proceeds — the operator-facing concern at shutdown is "tear
        down the httpx pool", and a hung DELETE on an unreachable target
        would otherwise block lifespan exit long enough to trip
        Kubernetes' 30-second terminationGracePeriod.

        The DELETE is issued before :meth:`super().aclose` so the
        cached client is still pooled when we need it. After the
        revoke loop, the parent close runs unchanged.
        """
        async with self._session_lock:
            tokens = dict(self._session_tokens)
            paths = dict(self._session_paths)
            self._session_tokens.clear()
            self._session_paths.clear()
        for cache_key, token in tokens.items():
            # ``_session_tokens`` is keyed on the tenant-unique
            # ``(tenant_id, target.id)`` tuple, while the shared
            # ``HttpConnector._clients`` pool keys that same prefix plus a
            # ``verify_tls`` dimension (evoila/meho#1682/#1774). The token
            # was minted against exactly one per-target client, so match
            # the pool entry whose key starts with this token's
            # ``(tenant_id, id)`` prefix — no name reverse-map needed.
            client = next(
                (
                    pooled
                    for client_key, pooled in self._clients.items()
                    if client_key[: len(cache_key)] == cache_key
                ),
                None,
            )
            if client is None:
                # Theoretically unreachable — every cached token was
                # established against a per-target client that was
                # created during _session_token. Defensive: skip
                # cleanly if the invariant ever drifts.
                continue
            # Use the same endpoint that minted the token. ``paths``
            # is populated in lock-step with ``_session_tokens`` in
            # ``_session_token``; the default keeps shutdown safe if
            # a future code path ever caches a token without recording
            # its endpoint.
            revoke_path = paths.get(cache_key, SESSION_PATH_MODERN)
            try:
                resp = await client.request(
                    "DELETE",
                    revoke_path,
                    headers={_SESSION_HEADER: token},
                )
                # Log non-2xx but don't raise — shutdown proceeds.
                if resp.status_code >= 400:
                    _log.warning(
                        "vsphere_session_revoke_non_2xx",
                        target=cache_key,
                        status_code=resp.status_code,
                        session_path=revoke_path,
                    )
            except (httpx.HTTPError, OSError) as exc:
                _log.warning(
                    "vsphere_session_revoke_failed",
                    target=cache_key,
                    error=f"{type(exc).__name__}: {exc}",
                    session_path=revoke_path,
                )
        await super().aclose()
