# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcfFleetConnector — hand-rolled HttpConnector subclass for VCF Fleet 9.0.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in #835 via G0.7 spec ingestion against the Fleet
(vRSLCM-derived) OpenAPI surface. Until then the connector is registered
and discoverable but ``execute(target, op_id, ...)`` against any
``op_id`` resolves to "unknown operation" at the dispatcher layer.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vcf_fleet.__init__`.

Auth
----

VCF Fleet (vRSLCM-derived lifecycle manager, rebranded under VCF 9) uses
**HTTP Basic** against its **own LCM-local user store** — typically the
``admin@local`` account. There is **no SSO federation**. The stored
username is sent verbatim in the ``Authorization: Basic`` header; no
realm suffix is appended.

Verified against the consumer wrapper ``scripts/vcf-fleet.sh`` (header
comment, 2026-05-21): *"HTTP Basic against Fleet's own LCM-local user
store (typical username `admin@local`). Fleet does NOT federate with
vCenter SSO out of the box."* and the curl invocation
``curl -u "${FLEET_USERNAME}:${FLEET_PASSWORD}" ...`` — no realm
decoration, the wrapper sends ``admin@local:<password>`` literally.

The connector caches the loaded credentials per ``target.name`` via the
shared :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
(load-once-per-target with the missing-key → :exc:`RuntimeError`
contract); :meth:`auth_headers` reuses the cached dict on subsequent
calls. No session token is established — Basic is sent per request.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT`
(or ``None`` for pre-G0.3 targets where the column hasn't been populated
yet). :meth:`auth_headers` rejects any other ``target.auth_model`` value
with a clear :exc:`NotImplementedError` naming both the target and the
requested mode — same posture the Harbor / NSX / SDDC Manager
precedents established and the shared module's
:func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
gate enforces.

Fingerprint
-----------

Fleet's first-party diagnostic endpoints (``/lcm/lcops/api/v2/about``,
``/health``, ``/version``, ``/system-details``, ``/lcm/common/api/about``,
``/lcm/locker/api/v2/about``) **return HTTP 500 in VCF 9.0 builds** —
known issue, not a credential problem. The consumer wrapper
``scripts/vcf-fleet.sh`` documents this explicitly in its probe block
and works around it by hitting ``/lcm/lcops/api/v2/datacenters`` with
HTTP Basic auth and reading the ``Lcm-API-Version`` response header for
the LCM API version. The product version itself is **not** exposed by
any working Fleet endpoint in 9.0 — operators cross-source it from
SDDC Manager's ``/v1/vcf-services`` (LCM service entry) when needed.

The connector follows the wrapper's verified path:

* ``probe_method`` = ``GET /lcm/lcops/api/v2/datacenters with HTTP
  Basic; read Lcm-API-Version response header``.
* ``version`` → the ``Lcm-API-Version`` header value (e.g. ``"8.0"``)
  when present, ``None`` otherwise (the connector does **not**
  cross-source the product version from SDDC Manager — that's an
  operator-context concern out of scope for the per-product skeleton).
* ``build`` → ``None`` (Fleet does not expose a build string via any
  working endpoint in 9.0).
* ``extras`` carries ``lcm_api_version`` (mirroring the wrapper's
  ``probe`` JSON shape — kept distinct from ``version`` so a future
  switch to a working product-version source doesn't break field
  semantics), ``datacenter_count`` (length of the response array),
  ``product_lineage`` = ``"vmware-vrealize-suite-lifecycle-manager"``,
  and ``diagnostic_endpoints_broken`` listing the 9.0-broken paths so
  the next operator probing this product sees the known-issue list
  inline.

On transport or status failure (including the 401 / 500 cases) the
connector returns a non-reachable :class:`FingerprintResult` whose
``extras["error"]`` carries the exception class + message — same
pattern Harbor / NSX / SDDC Manager / VCF Automation use.

Probe
-----

