# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""NsxConnector -- hand-rolled HttpConnector subclass for NSX.

Auth + fingerprint + probe + the G0.6 dispatch shim, plus the #2302
typed audited-read shims. Registered against the v2 registry at
module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.nsx.__init__`; re-ingests against the
same ``(product="nsx", version="9.0", impl_id="nsx-rest")`` triple no-op.

VCF-9 version renumber (#1530)
------------------------------

NSX-T 4.x was renumbered onto the VCF train at VCF 9.0 -- a live VCF-9
appliance reports NSX 9.0.x. The :attr:`supported_version_range`
``>=4.0,<10.0`` keeps one class covering both the standalone NSX-T 4.x
line and the VCF-9-aligned 9.x line; dispatch and the ingest
version-range pre-flight key on the
:class:`packaging.specifiers.SpecifierSet`, not the class-pinned
:attr:`version`. Same posture :class:`VmwareRestConnector` took for the
vSphere 8.x -> 9.0 jump.

Auth divergence from the vSphere precedent
------------------------------------------

NSX rejects HTTP Basic on the canonical FQDN behind the VCF 9 envoy
proxy; session-cookie + X-XSRF-TOKEN is the only mode that works across
both VCF 9 and standalone NSX-T (per ``scripts/nsx.sh`` in the consumer
wrapper repo). The flow:

1. ``POST /api/session/create`` with **form-encoded** ``j_username`` /
   ``j_password`` (``client.post(url, data=<dict>)``; NOT JSON, NOT
   HTTP Basic).
2. The response ``Set-Cookie: JSESSIONID=...`` lands in
   :attr:`httpx.AsyncClient.cookies` automatically, so subsequent
   requests through the same per-target client carry the cookie.
3. The response ``X-XSRF-TOKEN`` header is cached in
   :attr:`_session_tokens`, keyed on the tenant-unique
   ``(tenant_id, target.id)`` tuple (#1642) so two same-named targets in
   different tenants never share a cached session.
4. :meth:`auth_headers` returns ``{"X-XSRF-TOKEN": <cached>}`` on
   subsequent calls; the cookie travels via the client jar.
5. On HTTP 401 from a downstream call,
   :meth:`_get_json_with_session_retry` invalidates the cached token +
   client cookies, re-establishes the session, and retries once. A
   second 401 raises :exc:`RuntimeError` naming the target (re-login
   once, not a loop) so a bad credential pair fails fast.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT`
(or ``None`` for pre-G0.3 targets); :meth:`auth_headers` rejects any
other value with a :exc:`NotImplementedError` naming the target + mode.
Per-user and impersonation modes are deferred to v0.2.next. The
operator's validated Keycloak JWT is forwarded through
:meth:`auth_headers` -> :meth:`_session_token` -> the
:class:`NsxSessionLoader` so the live loader reads the per-target Vault
secret under the operator's identity (G3.10-T1 #945); the NSX session
establish itself stays HTTP-form against the resolved service account.

Session lifecycle
-----------------

NSX sessions have a documented idle timeout (~30 min); the connector
does not proactively refresh -- the 401-retry layer re-establishes on
demand. :meth:`aclose` clears the caches and tears down the httpx pool
but issues no DELETE-revoke: a network-call-per-target during lifespan
exit is more risk than benefit under Kubernetes'
``terminationGracePeriod``.

Operations
----------

