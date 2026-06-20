# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcfAutomationConnector -- hand-rolled HttpConnector subclass for VCF Automation 9.x.

Skeleton-only -- dual-plane auth + fingerprint + probe + the G0.6
dispatch shim + vhost routing. Operations arrive in #836 via G0.7
spec ingestion against both ``vcf-automation-9.0/provider.yaml`` +
``vcf-automation-9.0/tenant.yaml`` ingested under one connector with
``spec_source`` tags distinguishing them -- same dual-source shape as
vSphere's ``vcenter.yaml`` + ``vi-json.yaml``.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vcf_automation.__init__`.

Auth divergence: dual-plane on one appliance
--------------------------------------------

VCFA 9.x exposes two API planes on the same appliance (verified
against the consumer ``scripts/vcf-automation.sh`` wrapper -- header
comment + login blocks, 2026-05-21):

* **Provider plane** (vCloud-Director-derived): paths under
  ``/cloudapi/*`` and the classic ``/api/*`` family. Login is
  ``POST /cloudapi/1.0.0/sessions/provider`` with **HTTP Basic auth**;
  the response carries the access token as the
  ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` response **header** (a JWT). The
  connector caches that JWT per target and sends
  ``Authorization: Bearer <jwt>`` on subsequent ``/cloudapi/*`` and
  ``/api/*`` calls. ``Accept`` is path-family-dependent (#517 in the
  consumer repo, validated 2026-05-16):
  ``application/json;version=9.0.0`` for ``/cloudapi/*`` and
  ``application/*+json;version=40.0`` for the classic ``/api/*``
  family. The provider Bearer JWT authenticates both surfaces
  uniformly; only the ``Accept`` differs.

* **Tenant plane** (Aria-IaaS-derived): paths under ``/iaas/api/*``.
  Login is ``POST /iaas/api/login`` with a **JSON body**
  (``{"username": ..., "password": ...}`` plus optional ``domain``);
  the response body is ``{"token": "..."}``. Subsequent ``/iaas/api/*``
  calls carry ``Authorization: Bearer <token>`` and ``Accept:
  application/json``. Tokens are bespoke per plane; the provider
  JWT does NOT authenticate the tenant plane and vice versa.

Both per-plane token caches are keyed on the tenant-unique
``(tenant_id, target.id)`` tuple (``target_cache_key``, #1642/#1672) --
so two same-named targets in different tenants never share a cached
token -- and established lazily on first request that resolves to that plane.
On HTTP 401 from a downstream call, the relevant plane's cache is
invalidated and the call retries once -- consumer wrapper posture:
re-login once on session-expiry, not a retry loop.

Vhost routing
-------------

VCFA 9.x enforces strict ``Host:`` header matching. When ``target.fqdn``
is set, the per-target httpx ``AsyncClient`` is built with
``base_url=https://<fqdn>``; standard DNS resolution targets the FQDN.
When ``target.fqdn`` is unset and ``target.host`` is an IP literal,
:func:`._routing.compose_base_url` raises
:exc:`VcfAutomationConfigurationError` at first session-establish --
the consumer wrapper documents this as the silent-404 failure mode.
When ``target.host`` is itself an FQDN, ``fqdn`` is optional -- the
URL host already carries the right vhost.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT`
(or ``None`` for pre-G0.3 targets). :meth:`auth_headers` rejects any
other value with a clear :exc:`NotImplementedError` naming both the
target and the requested mode -- same posture the NSX, SDDC Manager,
and vSphere precedents established.

Operations
----------

This module ships zero operations -- the G0.6 dispatch shim
:meth:`execute` exists for ABC compatibility but operations land in
the ``endpoint_descriptor`` table via #836's dual-plane spec
ingestion. Until then, the connector is registered and discoverable
but ``execute(target, op_id, ...)`` against any ``op_id`` resolves to
"unknown operation" at the dispatcher layer.
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
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcf_automation._auth import (
    load_credentials_with_override,
    tenant_login,
    vcfa_provider_login,
)
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_VERSION_PATH,
    TENANT_ACCEPT,
    TENANT_VERSION_PATH,
    Plane,
    VcfAutomationConfigurationError,
    compose_base_url,
    is_acceptable_auth_model,
    plane_for_path,
    provider_accept_for_path,
)
from meho_backplane.connectors.vcf_automation.session import (
    VcfAutomationCredentialsLoader,
    VcfAutomationTargetLike,
    load_credentials_from_vault,
)

__all__ = ["VcfAutomationConfigurationError", "VcfAutomationConnector"]

_log = structlog.get_logger(__name__)


class VcfAutomationConnector(HttpConnector):
    """VCF Automation 9.x REST connector with dual-plane Bearer-token auth.

    Two independent per-target token caches:

    * :attr:`_provider_tokens` -- ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT
      values, established by ``POST /cloudapi/1.0.0/sessions/provider``
      with HTTP Basic.
    * :attr:`_tenant_tokens` -- ``{"token": "..."}`` body values,
      established by ``POST /iaas/api/login`` with a JSON body.

    Each plane has its own ``asyncio.Lock`` so concurrent first-use
    callers serialise per plane and don't double-POST the login
    endpoint. The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the
    same triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"vcfa-rest-9.0"`` -> (``"vcfa"``, ``"9.0"``, ``"vcfa-rest"``).
    product = "vcfa"
    version = "9.0"
    impl_id = "vcfa-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: VcfAutomationCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        # Per-target, per-plane token caches. Keyed on the tenant-unique
        # ``(tenant_id, target.id)`` tuple (``target_cache_key``, #1642/#1672)
        # so two same-named targets in different tenants never share a
        # cached token; same isolation invariant the NSX precedent established.
        self._provider_tokens: dict[tuple[str, str], str] = {}
        self._tenant_tokens: dict[tuple[str, str], str] = {}
        # One lock per plane -- provider and tenant first-uses are
        # independent and shouldn't block each other.
        self._provider_lock = asyncio.Lock()
        self._tenant_lock = asyncio.Lock()
        self._credentials_loader: VcfAutomationCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    # ------------------------------------------------------------------
    # URL composition + vhost routing
    # ------------------------------------------------------------------

    def _base_url(self, target: VcfAutomationTargetLike) -> str:
        """Delegate to :func:`._routing.compose_base_url` for vhost handling."""
        return compose_base_url(
            target_name=target.name,
            host=target.host,
            port=getattr(target, "port", None),
            fqdn=getattr(target, "fqdn", None),
        )

    # ------------------------------------------------------------------
    # auth_headers -- ABC + plane-aware path argument
    # ------------------------------------------------------------------

    async def auth_headers(
        self,
        target: VcfAutomationTargetLike,
        operator: Operator,
        *,
        path: str | None = None,
    ) -> dict[str, str]:
        """Return plane-specific headers for the request.

        ``path`` is keyword-only and required in practice: ``None`` is
        rejected because this connector is dual-plane and there is no
        plane-agnostic auth header set. The plane is selected from the
        path prefix (``/iaas/api/*`` -> tenant, anything else ->
        provider), the cached token for that plane is fetched (lazy
        login on first use), and the response is the canonical Bearer
        header plus the plane-specific ``Accept`` media type.

        The base ``HttpConnector._request_json`` / ``_post_json``
        callers don't forward ``path`` -- this connector overrides
        both transports to thread ``path`` through. A stray path-less
        call raises :exc:`VcfAutomationConfigurationError` rather than
        silently picking a plane. ``operator`` is forwarded to the
        :class:`VcfAutomationCredentialsLoader` on first session-establish
        so the default loader (:func:`.session.load_credentials_from_vault`)
        can perform the operator-context Vault read under the operator's
        identity; cached tokens are reused on warm-path calls without
        re-reading Vault. Raises :exc:`NotImplementedError` for any
        ``target.auth_model`` other than ``shared_service_account`` /
        ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcfAutomationConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        if path is None:
            raise VcfAutomationConfigurationError(
                f"vcf-automation auth_headers requires the request path to "
                f"select the auth plane (target={target.name!r}). Callers "
                "must use _request_json / _post_json which forward the path; "
                "direct auth_headers() calls must pass path= explicitly."
            )
        plane = plane_for_path(path)
        if plane == "provider":
            token = await self._provider_session_token(target, operator)
            return {
                "Authorization": f"Bearer {token}",
                "Accept": provider_accept_for_path(path),
            }
        token = await self._tenant_session_token(target, operator)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": TENANT_ACCEPT,
        }

    # ------------------------------------------------------------------
    # Per-plane session establishment
    # ------------------------------------------------------------------

    async def _provider_session_token(
        self, target: VcfAutomationTargetLike, operator: Operator
    ) -> str:
        """Return the cached provider-plane JWT, establishing on first use.

        ``operator`` is forwarded to the
        :class:`VcfAutomationCredentialsLoader` so the credential read
        runs under the operator's identity (the live default loader's
        operator-context Vault KV-v2 read). When
        ``target.provider_secret_ref`` is set, the loader is called
        with the override path so the provider account can differ
        from the SSO/tenant account (the ``admin@System`` vs
        ``svc-meho`` split documented in the consumer wrapper).
        Otherwise the default ``target.secret_ref`` pair is used for
        both planes. See :func:`._auth.vcfa_provider_login` for the
        wire-level POST.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`.
        The primary fail-closed gate against empty ``raw_jwt`` is the
        loader's ``vault_client_for_operator`` / ``load_basic_credentials``
        call chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return a cached provider JWT
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
        async with self._provider_lock:
            cached = self._provider_tokens.get(cache_key)
            if cached is not None:
                return cached
            override_ref = getattr(target, "provider_secret_ref", None)
            creds = await load_credentials_with_override(
                self._credentials_loader, target, operator, override_ref
            )
            client = await self._http_client(target)
            jwt = await vcfa_provider_login(client, creds, target)
            self._provider_tokens[cache_key] = jwt
            return jwt

    async def _tenant_session_token(
        self, target: VcfAutomationTargetLike, operator: Operator
    ) -> str:
        """Return the cached tenant-plane token, establishing on first use.

        ``operator`` is forwarded to the
        :class:`VcfAutomationCredentialsLoader` so the credential read
        runs under the operator's identity. The tenant plane does
        NOT honour ``provider_secret_ref`` -- it's strictly an SSO-ish
        account flow against ``target.secret_ref``. See
        :func:`._auth.tenant_login` for the wire-level POST.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`
        and the sibling check in :meth:`_provider_session_token`. The
        primary fail-closed gate against empty ``raw_jwt`` is the loader's
        ``vault_client_for_operator`` / ``load_basic_credentials`` call
        chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return a cached tenant
        token to an unauthenticated caller via a cache hit.
        :meth:`auth_headers` enforces only the ``auth_model`` boundary
        (rejects ``per_user`` / ``impersonation`` under
        ``shared_service_account`` scoping). Raised before the cache
        lookup so a primed token from an authenticated caller cannot
        leak to a system-initiated caller. See
        ``docs/architecture/connector-auth.md`` § "Cache scoping under
        ``shared_service_account``" for the contract.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        cache_key = target_cache_key(target)
        async with self._tenant_lock:
            cached = self._tenant_tokens.get(cache_key)
            if cached is not None:
                return cached
            creds = await load_credentials_with_override(
                self._credentials_loader, target, operator, None
            )
            client = await self._http_client(target)
            token = await tenant_login(client, creds, target)
            self._tenant_tokens[cache_key] = token
            return token

    async def _invalidate_plane(self, target: VcfAutomationTargetLike, plane: Plane) -> None:
        """Drop the cached token for *plane* on *target* under the plane's lock."""
        lock = self._provider_lock if plane == "provider" else self._tenant_lock
        cache = self._provider_tokens if plane == "provider" else self._tenant_tokens
        async with lock:
            cache.pop(target_cache_key(target), None)

    # ------------------------------------------------------------------
    # Transport overrides -- thread path into auth_headers + 401 retry-once
    # ------------------------------------------------------------------

    async def _request_json(
        self,
        target: VcfAutomationTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Path-aware idempotent JSON request with per-plane 401 retry-once.

        Diverges from :meth:`HttpConnector._request_json` in two ways:

        1. The request path drives plane selection (via
           :meth:`auth_headers`'s ``path=`` keyword); the base method
           has no path-aware hook.
        2. On HTTP 401, the relevant plane's cached token is
           invalidated and the call retries once with a freshly-minted
           token. A second 401 raises :exc:`RuntimeError` naming the
           target -- consumer wrapper posture: re-login once, not a
           retry loop.

        Connection-error / 5xx retry is intentionally not layered on
        here; the per-plane 401 dance is the only recovery the
        connector implements. The base method's ``ValueError`` on
        non-idempotent verbs is preserved.
        """
        method_upper = method.upper()
        if method_upper not in {"GET", "HEAD", "OPTIONS"}:
            raise ValueError(
                f"_request_json only accepts idempotent methods "
                f"['GET', 'HEAD', 'OPTIONS']; got {method_upper!r}"
            )
        return await self._do_request_with_retry(
            target,
            method_upper,
            path,
            operator=operator,
            params=params,
            json=json,
            extra_headers=extra_headers,
        )

    async def _post_json(
        self,
        target: VcfAutomationTargetLike,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Mapping[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Path-aware non-idempotent JSON request with per-plane 401 retry-once.

        Honours the *actual* non-idempotent verb (``POST``/``PUT``/``PATCH``/
        ``DELETE``) and an optional form-encoded ``data=`` body, mirroring the
        base :meth:`HttpConnector._post_json` contract (#1968) while keeping
        the per-plane 401 retry-once dance.
        """
        verb = verb.upper()
        if verb in {"GET", "HEAD", "OPTIONS"}:
            raise ValueError(f"_post_json only accepts non-idempotent methods; got {verb!r}")
        if json is not None and data is not None:
            raise ValueError("_post_json accepts json= or data=, not both")
        return await self._do_request_with_retry(
            target,
            verb,
            path,
            operator=operator,
            params=None,
            json=json,
            data=data,
            extra_headers=extra_headers,
        )

    async def _do_request_with_retry(
        self,
        target: VcfAutomationTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: Mapping[str, Any] | None,
        json: Mapping[str, Any] | None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Shared per-plane 401 retry-once dance for _request_json + _post_json.

        Build headers, fire the request; on 401 invalidate the relevant
        plane's token, refresh headers, retry once. A second 401
        surfaces as :exc:`RuntimeError`. ``extra_headers`` (header-located
        op params) merge onto the plane auth headers; ``data`` carries a
        form-encoded body.
        """

        async def _fire(headers: Mapping[str, str]) -> httpx.Response:
            client = await self._http_client(target)
            params_dict = dict(params) if params is not None else None
            json_dict = dict(json) if json is not None else None
            return await client.request(
                method,
                path,
                params=params_dict,
                json=json_dict,
                data=data,
                headers=dict(headers),
            )

        plane = plane_for_path(path)
        headers = await self.auth_headers(target, operator, path=path)
        if extra_headers:
            headers = {**headers, **extra_headers}
        resp = await _fire(headers)
        if resp.status_code == 401:
            await self._invalidate_plane(target, plane)
            headers = await self.auth_headers(target, operator, path=path)
            if extra_headers:
                headers = {**headers, **extra_headers}
            resp = await _fire(headers)
            if resp.status_code == 401:
                raise RuntimeError(
                    f"vcf-automation {plane} session re-login failed for "
                    f"target {target.name!r}: {method} {path} returned "
                    "HTTP 401 after refresh"
                )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"vcf-automation {method} {path} for target {target.name!r} "
                f"returned a non-object JSON payload of type {type(payload).__name__}"
            )
        return payload

    # ------------------------------------------------------------------
    # fingerprint / probe -- unauthenticated per-plane version endpoints
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: VcfAutomationTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Build the canonical fingerprint from per-plane unauthenticated probes.

        Both unauthenticated probes must succeed for ``reachable=True``
        -- a failure on either plane surfaces as ``reachable=False``
        with ``extras["failed_plane"]`` naming the offender and
        ``extras["error"]`` carrying the exception class + message.
        Vhost mis-configuration (IP host with no ``fqdn``) is caught
        at ``_http_client`` construction and reported as the
        structured failure too. See module docstring for the per-plane
        probe-endpoint rationale.

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — VCF
        Automation's per-plane version probes are unauthenticated, so
        the route operator plays no role here.
        """
        del operator  # unused — unauthenticated probes, no Vault read
        probed_at = datetime.now(UTC)
        probe_method = f"GET {PROVIDER_VERSION_PATH} + GET {TENANT_VERSION_PATH}"
        try:
            client = await self._http_client(target)
        except VcfAutomationConfigurationError as exc:
            return self._unreachable_fingerprint(probed_at, probe_method, exc, failed_plane=None)
        provider_resp = await _try_probe(client, PROVIDER_VERSION_PATH)
        if isinstance(provider_resp, Exception):
            return self._unreachable_fingerprint(
                probed_at, probe_method, provider_resp, failed_plane="provider"
            )
        tenant_resp = await _try_probe(client, TENANT_VERSION_PATH, accept=TENANT_ACCEPT)
        if isinstance(tenant_resp, Exception):
            return self._unreachable_fingerprint(
                probed_at, probe_method, tenant_resp, failed_plane="tenant"
            )
        # The tenant /iaas/api/about response is JSON; the provider
        # /api/versions response is XML. We carry the tenant version
        # field (the structured one) into the result.
        try:
            tenant_payload = tenant_resp.json()
        except ValueError:
            tenant_payload = {}
        latest_api_version = (
            tenant_payload.get("latestApiVersion") if isinstance(tenant_payload, dict) else None
        )
        supported_apis = (
            tenant_payload.get("supportedApis") if isinstance(tenant_payload, dict) else None
        )
        return FingerprintResult(
            vendor="vmware",
            product="vcfa",
            version=latest_api_version,
            reachable=True,
            probed_at=probed_at,
            probe_method=probe_method,
            extras={
                "planes": ["provider", "tenant"],
                "provider_versions_status": provider_resp.status_code,
                "tenant_latest_api_version": latest_api_version,
                "tenant_supported_apis": supported_apis,
            },
        )

    def _unreachable_fingerprint(
        self,
        probed_at: datetime,
        probe_method: str,
        exc: BaseException,
        *,
        failed_plane: str | None,
    ) -> FingerprintResult:
        """Build a non-reachable :class:`FingerprintResult` with a structured error."""
        extras: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
        if failed_plane is not None:
            extras["failed_plane"] = failed_plane
        return FingerprintResult(
            vendor="vmware",
            product="vcfa",
            reachable=False,
            probed_at=probed_at,
            probe_method=probe_method,
            extras=extras,
        )

    async def probe(self, target: VcfAutomationTargetLike) -> ProbeResult:
        """Lightweight reachability check -- delegates to :meth:`fingerprint`."""
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error", "unreachable")),
            probed_at=fp.probed_at,
        )

    # ------------------------------------------------------------------
    # G0.6 dispatch shim
    # ------------------------------------------------------------------

    async def execute(
        self,
        target: VcfAutomationTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Same shape as :meth:`NsxConnector.execute` /
        :meth:`SddcManagerConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI
        verbs once #840 lands) construct a real :class:`Operator` and
        invoke ``dispatch`` themselves.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vcfa-rest-connector-shim",
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

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Clear both plane token caches and tear down the httpx pool.

        No DELETE-revoke is issued against either plane -- VCFA's
        session has an idle timeout, and a per-target network call
        during lifespan shutdown is more risk than benefit (same
        posture the NSX precedent established).
        """
        async with self._provider_lock:
            self._provider_tokens.clear()
        async with self._tenant_lock:
            self._tenant_tokens.clear()
        await super().aclose()


async def _try_probe(
    client: httpx.AsyncClient,
    path: str,
    accept: str | None = None,
) -> httpx.Response | Exception:
    """GET *path* on *client*, returning the response or the captured exception.

    Used by :meth:`VcfAutomationConnector.fingerprint` to probe each
    plane and produce a structured ``reachable=False`` result on
    failure rather than letting the exception bubble.
    """
    try:
        headers = {"Accept": accept} if accept else None
        resp = await client.get(path, headers=headers)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        return exc
    return resp
