# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcloudDirectorConnector — hand-rolled HttpConnector subclass for VMware Cloud Director.

The **net-new typed** connector for the legacy standalone **VMware Cloud
Director** (vCD) product — the migration-source estate evoila brings customers
*off* (initiative `evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_,
task #3057). vCD is a *distinct product* from the Broadcom-era successor: VCF
Automation (:mod:`~meho_backplane.connectors.vcf_automation`, ``--plane
provider``) absorbed only vCD's *provider control plane*, so this is **not** a
``vcfa`` dual-impl — it is its own ``product="vcd"`` connector, resolved by
fingerprint like any other.

Why typed (not generic / ingested)
----------------------------------

Broadcom publishes no committable OpenAPI for vCD's full REST surface — the
classic ``/api/*`` query service is bundled into the UI JS at build time, and
the appliance serves no ``/cloudapi/1.0.0/openapi*`` (probed: 404). This is the
CLAUDE.md postulate-1 "no usable spec → typed" case, the same posture the
``fleet-lcm`` (#3047) and ``vcf-automation`` provider-plane cores ship with. The
spec-reconcile lane is therefore a **manifest pin + evidenced exclusion** (see
``docs/decisions/spec-reconcile-guards-standard.md``), not a served-op-id
compare.

Auth — provider session mint
----------------------------

vCD's provider login is the vCloud-Director-derived Basic→Bearer flow (the same
surface the ``vcf-automation`` provider plane speaks): ``POST
/cloudapi/1.0.0/sessions/provider`` with HTTP Basic (``<user>@System``) returns
the JWT in the ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` **response header**; every
subsequent read sends ``Authorization: Bearer <jwt>``. The token is minted lazily
on first use, cached per tenant-unique ``(tenant_id, target.id)`` key
(:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`) under a
lock, and re-minted on a 401 via the dispatcher's credential-recovery arm
(:meth:`invalidate_credentials`, #2396) — the ``fleet-lcm`` pattern, so no
in-connector retry loop is needed and the base transport's tenacity retry (5xx /
connection only) stays intact.

The one provider token authenticates **both** REST surfaces vCD exposes; they
differ only in the ``Accept`` media type, which :meth:`_vcd_get` derives from
the path (:func:`._paths.accept_for_path`): the classic ``/api/*`` query service
takes the versioned ``application/*+json`` form, the modern ``/cloudapi/*``
collections ``application/json``.

The ``version=`` token in ``Accept`` defaults to
:data:`._paths._DEFAULT_VCD_API_VERSION` (38.0 — served by vCD 10.5/10.6),
overridable per target via ``extras["vcd_api_version"]``. **Version negotiation**
(read the max non-deprecated version from ``GET /api/versions`` and use it, the
way ``go-vcloud-director`` does) is the documented robustness follow-up.

Fingerprint / probe
-------------------

``GET /api/versions`` is vCD's unauthenticated supported-versions endpoint — the
same reachability surface the ``vcf-automation`` provider probe reads. The
connector fingerprints against it **without minting a session** (pure
reachability, works even when credentials are unavailable). ``version`` is left
``None``: ``/api/versions`` reports *API* versions (37/38/39), not the *product*
version (10.x), and the connector does not fabricate one — product-version
fingerprinting via an authenticated call is a follow-up. Being single-impl, vCD
resolves through its ``("vcd", "", "")`` wildcard registration regardless, so an
absent product version never blocks dispatch.

Operations
----------

A curated **7-op typed read core** ships here — the migration-inventory surface,
provider/System-scoped so one session sees the whole estate:

* ``vcd.org.list`` — ``GET /cloudapi/1.0.0/orgs`` (organizations, modern cloudapi).
* ``vcd.vdc.list`` / ``.vapp.list`` / ``.vm.list`` / ``.catalog.list`` — the
  classic query service (``GET /api/query?type=adminOrgVdc|adminVApp|adminVM|
  adminCatalog&format=records``), the ``go-vcloud-director`` admin query types.
* ``vcd.edge-gateway.list`` — ``GET /api/query?type=edgeGateway``.
* ``vcd.task.list`` — ``GET /api/query?type=task``.

All read-only, ``safety_level=safe``, ungated; registered as
``source_kind="typed"`` (:mod:`.typed_ops` / :mod:`.typed_reads`, per-op
``llm_instructions`` in :mod:`._llm_instructions`) so they dispatch on a fresh
boot with **zero catalog ingest**. Set-shaped results are reduced to a JSONFlux
handle by the dispatcher. By-id detail reads, non-``System`` provider identities,
and any write surface are deliberately out of scope for this read core.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import is_acceptable_auth_model
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcd._paths import (
    _DEFAULT_VCD_API_VERSION,
    _VERSIONS_PATH,
    accept_for_path,
)
from meho_backplane.connectors.vcd.session import (
    VcloudDirectorCredentialsLoader,
    VcloudDirectorTargetLike,
    load_credentials_from_vault,
)

__all__ = ["VcloudDirectorConnector"]

_log = structlog.get_logger(__name__)

# Reachability probe: vCD's unauthenticated supported-versions endpoint. The
# reconcile lane (test_connectors_vcd_spec_reconcile.py) introspects this
# constant and pins GET:/api/versions in the hand-coded manifest.
_VCD_PROBE_METHOD = "GET /api/versions (VMware Cloud Director supported-versions endpoint)"


class VcloudDirectorConnector(HttpConnector):
    """VMware Cloud Director REST connector — provider Basic→Bearer session mint.

    Per-target minted JWTs cached in :attr:`_session_tokens` (keyed on the
    tenant-unique ``(tenant_id, target.id)`` tuple) under :attr:`_session_lock`,
    so two same-named targets in different tenants never share a token
    (evoila/meho#1642/#1672). :meth:`auth_headers` returns
    ``Authorization: Bearer <jwt>``; the per-surface ``Accept`` is added by
    :meth:`_vcd_get` from the request path.

    :attr:`priority` is ``1`` — the shim-tie-break convention every hand-rolled
    connector uses, so a stray ``GenericRestConnector`` auto-shim that somehow
    registered for the same triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple matches
    # the dispatcher's parse_connector_id contract: ``"vcd-rest-10.6"`` ->
    # (``"vcd"``, ``"10.6"``, ``"vcd-rest"``). ``product`` MUST be the first
    # hyphen-segment of ``impl_id`` (the round-trip invariant register_connector_v2
    # enforces), which is why the token is ``"vcd"`` and not ``"vcloud-director"``.
    product = "vcd"
    version = "10.6"
    impl_id = "vcd-rest"
    supported_version_range = ">=10.0,<11.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: VcloudDirectorCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._credentials_loader: VcloudDirectorCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        # Per-target minted-JWT cache, keyed on ``target_cache_key`` so the
        # tenant-isolation invariant holds; a single lock serialises concurrent
        # first-use callers so they don't double-POST the login endpoint.
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # API-version negotiation (Accept header ``version=`` token)
    # ------------------------------------------------------------------

    def _api_version(self, target: VcloudDirectorTargetLike) -> str:
        """Return the vCD API version for *target*'s ``Accept`` header.

        A target may override the module default via
        ``extras["vcd_api_version"]`` (e.g. ``"37.0"`` for a vCD 10.4 source);
        absent / malformed falls back to :data:`._paths._DEFAULT_VCD_API_VERSION`.
        Reading the max supported version from ``GET /api/versions`` is a
        follow-up.
        """
        extras = getattr(target, "extras", None)
        if isinstance(extras, dict):
            override = extras.get("vcd_api_version")
            if isinstance(override, str) and override.strip():
                return override
        return _DEFAULT_VCD_API_VERSION

    # ------------------------------------------------------------------
    # auth_headers — ABC override (Bearer; Accept added per-path by _vcd_get)
    # ------------------------------------------------------------------

    async def auth_headers(
        self, target: VcloudDirectorTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <jwt>"}``, minting the session on first use.

        The minted provider token authenticates both the ``/cloudapi/*`` and the
        classic ``/api/*`` surfaces, so this header is path-independent; the
        per-surface ``Accept`` is layered on by :meth:`_vcd_get` (via
        ``_request_json``'s ``extra_headers``). ``operator`` is threaded to the
        credential loader so the first-use Vault read runs under the operator's
        identity (reused on warm-path calls without re-reading Vault).

        Raises :exc:`NotImplementedError` for any ``target.auth_model`` other
        than ``shared_service_account`` / ``None`` — the shared VCF-family gate.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcloudDirectorConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: VcloudDirectorTargetLike, operator: Operator) -> str:
        """Return the cached provider JWT, establishing it on first use.

        ``operator.raw_jwt`` must be non-empty: the credential read runs under
        the operator's Vault identity, and a system-initiated call (empty JWT)
        cannot resolve per-target vendor credentials. The guard is enforced
        **before** the cache lookup so a token primed by an authenticated caller
        can never be handed to an unauthenticated one — the defense-in-depth
        cache-scoping contract the ``vcf-automation`` provider plane established
        (``docs/architecture/connector-auth.md``).
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
            creds = await self._credentials_loader(target, operator)
            client = await self._http_client(target)
            # Lazy import so pure fingerprint/probe unit tests don't pay the
            # _auth module's import; the login helper owns the wire POST.
            from meho_backplane.connectors.vcd._auth import vcd_provider_login

            jwt = await vcd_provider_login(
                client,
                creds,
                target,
                api_version=self._api_version(target),
                request_extensions=self._request_extensions(target),
            )
            self._session_tokens[cache_key] = jwt
            return jwt

    async def _vcd_get(
        self,
        target: VcloudDirectorTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON, with the vCD per-surface ``Accept``.

        Layers the path-derived ``Accept`` (:func:`._paths.accept_for_path`)
        onto the connector's ``Authorization: Bearer`` header via the base
        transport's ``extra_headers`` seam, so the base tenacity retry (5xx /
        connection) and the ``auth_headers`` mint both stay in force. A raw 401
        (expired token) propagates as :class:`httpx.HTTPStatusError` to the
        dispatcher's credential-recovery arm, which evicts the token via
        :meth:`invalidate_credentials` (#2396) and re-dispatches once.
        """
        accept = accept_for_path(path, self._api_version(target))
        return await self._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params=params,
            extra_headers={"Accept": accept},
        )

    # ------------------------------------------------------------------
    # fingerprint / probe — unauthenticated GET /api/versions
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: VcloudDirectorTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from the unauthenticated ``GET /api/versions`` probe.

        Reads vCD's supported-versions endpoint **without** minting a session
        (pure reachability). On success returns a reachable
        :class:`FingerprintResult` (``vendor="vmware"``, ``product="vcd"``);
        ``version`` is left ``None`` — ``/api/versions`` reports API versions,
        not the product version, and the connector does not fabricate one. On
        transport / status failure returns a non-reachable result whose
        ``extras["error"]`` names the exception — the Harbor / NSX / vcf-fleet
        pattern. ``operator`` is unused (the probe is unauthenticated) but kept
        for signature parity with the credentialed connectors.
        """
        del operator  # the /api/versions probe is unauthenticated
        probed_at = datetime.now(UTC)
        try:
            client = await self._http_client(target)
            resp = await client.get(
                _VERSIONS_PATH,
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="vcd",
                reachable=False,
                probed_at=probed_at,
                probe_method=_VCD_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product="vcd",
            version=None,
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_VCD_PROBE_METHOD,
            extras={
                "versions_status": resp.status_code,
                "product_lineage": "vmware-cloud-director",
                "api_surface": "vcloud-director",
            },
        )

    async def probe(self, target: VcloudDirectorTargetLike) -> ProbeResult:
        """Lightweight reachability check — delegates to :meth:`fingerprint`.

        ``GET /api/versions`` is the single unauthenticated reachability surface;
        reusing the fingerprint result keeps probe + fingerprint pinned to one
        round-trip, the SDDC Manager / NSX / vcf-fleet delegation shape.
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
        target: VcloudDirectorTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Post-G0.6 callers (``/api/v1/operations/call``, MCP ``call_operation``,
        the CLI verbs) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't reach
        this method. The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``: ``"vcd-rest-10.6"`` ->
        (product=``"vcd"``, version=``"10.6"``, impl_id=``"vcd-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vcd-connector-shim",
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
    # Typed read ops (#3057, initiative #3056)
    #
    # Thin bound-method shims for the migration-inventory read core. Each
    # delegates to the module-level ``_impl`` body in ``.typed_reads`` (lazy
    # import so pure fingerprint/probe unit tests don't pay the operations
    # package's embedding-pipeline import). The dispatcher resolves each
    # persisted ``module.ClassName.method`` handler_ref back to the bound
    # method and threads ``operator`` / ``target`` / ``params`` by name. All
    # read-only — no write op ships here.
    # ------------------------------------------------------------------

    async def org_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.org.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_org_list_impl

        return await vcd_org_list_impl(self, operator, target, params)

    async def vdc_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.vdc.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_vdc_list_impl

        return await vcd_vdc_list_impl(self, operator, target, params)

    async def vapp_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.vapp.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_vapp_list_impl

        return await vcd_vapp_list_impl(self, operator, target, params)

    async def vm_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.vm.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_vm_list_impl

        return await vcd_vm_list_impl(self, operator, target, params)

    async def catalog_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.catalog.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_catalog_list_impl

        return await vcd_catalog_list_impl(self, operator, target, params)

    async def edge_gateway_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.edge-gateway.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_edge_gateway_list_impl

        return await vcd_edge_gateway_list_impl(self, operator, target, params)

    async def task_list(
        self, operator: Operator, target: VcloudDirectorTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcd.task.list`` shim (#3057)."""
        from meho_backplane.connectors.vcd.typed_reads import vcd_task_list_impl

        return await vcd_task_list_impl(self, operator, target, params)

    async def invalidate_credentials(self, target: VcloudDirectorTargetLike) -> None:
        """Public duck-typed credential-eviction hook for the dispatch path (#2396).

        Drops the cached provider JWT for *target* so the next credential read
        re-mints a fresh session. The dispatcher calls this hook on an
        establish-auth failure (a data-path 401 from an expired token) and
        re-dispatches once, so an out-of-band credential restage — or a simply
        expired session — converges on the next dispatch without a backplane
        restart. The ``fleet-lcm`` credential-recovery pattern.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            self._session_tokens.pop(cache_key, None)

    async def aclose(self) -> None:
        """Clear cached session tokens, then tear down the httpx pool.

        No server-side session revoke — the Bearer header is sent per request.
        The token cache is cleared so a post-aclose reuse of the same connector
        instance re-mints cleanly.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await super().aclose()
