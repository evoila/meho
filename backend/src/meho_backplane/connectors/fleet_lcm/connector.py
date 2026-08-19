# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FleetLcmConnector — hand-rolled HttpConnector subclass for the VCF 9 Fleet LCM Service.

The **modern** generic implementation of ``product=fleet`` — the second,
coexisting impl beside the legacy typed ``fleet-rest``
(:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`).
This is the first real two-impl case in the codebase; the two are
resolved per target by fingerprint (see
:func:`~meho_backplane.connectors.resolver.resolve_connector`):

* ``fleet-lcm`` (this class) advertises ``supported_version_range=">=9.0,<10.0"``
  — the narrower band, so a VCF Fleet 9.0 target resolves here by the
  resolver's most-specific-version tie-break.
* ``fleet-rest`` advertises ``">=8.0,<10.0"`` (re-banded from ``">=9.0,<10.0"``
  in this same initiative, #3037) — Aria Suite Lifecycle 8.x plus the VCF
  Fleet 9.0 ``/lcm/*`` back-compat surface. An 8.x target resolves there
  as the only in-range candidate.

The two impls target **distinct API surfaces** on the same product family:
``fleet-lcm`` dispatches the new ``https://vcf.broadcom.com/fleet-lcm`` →
``/v1/*`` service (health, config, components, sddc-lcms, upgrade-plans,
tasks, system); ``fleet-rest`` dispatches the legacy vRSLCM-derived
``/lcm/lcops/api/v2/*`` surface. The modern surface is the VCF 9
successor to vRealize Suite Lifecycle Manager (vRSLCM) 8.x; the legacy
``/lcm/*`` surface still answers on 9.0 appliances for back-compat, which
is why both bands overlap at 9.0 and the resolver — not the connector —
picks the surface per target.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim, the
same shape the legacy ``vcf_fleet`` connector first shipped as (#831).
The 51 ``/v1/*`` operations arrive via **G0.7 spec ingestion** against the
pinned public ``fleet-lcm-openapi.yaml`` (Apache-2.0,
``vmware/vcf-api-specs``) as ``source_kind="ingested"``
``endpoint_descriptor`` rows under ``connector_id="fleet-lcm-9.0"`` — they
are **not** hand-coded here. Registering this real
:class:`HttpConnector` subclass is the load-bearing prerequisite that
makes those ingested rows *dispatchable*: a bare ``GenericRestConnector``
auto-shim is non-dispatchable (its ``auth_headers`` / ``execute`` raise),
so the ingested ops ride *this* class's ``auth_headers`` on dispatch, the
:class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
pattern.

Auth
----

Per the pinned spec's ``securitySchemes``, the global security scheme is
``bearerToken`` (HTTP Bearer), with ``basicAuth`` (HTTP Basic) also
defined. :meth:`auth_headers` implements Bearer as the primary path and
Basic as the fallback:

* When the loaded credentials carry a non-empty ``"token"`` key, the
  header is ``Authorization: Bearer <token>`` — the spec's primary
  scheme.
* Otherwise the header is ``Authorization: Basic <b64>`` off the
  ``username`` / ``password`` pair — the spec's ``basicAuth`` alternative,
  reusing the shared
  :func:`~meho_backplane.connectors._shared.vcf_auth.basic_auth_header`
  the legacy impl uses.

The shared
:class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
enforces the ``{"username", "password"}`` contract, so the loader always
returns that pair (the appliance accepts ``basicAuth`` too) and may
additionally carry the optional ``"token"``. Credentials are cached
per tenant-unique ``(tenant_id, target.id)`` and reused on subsequent
calls, the legacy Basic shape verbatim.

**Bearer live-verify is a follow-up (non-goal here).** There is no
reachable Fleet LCM appliance (#1002 / #995), so the Bearer *header
shape* is unit-tested (an injected loader returning a ``"token"``) but
never exercised against a live appliance, and the default
:func:`~meho_backplane.connectors.fleet_lcm.session.load_credentials_from_vault`
surfaces only the ``username`` / ``password`` pair (the shared
basic-creds read) — so the live default is the Basic alternative until
the Bearer-token provisioning seam lands. That seam — a fleet-lcm loader
surfacing a Vault-stored token, or a POST-``basicAuth`` →
mint-``bearerToken`` session flow — is the documented live-verify
follow-up gated on a stood-up appliance, exactly the posture the legacy
skeleton shipped with.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets), the shared
:func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
gate the Harbor / NSX / SDDC Manager / vcf-fleet precedents established.

Fingerprint / probe
-------------------

Unlike the legacy impl — whose first-party diagnostic endpoints return
HTTP 500 on 9.0 builds, forcing a datacenters-list workaround — the
modern service exposes a first-class health endpoint. The connector
probes ``GET /v1/health`` (``operationId`` ``getHealth``; the spec marks
it ``security: []`` — no auth required, so it is the clean reachability
surface). :meth:`fingerprint` sends the connector's auth headers anyway
(harmless on an unauthenticated endpoint, and it keeps probe and dispatch
on one auth path); on any transport/status failure it returns a
non-reachable :class:`FingerprintResult` whose ``extras["error"]`` names
the exception, the Harbor / NSX / vcf-fleet pattern. The product version
is not assumed from any single ``/v1/*`` field; ``version`` is left
``None`` unless the health payload carries an explicit ``version`` string.

Operations
----------

A curated **13-op typed read core** ships here (#3047): health, system,
config, SDDC-LCM list/detail, component list/detail/status, task
list/detail, and upgrade-plan list/detail + release-versions. They are
registered as ``source_kind="typed"`` (:mod:`.typed_ops` /
:mod:`.typed_reads`, per-op ``llm_instructions`` in
:mod:`._llm_instructions`), enabled at register time, so a VCF Fleet 9.x
target — which resolves to this impl by most-specific-version — dispatches
a modern read op on a **fresh boot with zero catalog ingest**. This is what
restores 9.0 fleet dispatch after the "modern default now" resolver
decision (initiative #3033); the two typed ops the legacy ``fleet-rest``
impl carries are its ``/lcm/*`` back-compat surface, distinct from these
``/v1/*`` reads.

The handler bodies build their ``/v1/*`` request paths from the templates
in :mod:`._paths` (reconcile-lane-introspectable) and dispatch on this
class's authenticated client, so the reads ride :meth:`auth_headers`
(Bearer primary, Basic fallback) exactly as an ingested op would. The
wider surface — the full 51-op spec as ``source_kind="ingested"`` breadth
plus the component / upgrade / task **writes** — is a follow-up enabled
operationally through the generic review flow
(``ReviewService.enable_reads`` over ``meho connector ingest`` of the
pinned ``fleet-lcm-openapi.yaml``); :meth:`execute` remains the G0.6
dispatch shim for any such ingested ``op_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import (
    CredentialsCache,
    basic_auth_header,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.fleet_lcm.session import (
    FleetLcmCredentialsLoader,
    FleetLcmTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["FleetLcmConnector"]

_log = structlog.get_logger(__name__)

# Reachability probe: the Fleet LCM Service health endpoint (operationId
# ``getHealth``; ``security: []`` in the pinned spec — no auth required).
# Served as ``GET /v1/health`` — the spec's server url
# ``https://vcf.broadcom.com/fleet-lcm`` is absolute, so ``parse_openapi``
# does NOT fold the ``/fleet-lcm`` base onto path keys (only *relative*
# server bases are folded, #1796); the ingested op_id and this probe
# constant are both the raw ``/v1/*`` path, and the ``/fleet-lcm`` base is
# an operator target-configuration concern. The reconcile lane
# (test_connectors_fleet_lcm_spec_reconcile.py) introspects this constant
# and asserts the pinned spec serves ``GET:/v1/health``.
_FLEET_LCM_HEALTH_PATH = "/v1/health"
_FLEET_LCM_PROBE_METHOD = "GET /v1/health (VCF Fleet LCM Service health endpoint)"


class FleetLcmConnector(HttpConnector):
    """VCF 9 Fleet LCM Service REST connector — Bearer (Basic fallback) auth.

    Per-target credentials cached in :attr:`_creds` via the shared
    :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
    (loaded once via the injectable
    :class:`~meho_backplane.connectors.fleet_lcm.session.FleetLcmCredentialsLoader`).
    :meth:`auth_headers` emits ``Authorization: Bearer <token>`` when the
    loaded credentials carry a ``"token"`` (the spec's primary scheme),
    falling back to ``Authorization: Basic <b64>`` off the
    username/password pair (the spec's ``basicAuth`` alternative).

    :attr:`priority` is ``1`` — equal to the legacy ``fleet-rest`` impl by
    design: the two impls are split by **version-specificity**, not
    priority (``fleet-lcm``'s narrower ``>=9.0,<10.0`` wins a 9.0 target at
    the resolver's most-specific-version step, before priority is ever
    consulted). A higher priority would be redundant and would mask the
    specificity split; ``1`` also keeps a stray ``GenericRestConnector``
    auto-shim from out-ranking this hand-coded class on the shared
    registry.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"fleet-lcm-9.0"`` -> (``"fleet"``, ``"9.0"``, ``"fleet-lcm"``).
    product = "fleet"
    version = "9.0"
    impl_id = "fleet-lcm"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: FleetLcmCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        loader: FleetLcmCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._creds: CredentialsCache = CredentialsCache(loader, product_label="fleet-lcm")

    async def auth_headers(self, target: FleetLcmTargetLike, operator: Operator) -> dict[str, str]:
        """Return the ``Authorization`` header — Bearer primary, Basic fallback.

        Loads credentials from Vault on first call against *target* via the
        shared :class:`CredentialsCache` (reused on subsequent calls) and
        threads the full ``operator`` through so the live default loader
        reads the per-target secret under the operator's Vault Identity
        entity — the locked Option A decision, the same path the legacy
        impl uses.

        When the loaded credentials carry a non-empty ``"token"``, returns
        ``{"Authorization": "Bearer <token>"}`` (the spec's global
        ``bearerToken`` scheme). Otherwise returns
        ``{"Authorization": "Basic <b64>"}`` off the ``username`` /
        ``password`` pair (the spec's ``basicAuth`` alternative), reusing
        the shared :func:`basic_auth_header`. The
        :class:`CredentialsCache` contract guarantees the username/password
        pair is present, so the Basic fallback never raises ``KeyError``.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"FleetLcmConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._creds.get(target, operator)
        token = creds.get("token")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {"Authorization": basic_auth_header(creds["username"], creds["password"])}

    async def fingerprint(
        self,
        target: FleetLcmTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from the ``GET /v1/health`` probe.

        Sends the connector's auth headers (harmless on the
        ``security: []`` health endpoint, and it keeps probe + dispatch on
        one auth path) and reads the health payload. On success returns a
        reachable :class:`FingerprintResult` (``vendor="vmware"``,
        ``product="fleet"``); ``version`` is taken from an explicit
        ``version`` string in the health payload when present, else
        ``None`` (the modern service does not guarantee a product-version
        field on ``/v1/health``, and the connector does not fabricate one).

        On transport or status failure (401 / 5xx / connection error)
        returns a non-reachable :class:`FingerprintResult` whose
        ``extras["error"]`` carries the exception class + message — the
        Harbor / NSX / SDDC Manager / vcf-fleet pattern.

        ``operator`` (optional) is forwarded to the credentials loader so
        the per-target Vault read happens under the operator's identity,
        matching the dispatch path; ``None`` falls back to a system
        operator whose placeholder JWT fails closed at the live Vault
        round-trip.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            client = await self._http_client(target)
            headers = await self.auth_headers(target, eff_operator)
            resp = await client.get(
                _FLEET_LCM_HEALTH_PATH,
                headers={"Accept": "application/json", **headers},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="fleet",
                reachable=False,
                probed_at=probed_at,
                probe_method=_FLEET_LCM_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        health_status = payload.get("status") if isinstance(payload, dict) else None
        version = payload.get("version") if isinstance(payload, dict) else None
        return FingerprintResult(
            vendor="vmware",
            product="fleet",
            version=version if isinstance(version, str) and version else None,
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_FLEET_LCM_PROBE_METHOD,
            extras={
                "health_status": health_status,
                "product_lineage": "vmware-vrealize-suite-lifecycle-manager",
                "api_surface": "fleet-lcm-v1",
            },
        )

    async def probe(self, target: FleetLcmTargetLike) -> ProbeResult:
        """Lightweight reachability check — delegates to :meth:`fingerprint`.

        ``GET /v1/health`` is the single reachability surface; reusing the
        fingerprint result keeps probe + fingerprint pinned to one
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
        target: FleetLcmTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`VcfFleetConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI
        verbs) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method. The connector's natural key is encoded as the
        dispatcher's ``connector_id`` per ``parse_connector_id``:
        ``"fleet-lcm-9.0"`` → (product=``"fleet"``, version=``"9.0"``,
        impl_id=``"fleet-lcm"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:fleet-lcm-connector-shim",
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
    # Typed read ops (#3047, initiative #3033)
    #
    # Thin bound-method shims for the modern ``/v1/*`` read core. Each
    # delegates to the module-level ``_impl`` body in ``.typed_reads`` (lazy
    # import so pure fingerprint/probe unit tests don't pay the operations
    # package's embedding-pipeline import). The dispatcher resolves each
    # persisted ``module.ClassName.method`` handler_ref back to the bound
    # method and threads ``operator`` / ``target`` / ``params`` by name (see
    # ``dispatch_typed``); ``operator`` reaches ``_get_json`` so the
    # credential loader reads under the operator's identity. All read-only —
    # no write op ships here (writes are a follow-up).
    # ------------------------------------------------------------------

    async def health(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.health`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_health_impl

        return await fleet_lcm_health_impl(self, operator, target, params)

    async def system_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.system.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_system_info_impl

        return await fleet_lcm_system_info_impl(self, operator, target, params)

    async def config_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.config.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_config_info_impl

        return await fleet_lcm_config_info_impl(self, operator, target, params)

    async def sddc_lcm_list(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.sddc-lcm.list`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_sddc_lcm_list_impl

        return await fleet_lcm_sddc_lcm_list_impl(self, operator, target, params)

    async def sddc_lcm_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.sddc-lcm.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_sddc_lcm_info_impl

        return await fleet_lcm_sddc_lcm_info_impl(self, operator, target, params)

    async def component_list(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.component.list`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_component_list_impl

        return await fleet_lcm_component_list_impl(self, operator, target, params)

    async def component_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.component.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_component_info_impl

        return await fleet_lcm_component_info_impl(self, operator, target, params)

    async def component_status(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.component.status`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_component_status_impl

        return await fleet_lcm_component_status_impl(self, operator, target, params)

    async def task_list(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.task.list`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_task_list_impl

        return await fleet_lcm_task_list_impl(self, operator, target, params)

    async def task_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.task.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_task_info_impl

        return await fleet_lcm_task_info_impl(self, operator, target, params)

    async def upgrade_plan_list(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.upgrade-plan.list`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_upgrade_plan_list_impl

        return await fleet_lcm_upgrade_plan_list_impl(self, operator, target, params)

    async def upgrade_plan_info(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.upgrade-plan.info`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import fleet_lcm_upgrade_plan_info_impl

        return await fleet_lcm_upgrade_plan_info_impl(self, operator, target, params)

    async def release_version_list(
        self, operator: Operator, target: FleetLcmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet-lcm.release-version.list`` shim (#3047)."""
        from meho_backplane.connectors.fleet_lcm.typed_reads import (
            fleet_lcm_release_version_list_impl,
        )

        return await fleet_lcm_release_version_list_impl(self, operator, target, params)

    async def invalidate_credentials(self, target: FleetLcmTargetLike) -> None:
        """Public duck-typed credential-eviction hook for the dispatch path (#2396).

        Delegates to the shared :class:`CredentialsCache.invalidate` so the
        next credential read re-reads Vault. The dispatcher calls this hook
        on an establish-auth failure so an operator's out-of-band restage
        converges on the next dispatch without a backplane restart.
        """
        await self._creds.invalidate(target)

    async def aclose(self) -> None:
        """Clear cached credentials, then tear down the httpx pool.

        No server-side session to revoke — the Bearer/Basic header is sent
        per request. The credential cache is cleared so a post-aclose reuse
        of the same connector instance starts clean.
        """
        await self._creds.clear()
        await super().aclose()
