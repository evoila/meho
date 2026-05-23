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


#: Curated ``when_to_use`` blurbs keyed by ``group_key``.
#: Each entry answers *"when should the agent search this group?"* — the
#: pairing signal for ``list_operation_groups`` (G0.9-T4b #732). Mirrors
#: the ``_WHEN_TO_USE_BY_GROUP`` pattern in Bind9Connector / KubernetesConnector.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for project-identity questions before any resource drill-in: "
        "'which GCP project is this target?' or 'is the project active and "
        "reachable?'. The ``gcloud.about`` op is the fast first call; "
        "``gcloud.project.describe`` returns the full CRM resource. Use this "
        "group first when the agent needs the project_number for a downstream "
        "API path, the lifecycle_state to gate a write op, or the organization "
        "ID to scope a billing or org-policy query."
    ),
    "project": (
        "Use when the operator needs the full Cloud Resource Manager project "
        "resource — not just identity fields but also custom labels, creation "
        "timestamp, and exact parent type. Prefer 'identity' group for quick "
        "reachability checks; reach for this group when the full structured "
        "resource is required for downstream logic."
    ),
    "services": (
        "Use to audit which GCP APIs are enabled on the project. The right group "
        "when diagnosing a 'method not found' or 403 on a GCP API call ('is the "
        "compute API enabled?'), before deploying resources that require a specific "
        "API, or for a compliance audit of enabled services. "
        "``gcloud.services.list`` defaults to enabled-only; pass "
        "``enabled_only=false`` to include disabled services."
    ),
    "iam": (
        "Use for IAM questions: which service accounts exist on the project? "
        "Which principals have which roles? ``gcloud.iam.service_accounts.list`` "
        "enumerates SAs; ``gcloud.iam.policy.read`` returns the full project-level "
        "policy with all role bindings. The right group when the agent needs to "
        "verify a permission, pick an impersonation SA, or produce an access-review "
        "report. Pair with the 'identity' group when onboarding a new target."
    ),
    "compute": (
        "Use for Compute Engine resource inventory: VMs, VPC networks, and subnets. "
        "``gcloud.compute.instances.list`` enumerates VMs project-wide (or per-zone); "
        "``gcloud.compute.networks.list`` maps the VPC topology; "
        "``gcloud.compute.subnetworks.list`` enumerates subnets per-region or "
        "project-wide. The right group when the operator asks about running VMs, "
        "network address space, or wants to pick a zone/region before deploying a "
        "resource. Large instance lists return a JSONFlux-compatible envelope."
    ),
}


def _instance_row(inst: dict[str, Any], *, zone: str) -> dict[str, Any]:
    """Extract a compact row dict from a Compute Engine instance resource."""
    internal_ips: list[str] = []
    external_ips: list[str] = []
    for iface in inst.get("networkInterfaces") or []:
        ip = iface.get("networkIP")
        if ip:
            internal_ips.append(ip)
        for access_config in iface.get("accessConfigs") or []:
            nat_ip = access_config.get("natIP")
            if nat_ip:
                external_ips.append(nat_ip)
    machine_type = inst.get("machineType", "")
    if "/" in machine_type:
        machine_type = machine_type.split("/")[-1]
    return {
        "zone": zone,
        "name": inst.get("name"),
        "machine_type": machine_type or None,
        "status": inst.get("status"),
        "internal_ips": internal_ips,
        "external_ips": external_ips,
        "creation_timestamp": inst.get("creationTimestamp"),
    }