:meth:`probe` delegates to :meth:`fingerprint`. Fleet does not expose a
dedicated health endpoint that works in 9.0 (the four-broken-diagnostic
matrix above); the datacenters call is the single reachability surface
that proves both transport and auth, so reusing the fingerprint result
is the right shape.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim
:meth:`execute` exists for ABC compatibility but operations land in the
``endpoint_descriptor`` table via #835's spec ingestion. Until then,
the connector is registered and discoverable but
``execute(target, op_id, ...)`` against any ``op_id`` resolves to
"unknown operation" at the dispatcher layer — which is the correct
behaviour for a registered-but-empty connector at this Task's stage.
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
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcf_fleet.session import (
    VcfFleetCredentialsLoader,
    VcfFleetTargetLike,
    load_credentials_from_vault,
)

__all__ = ["VcfFleetConnector"]

_log = structlog.get_logger(__name__)

# Fleet's first-party diagnostic endpoints all return HTTP 500 in 9.0
# builds (verified against the consumer wrapper scripts/vcf-fleet.sh
# probe-block error hint, 2026-05-21). Carried in the FingerprintResult
# extras so the next operator probing this product sees the
# known-issue inventory inline rather than rediscovering it.
_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS: tuple[str, ...] = (
    "/lcm/lcops/api/v2/about",
    "/lcm/lcops/api/v2/health",
    "/lcm/lcops/api/v2/version",
    "/lcm/lcops/api/v2/system-details",
    "/lcm/common/api/about",
    "/lcm/locker/api/v2/about",
)

# Wrapper-verified probe path: Fleet's diagnostic endpoints (about /
# health / version / system-details) return HTTP 500 in VCF 9.0;
# /lcm/lcops/api/v2/datacenters is the only reachability surface the
# wrapper found that proves both transport and HTTP Basic auth.
_FLEET_PROBE_PATH = "/lcm/lcops/api/v2/datacenters"
_FLEET_PROBE_METHOD = (
    "GET /lcm/lcops/api/v2/datacenters with HTTP Basic; read Lcm-API-Version response header"
)
_FLEET_LCM_API_VERSION_HEADER = "Lcm-API-Version"

# Typed read-op endpoint paths (T4 · #2304). The audited read set — the
# about/health probe + the component-inventory ("what's deployed") list —
# dispatches off these paths as ``source_kind="typed"`` on the connector's
# existing HTTP Basic session, with no dependence on ingesting the
# crash-prone Fleet LCM spec.
_FLEET_ABOUT_PATH = "/lcm/lcops/api/v2/about"
_FLEET_ENVIRONMENTS_PATH = "/lcm/lcops/api/v2/environments"


