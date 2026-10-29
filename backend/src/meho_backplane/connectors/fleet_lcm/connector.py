# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FleetLcmConnector — hand-rolled HttpConnector subclass for the VCF 9 Fleet LCM Service.

The **modern** generic implementation of ``product=fleet`` — the second,
coexisting impl beside the legacy typed ``fleet-rest``
(:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`).
This is the first real two-impl case in the codebase; the two are
resolved per target by fingerprint (see
:func:`~meho_backplane.connectors.resolver.resolve_connector`):

* ``fleet-lcm`` (this class) advertises ``supported_version_range=">=9.1,<10.0"``
  — the ``/fleet-lcm/v1`` API first ships with VCF 9.1 (the spec file is in
  the ``9.1.0.0`` tag of ``vmware/vcf-api-specs``), so a 9.1 or newer
  target resolves here.
* ``fleet-rest`` advertises ``">=8.0,<9.1"`` — Aria Suite Lifecycle 8.x and
  VCF Fleet 9.0, which serve the legacy ``/lcm/*`` API. An 8.x or 9.0
  target resolves there.

The two ranges do not overlap (#4442), so each version has exactly one
versioned candidate.

The two impls target **distinct API surfaces** on the same product family:
``fleet-lcm`` dispatches the new ``https://vcf.broadcom.com/fleet-lcm`` →
``/v1/*`` service (health, config, components, sddc-lcms, upgrade-plans,
tasks, system); ``fleet-rest`` dispatches the legacy vRSLCM-derived
``/lcm/lcops/api/v2/*`` surface. The modern surface is the VCF 9.1
successor to vRealize Suite Lifecycle Manager (vRSLCM) 8.x; a VCF 9.0
fleet appliance serves only the legacy ``/lcm/*`` API, which is why 9.0
sits in the ``fleet-rest`` range and the resolver — not the connector —
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

The API takes a Bearer token that VCF Operations hands out (#4476).
:meth:`auth_headers` returns ``Authorization: Bearer <token>``. The token
comes from one of two places:

* **Login (the normal way).** MEHO logs in to VCF Operations with the
  ``username`` / ``password`` from the target's Vault secret
  (``token/acquire``), then trades that token for a fleet lifecycle token
  (``token/exchange`` with the service key ``fleet-lcm``). See
  :func:`~meho_backplane.connectors.fleet_lcm.session.mint_fleet_lcm_token`.
  The login goes to ``vcf_ops_url`` from the secret when it is set, else to
  the target's own host and port. The login paths start at the root of the
  host, so a service path carried on the target's ``host`` (for example
  ``/fleet-lcm``) is not used for the login. The token is kept per credential
  (``credential_cache_key``: tenant, secret and the resolved login address)
  until :meth:`invalidate_session` drops it. So two fleet targets with one
  secret and one ``vcf_ops_url`` share one token, whatever their own hosts
  and ``fqdn`` values are (the login does not send the ``fqdn``, and the
  key does not hold it). The connector remembers which login key each
  target used, so the ``401`` path finds the token without reading the
  secret again.
* **Stored token (fallback).** When the secret carries a ``token``, MEHO
  sends it as is and does not log in.

On a ``401`` from a fleet call, the dispatcher calls
:meth:`invalidate_session` and runs the call once more, so an expired token
is renewed with one new login. A second ``401`` ends as
``connector_auth_failed``.

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
:mod:`._llm_instructions`), enabled at register time, so a VCF Fleet 9.1
or newer target — which resolves to this impl by version range —
dispatches a modern read op on a **fresh boot with zero catalog ingest**
(initiative #3033); the two typed ops the legacy ``fleet-rest``
impl carries are its ``/lcm/*`` back-compat surface, distinct from these
``/v1/*`` reads.

The handler bodies build their ``/v1/*`` request paths from the templates
in :mod:`._paths` (reconcile-lane-introspectable) and dispatch on this
class's authenticated client, so the reads ride :meth:`auth_headers`
(the VCF Operations Bearer token) exactly as an ingested op would. The
wider surface — the full 51-op spec as ``source_kind="ingested"`` breadth
plus the component / upgrade / task **writes** — is a follow-up enabled
operationally through the generic review flow
(``ReviewService.enable_reads`` over ``meho connector ingest`` of the
pinned ``fleet-lcm-openapi.yaml``); :meth:`execute` remains the G0.6
dispatch shim for any such ingested ``op_id``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import credential_cache_key, target_cache_key
from meho_backplane.connectors._shared.rejected_token import drop_rejected_token
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import (
    CredentialsCache,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.fleet_lcm.session import (
    FleetLcmCredentialsLoader,
    FleetLcmTargetLike,
    load_credentials_from_vault,
    mint_fleet_lcm_token,
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
    """VCF 9 Fleet LCM Service REST connector — Bearer token from VCF Operations.

    Per-target credentials are kept in :attr:`_creds` (the shared
    :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`,
    loaded with the injectable
    :class:`~meho_backplane.connectors.fleet_lcm.session.FleetLcmCredentialsLoader`).
    Per-target Bearer tokens from the VCF Operations login are kept in
    :attr:`_session_tokens`.

    :attr:`priority` is ``1`` — equal to the legacy ``fleet-rest`` impl by
    design: the two impls are split by **version range**, not priority
    (``fleet-lcm`` covers ``>=9.1,<10.0`` and ``fleet-rest`` covers
    ``>=8.0,<9.1``, so only one is in range for any version). A higher
    priority would be redundant; ``1`` also keeps a stray ``GenericRestConnector``
    auto-shim from out-ranking this hand-coded class on the shared
    registry.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"fleet-lcm-9.0"`` -> (``"fleet"``, ``"9.0"``, ``"fleet-lcm"``).
    product = "fleet"
    version = "9.0"
    impl_id = "fleet-lcm"
    supported_version_range = ">=9.1,<10.0"
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
        self._session_tokens: dict[tuple[str, ...], str] = {}
        # Per target: the login key its token sits under. The key needs the
        # login address from the secret, which the 401 path does not have.
        self._login_keys: dict[tuple[str, str], tuple[str, ...]] = {}
        self._session_lock = asyncio.Lock()

    async def auth_headers(self, target: FleetLcmTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}`` for *target*.

        Reads the credentials from Vault under the operator's identity
        (kept per target after the first read). A stored ``token`` is sent
        as is; otherwise the token comes from the VCF Operations login,
        which runs once per target and is then reused.

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
        # ``CredentialsCache.get`` refuses an operator without a JWT even on a
        # cache hit, so a token minted for one caller never reaches a
        # system-initiated one.
        creds = await self._creds.get(target, operator)
        token = creds.get("token") or await self._session_token(target, creds)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: FleetLcmTargetLike, creds: dict[str, str]) -> str:
        """Return the kept Bearer token for *target*, logging in on first use.

        The lock makes concurrent first callers for a target share one login.
        The token is keyed by the resolved login address (``vcf_ops_url`` from
        the secret, else the target's own host), not the target's address.
        """
        login_target: Any = _LoginEndpoint(target, creds.get("vcf_ops_url"))
        cache_key = credential_cache_key(login_target)
        async with self._session_lock:
            self._login_keys[target_cache_key(target)] = cache_key
            cached = self._session_tokens.get(cache_key)
            if cached is not None:
                return cached
            client = await self._http_client(login_target)
            token = await mint_fleet_lcm_token(
                client,
                username=creds["username"],
                password=creds["password"],
                target_name=target.name,
                request_extensions=self._request_extensions(login_target),
            )
            self._session_tokens[cache_key] = token
            _log.info(
                "fleet_lcm_session_established",
                target=target.name,
                login_host=login_target.host,
            )
            return token

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

    async def invalidate_session(
        self, target: FleetLcmTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the kept token and credentials for *target* (the #2067 seam).

        The dispatcher calls this on a ``401`` from a fleet call and then
        runs the call once more. The next call reads Vault again and logs in
        again, so both an expired login token and a replaced stored token
        are picked up. With *rejected*, the token is dropped only when that
        request sent it; a newer token another call just logged in for
        stays (#5033).
        """
        async with self._session_lock:
            cache_key = self._login_keys.get(target_cache_key(target))
            if cache_key is not None:
                drop_rejected_token(
                    self._session_tokens,
                    cache_key,
                    rejected,
                    lambda token: {"Authorization": f"Bearer {token}"},
                )
        await self._creds.invalidate(target)

    async def invalidate_credentials(self, target: FleetLcmTargetLike) -> None:
        """Drop the kept credentials for *target* (#2396).

        The dispatcher calls this when the login itself is refused, so the
        next call re-reads Vault after an operator fixes the secret.
        """
        await self._creds.invalidate(target)

    async def aclose(self) -> None:
        """Clear kept tokens and credentials, then close the httpx pool."""
        async with self._session_lock:
            self._session_tokens.clear()
            self._login_keys.clear()
        await self._creds.clear()
        await super().aclose()


class _LoginEndpoint:
    """The target, seen at the address where VCF Operations answers.

    The login paths start at the root of the host (``/suite-api/...``), so
    the login never runs on the fleet client: a target's ``host`` may carry
    the ``/fleet-lcm`` service path, and that path must not sit in front of
    the login paths. Without ``vcf_ops_url`` the login goes to the target's
    own host and port, with any path on ``host`` dropped. With
    ``vcf_ops_url`` it goes to that address, and an IPv6 address keeps its
    brackets so the client address stays valid.

    Everything else (``id`` / ``tenant_id``, ``verify_tls``, ``tls_ca_pin``)
    comes from the target, so the login gets its own pooled client with the
    same TLS rules and the same address check. A ``tls_server_name`` set on
    the target belongs to the target's address, so it is kept only when the
    login goes there.
    """

    def __init__(self, target: FleetLcmTargetLike, ops_url: str | None) -> None:
        self._target = target
        if ops_url:
            url = httpx.URL(ops_url)
            self.host = f"[{url.host}]" if ":" in url.host else url.host
            self.port = url.port
            self.extras = {"scheme": "https"}
            self.tls_server_name: str | None = None
        else:
            self.host = str(target.host).partition("/")[0]
            self.port = target.port
            self.tls_server_name = getattr(target, "tls_server_name", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)