def _subnet_row(sn: dict[str, Any], *, region: str) -> dict[str, Any]:
    """Extract a compact row dict from a Compute Engine subnetwork resource."""
    return {
        "region": region,
        "name": sn.get("name", ""),
        "cidr_range": sn.get("ipCidrRange"),
        "network": sn.get("network"),
        "purpose": sn.get("purpose"),
        "private_ip_google_access": sn.get("privateIpGoogleAccess"),
        "creation_timestamp": sn.get("creationTimestamp"),
    }


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

    # ------------------------------------------------------------------
    # GCP-specific HTTP helper — POST with absolute URL
    # ------------------------------------------------------------------

    async def _post_json_abs(
        self,
        target: GcloudTargetLike,
        abs_url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST to an absolute GCP API URL, handle 401 with one token refresh.

        Used by ops that call GCP APIs with POST semantics (e.g.
        ``cloudresourcemanager.googleapis.com/v1/projects/<id>:getIamPolicy``).
        On a 401 response, the token is refreshed once and the request retried.
        Any subsequent non-2xx raises ``httpx.HTTPStatusError``.
        """
        token = await self._ensure_token(target)
        client = await self._http_client(target)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.post(abs_url, json=json_body or {}, headers=headers)

        if resp.status_code == 401:
            _log.info("gcloud_401_token_refresh_post", target=target.name, url=abs_url)
            new_token = await self.refresh_token(target)
            headers = {"Authorization": f"Bearer {new_token}"}
            resp = await client.post(abs_url, json=json_body or {}, headers=headers)

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Typed-op registration (G3.7-T5 #848)
    # ------------------------------------------------------------------

    @classmethod
    async def register_gcloud_typed_operations(cls) -> None:
        """Register all G3.7-T5 gcloud typed ops into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks :data:`GCLOUD_OPS`
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`,
        which derives ``handler_ref`` from the bound method's
        ``__module__`` + ``__qualname__``, upserts the ``endpoint_descriptor``
        row, and skips the embedding compute when the summary/description/tags
        are unchanged.

        Idempotent across pod restarts — mirrors the
        :meth:`Bind9Connector.register_operations` shape.
        """
        from meho_backplane.connectors.gcloud.ops import GCLOUD_OPS
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in GCLOUD_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"GcloudConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = _WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"GcloudConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry "
                        f"to _WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.gcloud.connector."
                    )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "gcloud_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    # ------------------------------------------------------------------
    # Op handlers (G3.7-T5 #848)
    # ------------------------------------------------------------------

    async def gcloud_about(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return GCP project identity summary.

        Op-id: ``gcloud.about``. Wraps :meth:`fingerprint` to expose the same
        project-identity data through the typed-op dispatcher as a flat dict.
        """
        del params
        result = await self.fingerprint(target)
        return {
            "project_id": result.extras.get("project_id"),
            "project_number": result.extras.get("project_number"),
            "lifecycle_state": result.extras.get("lifecycle_state"),
            "organization": result.extras.get("organization"),
        }

    async def gcloud_project_describe(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the full CRM project resource.

        Op-id: ``gcloud.project.describe``. Returns the raw dict from
        ``GET cloudresourcemanager.googleapis.com/v1/projects/<id>``.
        """
        del params
        url = f"{_CRM_API_BASE}/v1/projects/{target.gcp_project}"
        return await self._get_json_abs(target, url)

    async def gcloud_services_list(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List GCP services (APIs) on the project.

        Op-id: ``gcloud.services.list``. Follows ``nextPageToken`` pagination.
        ``params.enabled_only`` defaults to ``True`` — applies
        ``filter=state:ENABLED`` to the Service Usage API request.
        """
        enabled_only: bool = params.get("enabled_only", True)
        query_params: dict[str, Any] = {}
        if enabled_only:
            query_params["filter"] = "state:ENABLED"

        base_url = f"https://serviceusage.googleapis.com/v1/projects/{target.gcp_project}/services"
        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            if page_token:
                query_params["pageToken"] = page_token
            payload = await self._get_json_abs(target, base_url, params=query_params)
            for svc in payload.get("services") or []:
                config = svc.get("config") or {}
                rows.append(
                    {
                        "name": svc.get("name", "").split("/")[-1],
                        "title": config.get("title"),
                        "state": svc.get("state", ""),
                    }
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return {"rows": rows, "total": len(rows)}

    async def gcloud_iam_service_accounts_list(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List IAM service accounts in the project.

        Op-id: ``gcloud.iam.service_accounts.list``. Follows ``nextPageToken``
        pagination against the IAM v1 serviceAccounts.list API.
        """
        del params
        base_url = f"https://iam.googleapis.com/v1/projects/{target.gcp_project}/serviceAccounts"
        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            query_params: dict[str, Any] = {}
            if page_token:
                query_params["pageToken"] = page_token
            payload = await self._get_json_abs(target, base_url, params=query_params or None)
            for sa in payload.get("accounts") or []:
                rows.append(
                    {
                        "email": sa.get("email", ""),
                        "unique_id": sa.get("uniqueId"),
                        "display_name": sa.get("displayName"),
                        "description": sa.get("description"),
                        "disabled": bool(sa.get("disabled", False)),
                    }
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return {"rows": rows, "total": len(rows)}

    async def gcloud_compute_instances_list(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List Compute Engine instances (project-wide or per-zone).

        Op-id: ``gcloud.compute.instances.list``. Uses ``aggregatedList``
        when no zone is specified, or the per-zone list API when
        ``params.zone`` is set. Follows ``nextPageToken`` pagination.
        The response envelope (``rows`` + ``total``) is compatible with
        the JSONFlux reducer.
        """
        zone: str | None = params.get("zone")
        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        if zone:
            base_url = (
                f"https://compute.googleapis.com/compute/v1/projects/"
                f"{target.gcp_project}/zones/{zone}/instances"
            )
            while True:
                query_params: dict[str, Any] = {}
                if page_token:
                    query_params["pageToken"] = page_token
                payload = await self._get_json_abs(target, base_url, params=query_params or None)
                for inst in payload.get("items") or []:
                    rows.append(_instance_row(inst, zone=zone))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
        else:
            base_url = (
                f"https://compute.googleapis.com/compute/v1/projects/"
                f"{target.gcp_project}/aggregated/instances"
            )
            while True:
                query_params = {}
                if page_token:
                    query_params["pageToken"] = page_token
                payload = await self._get_json_abs(target, base_url, params=query_params or None)
                for zone_key, zone_data in (payload.get("items") or {}).items():
                    zone_name = zone_key.removeprefix("zones/")
                    for inst in zone_data.get("instances") or []:
                        rows.append(_instance_row(inst, zone=zone_name))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break

        return {"rows": rows, "total": len(rows)}

    async def gcloud_compute_networks_list(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List VPC networks in the project.

        Op-id: ``gcloud.compute.networks.list``. Follows ``nextPageToken``
        pagination against the Compute Engine global networks API.
        """
        del params
        base_url = (
            f"https://compute.googleapis.com/compute/v1/projects/"
            f"{target.gcp_project}/global/networks"
        )
        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            query_params: dict[str, Any] = {}
            if page_token:
                query_params["pageToken"] = page_token
            payload = await self._get_json_abs(target, base_url, params=query_params or None)
            for net in payload.get("items") or []:
                routing_config = net.get("routingConfig") or {}
                rows.append(
                    {
                        "name": net.get("name", ""),
                        "auto_create_subnetworks": net.get("autoCreateSubnetworks"),
                        "routing_mode": routing_config.get("routingMode"),
                        "mtu": net.get("mtu"),
                        "creation_timestamp": net.get("creationTimestamp"),
                    }
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return {"rows": rows, "total": len(rows)}

    async def gcloud_compute_subnetworks_list(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List VPC subnets (project-wide or per-region).

        Op-id: ``gcloud.compute.subnetworks.list``. Uses ``aggregatedList``
        when no region is specified, or the per-region API when
        ``params.region`` is set. Follows ``nextPageToken`` pagination.
        """
        region: str | None = params.get("region")
        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        if region:
            base_url = (
                f"https://compute.googleapis.com/compute/v1/projects/"
                f"{target.gcp_project}/regions/{region}/subnetworks"
            )
            while True:
                query_params: dict[str, Any] = {}
                if page_token:
                    query_params["pageToken"] = page_token
                payload = await self._get_json_abs(target, base_url, params=query_params or None)
                for sn in payload.get("items") or []:
                    rows.append(_subnet_row(sn, region=region))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
        else:
            base_url = (
                f"https://compute.googleapis.com/compute/v1/projects/"
                f"{target.gcp_project}/aggregated/subnetworks"
            )
            while True:
                query_params = {}
                if page_token:
                    query_params["pageToken"] = page_token
                payload = await self._get_json_abs(target, base_url, params=query_params or None)
                for region_key, region_data in (payload.get("items") or {}).items():
                    region_name = region_key.removeprefix("regions/")
                    for sn in region_data.get("subnetworks") or []:
                        rows.append(_subnet_row(sn, region=region_name))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break

        return {"rows": rows, "total": len(rows)}

    async def gcloud_iam_policy_read(
        self,
        target: GcloudTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Read the project-level IAM policy.

        Op-id: ``gcloud.iam.policy.read``. Calls
        ``POST cloudresourcemanager.googleapis.com/v1/projects/<id>:getIamPolicy``
        and returns the full policy (version, etag, bindings).
        """
        del params
        url = f"{_CRM_API_BASE}/v1/projects/{target.gcp_project}:getIamPolicy"
        payload = await self._post_json_abs(target, url)
        return {
            "version": payload.get("version"),
            "etag": payload.get("etag"),
            "bindings": [
                {
                    "role": b.get("role", ""),
                    "members": b.get("members") or [],
                    "condition": b.get("condition"),
                }
                for b in (payload.get("bindings") or [])
            ],
        }

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