class VcfFleetConnector(HttpConnector):
    """VCF Fleet 9.0 REST connector with HTTP Basic auth.

    Per-target credentials cached in :attr:`_creds` via the shared
    :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
    helper (loaded once via the injectable :class:`VcfFleetCredentialsLoader`).
    HTTP Basic auth is sent on every request via
    ``Authorization: Basic <base64>`` — no session token is established
    and no 401-driven re-login is needed (Fleet's local user store
    accepts the same Basic header per request, same shape as Harbor).

    The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the
    same triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"fleet-rest-9.0"`` -> (``"fleet"``, ``"9.0"``, ``"fleet-rest"``).
    product = "fleet"
    version = "9.0"
    impl_id = "fleet-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: VcfFleetCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        loader: VcfFleetCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._creds: CredentialsCache = CredentialsCache(loader, product_label="fleet")

    async def auth_headers(self, target: VcfFleetTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target* via
        the shared :class:`CredentialsCache` and reuses the cached
        values on subsequent calls. The full ``operator`` is threaded
        into the loader so the live default
        (:func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`)
        reads the per-target KV-v2 secret under the operator's Vault
        Identity entity via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator` —
        the locked Option A decision. An injected test loader receives
        the same ``(target, operator)`` pair.

        The Basic auth username is sent verbatim from the Vault-loaded
        credentials — no SSO-realm suffix is appended. The typical
        Fleet account is ``admin@local`` (literal — the ``@local``
        suffix is part of the stored username, not a realm
        decoration); production deploys store the username verbatim
        under the target's ``secret_ref``.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcfFleetConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._creds.get(target, operator)
        return {"Authorization": basic_auth_header(creds["username"], creds["password"])}

    async def fingerprint(
        self,
        target: VcfFleetTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from the wrapper-verified probe call.

        Fleet's first-party diagnostic endpoints return HTTP 500 in 9.0
        (see module docstring); the consumer wrapper
        ``scripts/vcf-fleet.sh`` works around this by calling
        ``GET /lcm/lcops/api/v2/datacenters`` with HTTP Basic auth and
        reading the ``Lcm-API-Version`` response header for the LCM
        API version. The connector follows that contract verbatim.

        Carries the wrapper's ``extras`` shape: ``lcm_api_version``,
        ``datacenter_count``, ``product_lineage``, and the
        ``diagnostic_endpoints_broken`` known-issue inventory. The
        product version itself is **not** surfaced here — Fleet does
        not expose it via any working endpoint in 9.0, and the
        wrapper's note ("cross-source from SDDC Manager
        ``/v1/vcf-services``") is an operator-context concern that
        belongs above the connector layer.

        On transport or status failure (401 / 500 / connection
        error), returns a non-reachable :class:`FingerprintResult`
        whose ``extras["error"]`` carries the exception class +
        message — same pattern Harbor / NSX / SDDC Manager / VCF
        Automation use.

        ``operator`` (optional, G0.16-T4 #1306) is forwarded to the
        credentials loader so the per-target Vault read happens under
        the operator's identity, matching the dispatch path. ``None``
        falls back to a system operator whose placeholder JWT fails
        closed at the live Vault round-trip.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            client = await self._http_client(target)
            headers = await self.auth_headers(target, eff_operator)
            resp = await client.get(
                _FLEET_PROBE_PATH,
                headers={"Accept": "application/json", **headers},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="fleet",
                reachable=False,
                probed_at=probed_at,
                probe_method=_FLEET_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        lcm_api_version = resp.headers.get(_FLEET_LCM_API_VERSION_HEADER) or None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        datacenter_count = len(payload) if isinstance(payload, list) else None
        return FingerprintResult(
            vendor="vmware",
            product="fleet",
            # Carry the LCM API version into `version` as the only
            # version string Fleet exposes via a working endpoint in
            # 9.0; the product version itself stays None (operators
            # cross-source from SDDC Manager when needed).
            version=lcm_api_version,
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_FLEET_PROBE_METHOD,
            extras={
                "lcm_api_version": lcm_api_version,
                "datacenter_count": datacenter_count,
                "product_lineage": "vmware-vrealize-suite-lifecycle-manager",
                "diagnostic_endpoints_broken": list(_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS),
            },
        )

    async def probe(self, target: VcfFleetTargetLike) -> ProbeResult:
        """Lightweight reachability check — delegates to :meth:`fingerprint`.

        Fleet does not expose a working dedicated health endpoint in
        VCF 9.0 builds (see :meth:`fingerprint` for the broken-diagnostic
        matrix); the datacenters call is the only surface that proves
        both transport and HTTP Basic auth, so reusing the fingerprint
        result keeps probe + fingerprint pinned to one round-trip.
        Same delegation shape as the SDDC Manager / NSX / VCF Automation
        precedents.
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
        target: VcfFleetTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`VcfAutomationConnector.execute` /
        :meth:`HarborConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI
        verbs once #839 lands) construct a real :class:`Operator` and
        call :func:`meho_backplane.operations.dispatch` directly —
        they don't reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"fleet-rest-9.0"`` → (product=``"fleet"``,
        version=``"9.0"``, impl_id=``"fleet-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:fleet-rest-connector-shim",
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
    # Typed read ops (T4 · #2304, Initiative #2266)
    #
    # The audited Fleet read set — about/health probe + component
    # inventory — dispatched off the connector's existing HTTP Basic
    # session as ``source_kind="typed"`` (no ingested endpoint_descriptor
    # rows, no Fleet-LCM spec dependency). The dispatcher rebinds these
    # bound methods to the per-process connector instance and threads
    # ``operator`` by name (see ``dispatch_typed``); ``operator`` is
    # forwarded to ``_get_json`` so the credential loader reads the
    # per-target Basic credentials under the operator's identity. Both are
    # read-only — no write op ships here (writes are out of #2266 scope).
    # ------------------------------------------------------------------

    async def about(
        self,
        operator: Operator,
        target: VcfFleetTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``fleet.about`` — ``GET /lcm/lcops/api/v2/about``.

        Returns the appliance identity payload (``apiVersion`` /
        ``productVersion`` / ``buildNumber`` / ``releaseDate``). KNOWN
        REGRESSION: in VCF 9.0 builds this endpoint returns HTTP 500 — the
        dispatcher records that as a ``connector_error`` result; the
        curated ``llm_instructions`` tell the agent the appliance is still
        reachable (the connector probe confirms it off the datacenters
        surface) and to cross-source the product version from SDDC Manager.
        """
        del params  # schema declares the param object empty
        return await self._get_json(target, _FLEET_ABOUT_PATH, operator=operator)

    async def environment_list(
        self,
        operator: Operator,
        target: VcfFleetTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``fleet.environment.list`` — ``GET /lcm/lcops/api/v2/environments``.

        The component inventory ("what's deployed"). Fleet returns a bare
        JSON array of Environment objects; the handler wraps it under an
        ``environments`` key so the typed result is a stable object
        envelope (mirrors the vSphere typed reads' ``{"hosts": [...]}``
        shape). Each environment carries its status and an inline
        ``products[]`` summary.
        """
        del params  # schema declares the param object empty
        environments: Any = await self._get_json(
            target, _FLEET_ENVIRONMENTS_PATH, operator=operator
        )
        return {"environments": environments}

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`FLEET_TYPED_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.vcf_fleet.__init__`) after the
        registry has eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.vcf_fleet.typed_ops.FLEET_TYPED_OPS`,
        resolves each op's ``handler_attr`` to the class-visible handler,
        looks the group's curated ``when_to_use`` up in
        :data:`~meho_backplane.connectors.vcf_fleet.typed_ops.FLEET_TYPED_WHEN_TO_USE_BY_GROUP`,
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`
        (``source_kind="typed"``). Idempotent across restarts (the helper
        skips the embedding recompute on unchanged text). Mirrors the
        argocd / bind9 / vmware_rest ``register_operations`` shape.
        """
        # Lazy import: the operations package pulls in the embedding
        # pipeline (ONNX runtime + model), which pure fingerprint/probe
        # unit tests should not pay. Lifespan callers have it warmed by the
        # time this runs.
        from meho_backplane.connectors.vcf_fleet.typed_ops import (
            FLEET_TYPED_OPS,
            FLEET_TYPED_WHEN_TO_USE_BY_GROUP,
        )
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in FLEET_TYPED_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"VcfFleetConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = FLEET_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"VcfFleetConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use exists "
                        f"for that key. Add an entry to "
                        f"FLEET_TYPED_WHEN_TO_USE_BY_GROUP (typed_ops.py)."
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
            "fleet_typed_operations_registered",
            count=len(FLEET_TYPED_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def aclose(self) -> None:
        """Clear cached credentials, then tear down the httpx pool.

        No server-side session to revoke — HTTP Basic is stateless.
        The credential cache is cleared so a post-aclose reuse of the
        same connector instance starts clean.
        """
        await self._creds.clear()
        await super().aclose()