The audited read set (#2302) ships as **typed** ops
(``source_kind="typed"``) via the bound-method shims below, registered
through :mod:`meho_backplane.connectors.nsx.typed_ops`; they dispatch on
a fresh boot with zero catalog ingest. The remaining reads stay as
ingested browse breadth, enable-able through the generic review flow
(``ReviewService.enable_reads``). The G0.6 :meth:`execute`
shim remains for ABC compatibility.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.nsx.session import (
    NsxSessionLoader,
    NsxTargetLike,
    is_acceptable_auth_model,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["NsxConnector"]

_log = structlog.get_logger(__name__)

# NSX session-establish endpoint. POST with form-encoded
# ``j_username`` / ``j_password``; success returns 200 with the
# ``Set-Cookie: JSESSIONID=...`` and ``X-XSRF-TOKEN: ...`` response
# headers. Per the consumer wrapper at
# https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/nsx.sh
# and the NSX REST API guide.
_SESSION_CREATE_PATH = "/api/session/create"

# Header NSX expects on every authenticated request (except session
# create itself). The XSRF token is paired with the JSESSIONID cookie;
# either one alone is rejected.
_XSRF_HEADER = "X-XSRF-TOKEN"

# Form-body keys NSX's session-create endpoint expects. Lifted to
# module constants so the call site in :meth:`_session_token` reads
# as ``data={_FORM_USERNAME_KEY: username, _FORM_PASSWORD_KEY: password}``
# rather than carrying the magic strings inline.
_FORM_USERNAME_KEY = "j_username"
_FORM_PASSWORD_KEY = "j_password"


class NsxConnector(HttpConnector):
    """NSX REST connector with session-cookie + XSRF auth.

    Per-target XSRF token cached in :attr:`_session_tokens`; the
    accompanying ``JSESSIONID`` cookie is held by the per-target
    ``httpx.AsyncClient`` instance's cookie jar (httpx auto-extracts
    Set-Cookie response headers, so the cookie is reused on subsequent
    requests through the same client without manual plumbing).

    The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the
    same triple (e.g. a stale ingest before this class's module
    imports) loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"nsx-rest-9.0"`` -> (``"nsx"``, ``"9.0"``, ``"nsx-rest"``). The
    # version pin tracks the VCF-9-aligned product line (#1530); the
    # ``>=4.0,<10.0`` range keeps the standalone NSX-T 4.x line
    # dispatchable through the same class.
    product = "nsx"
    version = "9.0"
    impl_id = "nsx-rest"
    supported_version_range = ">=4.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        session_loader: NsxSessionLoader | None = None,
    ) -> None:
        super().__init__()
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: NsxSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: NsxTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"X-XSRF-TOKEN": <token>}`` for the request.

        Lazily establishes the session on first call against *target*;
        subsequent calls reuse the cached token. The full ``operator`` is
        forwarded to :meth:`_session_token` so the live default loader
        (G3.10-T1 #945) can read the per-target secret under the
        operator's identity (``vault_client_for_operator(operator)``).
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` selects the
        Vault-sourced service account once the loader has resolved it;
        the operator's JWT only authenticates the read, not the NSX
        session itself.

        The JSESSIONID cookie that pairs with this XSRF token lives in
        the per-target client's cookie jar
        (:attr:`httpx.AsyncClient.cookies`); httpx attaches it
        automatically on subsequent requests through the same client.

        Raises :exc:`NotImplementedError` (with ``target.name`` and the
        requested mode in the message) if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"NsxConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {_XSRF_HEADER: token}

    async def _session_token(self, target: NsxTargetLike, operator: Operator) -> str:
        """Return the cached XSRF token for *target*, establishing one on first use.

        The lock serialises concurrent first-use for one target; the
        cache fast-path means subsequent callers are bounded only by
        the lock acquisition itself. The slow
        ``POST /api/session/create`` call runs under the lock so two
        concurrent first-use callers against the same target don't both
        pay the round-trip cost.

        The response carries ``X-XSRF-TOKEN`` as a header (cached here)
        and ``Set-Cookie: JSESSIONID=...`` which the per-target httpx
        client jar captures automatically. The response body is not
        used.

        ``operator`` is forwarded to the
        :class:`NsxSessionLoader` so the default loader can read the
        per-target Vault secret under the operator's identity
        (G3.10-T1's live read). The default loader is the thin
        nsx-specific entry point to the shared operator-context Vault
        read; injected test loaders accept the same
        ``(target, operator)`` pair.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None:
                return cached
            creds = await self._session_loader(target, operator)
            try:
                username = creds["username"]
                password = creds["password"]
            except KeyError as exc:
                # Surface a clear error if the loader returned a dict
                # missing one of the two required keys -- a typo in a
                # production loader implementation otherwise surfaces
                # as a confusing KeyError deep inside the form encoder.
                raise RuntimeError(
                    f"nsx session loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            client = await self._http_client(target)
            try:
                resp = await client.post(
                    _SESSION_CREATE_PATH,
                    data={_FORM_USERNAME_KEY: username, _FORM_PASSWORD_KEY: password},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Wrap so the operator-facing message names the target;
                # httpx's default str() shows only the URL/status, which
                # loses the per-target identification the dispatcher's
                # audit row needs.
                raise RuntimeError(
                    f"nsx session establish failed for target {target.name!r}: "
                    f"POST {_SESSION_CREATE_PATH} returned HTTP {exc.response.status_code}"
                ) from exc
            xsrf: str | None = resp.headers.get(_XSRF_HEADER)
            if not xsrf:
                # NSX guarantees the XSRF header on success; absence
                # signals a misbehaving proxy or a wrong endpoint
                # (probably HTTP Basic against /api/session/create
                # silently 200-ing on a stale appliance). Fail loudly.
                raise RuntimeError(
                    f"nsx session establish for target {target.name!r}: "
                    f"POST {_SESSION_CREATE_PATH} returned 2xx with no "
                    f"{_XSRF_HEADER} response header"
                )
            self._session_tokens[cache_key] = xsrf
            _log.info(
                "nsx_session_established",
                target=target.name,
                host=target.host,
            )
            return xsrf

    async def invalidate_session(self, target: NsxTargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path.

        The seam the generic dispatch path calls on an auth-class status
        (NSX's 401) before re-dispatching once (G0.29-T2 #2067) -- the path
        the typed read ops (#2302) traverse. Delegates to
        :meth:`_invalidate_session`, mirroring the vmware-rest / vcf-logs seam.
        """
        await self._invalidate_session(target)

    async def _invalidate_session(self, target: NsxTargetLike) -> None:
        """Drop the cached XSRF token + clear the client cookie jar for *target*.

        Called by :meth:`_get_json_with_session_retry` on 401 from a
        downstream call, and by the public :meth:`invalidate_session`
        dispatch-path hook (#2067), so the subsequent :meth:`_session_token`
        re-issues ``POST /api/session/create`` from a clean state.
        Holds the lock so a concurrent re-establish doesn't race with
        the invalidation.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            self._session_tokens.pop(cache_key, None)
            # The shared ``HttpConnector._clients`` pool is keyed on the
            # tenant-unique ``(tenant_id, id)`` prefix plus the
            # ``verify_tls`` dimension (evoila/meho#1682/#1774), so build
            # the full key the base would to index this tenant's
            # host-bound client and clear exactly its cookie jar.
            client = self._clients.get(self._client_cache_key(target))
            if client is not None:
                client.cookies.clear()

    async def _get_json_with_session_retry(
        self,
        target: NsxTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *path* with single 401 -> re-login -> retry-once recovery.

        Wraps the inherited :meth:`HttpConnector._get_json` (which
        carries tenacity's connection-error + 5xx retry decorator);
        invokes it via ``super()._get_json`` so the ``.retry`` attribute
        on the base method is preserved for retry-aware
        introspection.

        On 401 from the inherited call, invalidates the cached XSRF
        token + the client cookie jar and re-tries once. A second 401
        raises :exc:`RuntimeError` naming the target -- the consumer
        wrapper's posture: re-login once on session-expiry, not a
        retry loop.
        """
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            await self._invalidate_session(target)
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise RuntimeError(
                    f"nsx session re-login failed for target {target.name!r}: "
                    f"GET {path} returned HTTP 401 after refresh"
                ) from exc
            raise

    async def fingerprint(
        self,
        target: NsxTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /api/v1/node``.

        The session is fetched lazily by :meth:`auth_headers` (called
        transitively through
        :meth:`HttpConnector._request_json`). The GET goes through
        :meth:`_get_json_with_session_retry` so an expired session
        (401) triggers one re-login before the result is reported.

        On transport, status, or session-establish failure, returns a
        non-reachable :class:`FingerprintResult` whose
        ``extras["error"]`` carries the exception class + message --
        same pattern :class:`VmwareRestConnector` established so the
        operator's first ``meho connector fingerprint`` against an
        unreachable NSX gets a structured response rather than a
        stack trace.

        ``operator`` (optional) is the request-scoped operator forwarded
        from the probe routes. When provided, the session credentials
        loader reads the per-target Vault secret under that identity --
        the same code path the dispatch surface uses. ``None`` falls
        back to a system operator whose placeholder JWT fails closed
        at the live Vault round-trip. G0.16-T4 (#1306) converged probe
        + dispatch on this signature; pre-fix the probe path hard-coded
        the placeholder JWT and surfaced as the v0.8.0 dogfood's
        ``malformed jwt: must have three parts`` finding on ``vcf9-nsx``.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json_with_session_retry(
                target, "/api/v1/node", operator=eff_operator
            )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="nsx",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /api/v1/node",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product="nsx",
            version=payload.get("node_version"),
            build=payload.get("kernel_version"),
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /api/v1/node",
            extras={
                "node_uuid": payload.get("node_uuid"),
                "hostname": payload.get("hostname"),
                "external_id": payload.get("external_id"),
            },
        )

    async def probe(self, target: NsxTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` rather than running a
        separate probe path. The issue body permits the implementer
        to pick between (a) the heavier fingerprint delegation or (b)
        a lighter ``GET /api/v1/cluster/status`` call; the delegation
        path is chosen here for parity with the vSphere precedent
        (one auth round-trip already covers both reachability and
        auth-challenge, so a separate cluster-status call would add
        round-trip cost without changing the boolean ``ok``).
        ``probe()`` therefore inherits :meth:`fingerprint`'s 401-retry
        layer transparently.
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
        target: NsxTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`VmwareRestConnector.execute`'s shape: the
        connector's ABC :meth:`Connector.execute` predates the G0.6
        operator-aware dispatch path, so this shim exists for
        pre-G0.6 callers. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI
        verbs once #615 lands) construct a real :class:`Operator` and
        call :func:`meho_backplane.operations.dispatch` directly.

        The shim synthesises a minimal :class:`Operator` carrying a
        nil-UUID tenant_id + a fixed system sentinel ``sub``; the
        connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"nsx-rest-9.0"`` -> (product=``"nsx"``, version=``"9.0"``,
        impl_id=``"nsx-rest"``).
        """
        # Lazy import -- meho_backplane.operations.dispatch transitively
        # imports the connector registry which imports this module at
        # package import time; deferring keeps that initialisation
        # order stable.
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:nsx-rest-connector-shim",
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

    # Typed read ops (#2302): thin bound-method shims delegating to
    # ``nsx.typed_reads`` bodies (kept in a sibling module for the
    # file-length budget). All read-only; a raw 401 propagates to the
    # dispatcher's #2067 arm (see :meth:`invalidate_session`).

    async def node_status(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.node.status`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_node_status_impl

        return await nsx_node_status_impl(self, operator, target, params)

    async def cluster_status(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.cluster.status`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_cluster_status_impl

        return await nsx_cluster_status_impl(self, operator, target, params)

    async def backup_config(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.backup.config`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_backup_config_impl

        return await nsx_backup_config_impl(self, operator, target, params)

    async def backup_status(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.backup.status`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_backup_status_impl

        return await nsx_backup_status_impl(self, operator, target, params)

    async def transport_zone_list(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.transport_zone.list`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_transport_zone_list_impl

        return await nsx_transport_zone_list_impl(self, operator, target, params)

    async def tier1_list(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.tier1.list`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_tier1_list_impl

        return await nsx_tier1_list_impl(self, operator, target, params)

    async def alarm_list(
        self, operator: Operator, target: NsxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``nsx.alarm.list`` shim (#2302)."""
        from meho_backplane.connectors.nsx.typed_reads import nsx_alarm_list_impl

        return await nsx_alarm_list_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Clear cached XSRF tokens, then tear down the httpx pool.

        No DELETE-revoke is issued -- NSX's session has a documented
        idle timeout, and a per-target network call during lifespan
        shutdown is more risk than benefit (a hung DELETE on an
        unreachable target would trip Kubernetes' 30-second
        terminationGracePeriod). The token cache is cleared so a
        post-aclose reuse of the same connector instance (e.g. a test
        that builds one connector across two contexts) starts clean.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await super().aclose()
