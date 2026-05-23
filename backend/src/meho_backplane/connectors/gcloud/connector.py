# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GcloudConnector — HttpConnector subclass for GCP REST APIs.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in G3.7-T5 (#848) via hand-registered typed ops against
GCP REST surfaces (cloudresourcemanager, compute, iam, serviceusage).

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.gcloud.__init__`.

Auth
----

The connector uses GCP **Application Default Credentials + Service Account
Impersonation** per decision #12 (transport = B). There are two layers:

1. **Source credentials**: ``google.auth.default()`` yields the operator's
   ambient ADC (Workload Identity, gcloud CLI credentials, or a
   ``GOOGLE_APPLICATION_CREDENTIALS`` env-var path to a non-key-file
   credential — e.g. an ``external_account`` credential file for Workload
   Identity Federation).

2. **Impersonated credentials**: ``google.auth.impersonated_credentials.Credentials``
   wraps the source, targeting ``target.gcp_impersonate_sa``, with
   ``https://www.googleapis.com/auth/cloud-platform`` scope and
   ``lifetime=3600`` (the GCP maximum for impersonated tokens).

The bearer token is cached per ``target.name``. On a 401 response,
:meth:`_refresh_token` re-calls ``creds.refresh()`` to get a new token and
retries the original request once.

SA-JSON-key refusal
-------------------

Org policy ``constraints/iam.disableServiceAccountKeyCreation`` is in force
on the consumer's GCP organization. :meth:`auth_headers` inspects the Vault
``secret_ref`` payload for SA-JSON-key field names (``private_key``,
``private_key_id``, ``client_email``, etc.) and raises a :exc:`ValueError`
with a clear message before building any token. The error names the target
and the offending fields so operators can identify and remove the misdirected
key material.

Target conventions
------------------

``target.host`` is unused — the connector reaches GCP via the well-known
public API hostnames (``cloudresourcemanager.googleapis.com``,
``compute.googleapis.com``, etc.). ``target.gcp_project`` and
``target.gcp_impersonate_sa`` drive the auth and fingerprint flows.

Fingerprint
-----------

``GET https://cloudresourcemanager.googleapis.com/v1/projects/<gcp_project>``
returns the project resource. The fingerprint shape is:
- ``vendor="google"``, ``product="gcp-project"``
- ``version=None`` (GCP projects have no connector-visible "version")
- ``extras["project_number"]``, ``extras["lifecycle_state"]``,
  ``extras["organization"]`` (resolved from ``parent`` if present).

Probe
-----

``probe()`` exercises the same endpoint as ``fingerprint()`` but focuses on
verifying the impersonation flow end-to-end rather than the fingerprint shape.
``ok=True`` when the endpoint returns HTTP 200 with a valid JSON body;
``ok=False`` + ``reason`` on any transport, auth, or status failure.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim :meth:`execute`
exists for ABC compatibility but operations land in G3.7-T5 (#848) via
``register_typed_operation()``. Until then, the connector is registered and
discoverable but ``execute(target, op_id, ...)`` resolves to
"unknown operation" at the dispatcher layer — the correct behaviour for a
registered-but-empty skeleton.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.gcloud.session import (
    GcloudCredentialsLoader,
    GcloudTargetLike,
    _contains_sa_key_fields,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["GcloudConnector"]

_log = structlog.get_logger(__name__)

_GCP_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_CRM_API_BASE = "https://cloudresourcemanager.googleapis.com"
_IMPERSONATION_LIFETIME = 3600  # seconds — GCP maximum for impersonated tokens


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is IMPERSONATION mode or the pre-G0.3 None sentinel.

    Accepts the enum member, its string value, and ``None``.
    """
    if value is None:
        return True
    if value is AuthModel.IMPERSONATION:
        return True
    return bool(value == AuthModel.IMPERSONATION.value)


class GcloudConnector(HttpConnector):
    """GCP REST connector with ADC + service-account impersonation.

    Auth: ``google.auth.default()`` for ADC source credentials, wrapped
    by ``google.auth.impersonated_credentials.Credentials`` targeting the
    SA in ``target.gcp_impersonate_sa``. Bearer token is cached per
    ``target.name``; auto-refreshed on 401 via ``_ensure_token(target)``.

    SA-JSON-key material in the Vault ``secret_ref`` payload is refused
    before any token is built. The refusal is unconditional — org policy
    ``constraints/iam.disableServiceAccountKeyCreation`` applies at the
    GCP organization level and the connector encodes the same constraint.

    ``target.host`` is unused — all GCP calls use well-known public
    hostnames. ``_base_url()`` returns a placeholder; real calls use
    absolute URLs via ``_get_json_abs()``.
    """

    product = "gcloud"
    version = "1.0"
    impl_id = "gcloud-rest"
    supported_version_range: str | None = None
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: GcloudCredentialsLoader | None = None,
        adc_loader: Any | None = None,
    ) -> None:
        """Construct a GcloudConnector.

        Parameters
        ----------
        credentials_loader:
            Async callable resolving a target to its Vault secret dict. Used
            for the SA-JSON-key-refusal gate. Defaults to the stub that raises
            :exc:`NotImplementedError` until G0.3/Goal #214 wires the live
            Vault read.
        adc_loader:
            Synchronous callable returning ``(source_credentials, project)``
            — the same shape as ``google.auth.default()``. Injected by unit
            tests to avoid touching the real ADC chain. Defaults to
            ``google.auth.default`` when ``None``.
        """
        super().__init__()
        self._credentials_loader: GcloudCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._adc_loader = adc_loader
        # Per-target cache: token string + impersonated Credentials object.
        # Per-target locks allow concurrent fetch/refresh for different targets
        # without serialising across all targets (M1 fix).
        self._token_cache: dict[str, str] = {}
        self._creds_cache: dict[str, Any] = {}  # google.auth.impersonated_credentials.Credentials
        self._token_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # HttpConnector overrides
    # ------------------------------------------------------------------

    def _base_url(self, target: GcloudTargetLike) -> str:
        """Placeholder base URL — GCP calls use absolute URIs.

        The httpx client is keyed by ``target.name``; when operations call
        GCP APIs they pass absolute URLs to :meth:`_get_json_abs`. The
        base_url set here is a placeholder so ``HttpConnector._http_client``
        can create the per-target client without a real host.
        """
        return f"https://{target.gcp_project}.gcp.invalid"

    async def auth_headers(
        self,
        target: GcloudTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}`` for *target*.

        Enforces three things before building a token:

        1. **Auth model gate**: ``target.auth_model`` must be
           ``IMPERSONATION`` or ``None`` (pre-G0.3 sentinel). Any other
           value raises :exc:`NotImplementedError`.

        2. **SA-JSON-key refusal**: loads the Vault ``secret_ref`` payload
           and checks for SA-JSON-key field names. If any are found, raises
           :exc:`ValueError` naming the target and the offending fields.
           No token is built.

        3. **Token fetch / cache**: calls :meth:`_ensure_token` which
           builds impersonated credentials via ADC + impersonation if the
           cache is empty, then returns the bearer token.

        The ``operator`` is forwarded to :meth:`_gate_sa_key_refusal` so the
        credentials loader reads the per-target Vault secret under the
        operator's identity. It is NOT forwarded to GCP — ``IMPERSONATION``
        auth drives all GCP calls through the google-auth impersonation
        chain, not the operator's OIDC JWT.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"GcloudConnector only supports auth_model="
                f"{AuthModel.IMPERSONATION.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        await self._gate_sa_key_refusal(target, operator)
        token = await self._ensure_token(target)
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # SA-JSON-key refusal
    # ------------------------------------------------------------------

    async def _gate_sa_key_refusal(self, target: GcloudTargetLike, operator: Operator) -> None:
        """Raise :exc:`ValueError` if the Vault secret carries SA-JSON-key fields.

        The check runs on every :meth:`auth_headers` call (not just the
        first) so a Vault secret rotation that introduces key material is
        caught on the next request rather than silently ignored due to
        caching. The credentials_loader is responsible for cache behaviour;
        this method only validates the shape. ``operator`` is forwarded to the
        loader so the per-target Vault read happens under the operator's
        identity.

        Raises
        ------
        ValueError
            When any SA-JSON-key field name is present in the loaded record,
            naming the target and the offending field names.
        """
        record = await self._credentials_loader(target, operator)
        if _contains_sa_key_fields(record):
            from meho_backplane.connectors.gcloud.session import _SA_KEY_FIELDS

            offending_fields = sorted(_SA_KEY_FIELDS & set(record.keys()))
            raise ValueError(
                f"GcloudConnector refuses SA-JSON-key material for target "
                f"{target.name!r}: secret_ref={target.secret_ref!r} contains "
                f"SA key fields {offending_fields!r}. "
                "Org policy constraints/iam.disableServiceAccountKeyCreation "
                "forbids SA JSON keys. Use ADC + impersonation instead. "
                "Remove SA key fields from the Vault secret and configure "
                "gcp_impersonate_sa on the target row."
            )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self, target: GcloudTargetLike) -> str:
        """Return a valid bearer token for *target*, fetching if needed.

        Builds impersonated credentials on first use via
        ``google.auth.default()`` + ``google.auth.impersonated_credentials.Credentials``.
        Caches the token string and the Credentials object.

        A per-target lock (keyed by ``target.name``) serialises concurrent
        first-use callers for the same target without blocking callers for
        different targets (M1 fix). Subsequent calls take the fast path
        (cached token still valid) without acquiring any lock.
        """
        # Fast path: cached and valid
        cached_token = self._token_cache.get(target.name)
        if cached_token is not None:
            creds = self._creds_cache.get(target.name)
            if creds is not None and getattr(creds, "valid", False):
                return cached_token

        lock = self._token_locks.setdefault(target.name, asyncio.Lock())
        async with lock:
            # Re-check under lock in case another coroutine fetched first.
            cached_token = self._token_cache.get(target.name)
            if cached_token is not None:
                creds = self._creds_cache.get(target.name)
                if creds is not None and getattr(creds, "valid", False):
                    return cached_token

            return await self._fetch_token(target)

    async def _fetch_token(self, target: GcloudTargetLike) -> str:
        """Build impersonated credentials and fetch the initial bearer token.

        Runs in a thread pool executor because ``google.auth.default()`` and
        ``Credentials.refresh()`` call synchronous ``requests``-based HTTP
        under the hood (``google.auth.transport.requests.Request``).
        """
        loop = asyncio.get_running_loop()
        token, creds = await loop.run_in_executor(None, self._fetch_token_sync, target)
        self._token_cache[target.name] = token
        self._creds_cache[target.name] = creds
        _log.info(
            "gcloud_token_fetched",
            target=target.name,
            sa=target.gcp_impersonate_sa,
        )
        return token

    def _fetch_token_sync(self, target: GcloudTargetLike) -> tuple[str, Any]:
        """Synchronous token fetch — runs in a thread pool executor.

        Calls ``google.auth.default()`` to obtain ADC source credentials,
        wraps them with ``google.auth.impersonated_credentials.Credentials``
        targeting ``target.gcp_impersonate_sa``, then calls
        ``creds.refresh(Request())`` to materialise the bearer token.

        Returns ``(token_string, creds_object)``.
        """
        import google.auth
        import google.auth.impersonated_credentials
        import google.auth.transport.requests

        adc_loader = self._adc_loader if self._adc_loader is not None else google.auth.default
        source_credentials, _project = adc_loader(scopes=[_GCP_CLOUD_PLATFORM_SCOPE])

        creds = google.auth.impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
            source_credentials=source_credentials,
            target_principal=target.gcp_impersonate_sa,
            target_scopes=[_GCP_CLOUD_PLATFORM_SCOPE],
            lifetime=_IMPERSONATION_LIFETIME,
        )

        request = google.auth.transport.requests.Request()
        creds.refresh(request)  # type: ignore[no-untyped-call]

        token: str = creds.token  # type: ignore[assignment]
        return token, creds

    async def refresh_token(self, target: GcloudTargetLike) -> str:
        """Force-refresh the bearer token for *target* and return the new token.

        Called by :meth:`_get_json_abs` on a 401 response. Acquires the
        per-target token lock, calls ``creds.refresh()`` in the executor,
        updates the cache, and returns the new token.
        """
        loop = asyncio.get_running_loop()
        lock = self._token_locks.setdefault(target.name, asyncio.Lock())
        async with lock:
            creds = self._creds_cache.get(target.name)
            if creds is None:
                return await self._fetch_token(target)

            def _refresh_sync() -> str:
                import google.auth.transport.requests

                request = google.auth.transport.requests.Request()
                creds.refresh(request)
                return str(creds.token)

            new_token = await loop.run_in_executor(None, _refresh_sync)
            self._token_cache[target.name] = new_token
            _log.info(
                "gcloud_token_refreshed",
                target=target.name,
                sa=target.gcp_impersonate_sa,
            )
            return new_token

    # ------------------------------------------------------------------
    # GCP-specific HTTP helper (absolute URLs)
    # ------------------------------------------------------------------

    async def _get_json_abs(
        self,
        target: GcloudTargetLike,
        abs_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET an absolute GCP API URL, handle 401 with one token refresh.

        GCP REST calls use absolute URIs (e.g.
        ``https://cloudresourcemanager.googleapis.com/v1/projects/<id>``).
        The ``httpx.AsyncClient`` base_url is a placeholder; ``abs_url``
        overrides it fully.

        On a 401 response, the token is refreshed once and the request
        retried. Any subsequent non-2xx raises ``httpx.HTTPStatusError``.
        """
        token = await self._ensure_token(target)
        client = await self._http_client(target)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get(abs_url, params=params, headers=headers)

        if resp.status_code == 401:
            _log.info("gcloud_401_token_refresh", target=target.name, url=abs_url)
            new_token = await self.refresh_token(target)
            headers = {"Authorization": f"Bearer {new_token}"}
            resp = await client.get(abs_url, params=params, headers=headers)

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # ABC methods
    # ------------------------------------------------------------------

    async def fingerprint(self, target: GcloudTargetLike) -> FingerprintResult:
        """Canonical fingerprint via Cloud Resource Manager ``projects.get``.

        Calls ``GET https://cloudresourcemanager.googleapis.com/v1/projects/<gcp_project>``.
        The response's ``projectNumber``, ``lifecycleState``, and ``parent``
        fields populate ``extras``. ``vendor="google"``,
        ``product="gcp-project"`` — this fingerprints the project resource
        itself, not the GCP API surface.

        On any transport, auth, or status failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message.
        """
        probed_at = datetime.now(UTC)
        url = f"{_CRM_API_BASE}/v1/projects/{target.gcp_project}"
        try:
            payload = await self._get_json_abs(target, url)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return FingerprintResult(
                vendor="google",
                product="gcp-project",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET cloudresourcemanager.googleapis.com/v1/projects",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        organization: str | None = None
        parent = payload.get("parent") or {}
        if parent.get("type") == "organization":
            organization = parent.get("id")

        return FingerprintResult(
            vendor="google",
            product="gcp-project",
            reachable=True,
            probed_at=probed_at,
            probe_method="GET cloudresourcemanager.googleapis.com/v1/projects",
            extras={
                "project_number": payload.get("projectNumber"),
                "lifecycle_state": payload.get("lifecycleState"),
                "organization": organization,
                "project_id": payload.get("projectId"),
            },
        )

    async def probe(self, target: GcloudTargetLike) -> ProbeResult:
        """Reachability check that exercises the full impersonation flow.

        Calls the same Cloud Resource Manager endpoint as
        :meth:`fingerprint` so the probe validates that:

        1. ADC source credentials are present and valid.
        2. The impersonation chain succeeds (SA email is correct; Token
           Creator role is granted).
        3. The Cloud Resource Manager API is reachable.
        4. The project exists and the impersonated SA has at minimum
           ``resourcemanager.projects.get`` permission.

        Returns ``ok=True`` when the response is HTTP 200 with a
        ``projectId`` field matching ``target.gcp_project``.
        Returns ``ok=False`` + ``reason`` on any failure.
        """
        probed_at = datetime.now(UTC)
        url = f"{_CRM_API_BASE}/v1/projects/{target.gcp_project}"
        try:
            payload = await self._get_json_abs(target, url)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                probed_at=probed_at,
            )

        if payload.get("projectId") != target.gcp_project:
            return ProbeResult(
                ok=False,
                reason=(
                    f"project_id mismatch: expected {target.gcp_project!r}, "
                    f"got {payload.get('projectId')!r}"
                ),
                probed_at=probed_at,
            )

        return ProbeResult(ok=True, probed_at=probed_at)

    async def execute(
        self,
        target: GcloudTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Operations ship in G3.7-T5 (#848) via ``register_typed_operation()``.
        Post-G0.6 callers (``/api/v1/operations/call``, MCP ``call_operation``)
        use the dispatcher directly; they don't reach this method.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:gcloud-rest-connector-shim",
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
        """Clear cached tokens and credentials, then tear down the httpx pool.

        No server-side session to revoke — impersonated tokens expire
        server-side after ``lifetime`` seconds. The token cache is cleared
        so a post-aclose reuse of the same connector instance starts clean.
        """
        self._token_cache.clear()
        self._creds_cache.clear()
        self._token_locks.clear()
        await super().aclose()
