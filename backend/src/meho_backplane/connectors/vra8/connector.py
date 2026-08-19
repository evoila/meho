# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vra8Connector — hand-rolled HttpConnector subclass for vRealize Automation 8.x.

The **legacy dual-impl** for **vRealize Automation 8.x** (vRA 8) — the migration-
source estate evoila brings customers *off* (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3058). vRA 8
is the direct predecessor of the Broadcom-era **VCF Automation 9.x**
(:mod:`~meho_backplane.connectors.vcf_automation`, ``vcfa-rest``): the same product
line, renamed across a major-version rebuild. So this is a ``product="vcfa"``
**second implementation** (``impl_id="vcfa-vra8"``, band ``>=8.0,<9.0``), not a
net-new product — the **second real two-impl case after ``fleet``** (policy #3033 /
#3038). The resolver picks this legacy impl for an 8.x target by fingerprint and the
modern ``vcfa-rest`` for a 9.x target; the bands are **disjoint** (``>=9.0,<10.0``
for modern), so no re-band of the modern impl is needed and no specificity tie
arises. The ``("vcfa","","")`` wildcard is owned by the **modern** ``vcf_automation``
package (the inversion vs ``fleet``, where the legacy owns it), so an *unfingerprinted*
``vcfa`` target resolves to modern — a vRA 8 target must carry an 8.x product version
**operator-asserted at onboarding** (e.g. ``--version 8.12``) to resolve here; the
connector's fingerprint reports only the API version (a date label), not a resolvable
product version, so it cannot auto-populate the resolution version (see Fingerprint
below).

Why typed (not generic / ingested)
----------------------------------

``vmware/vra-sdk-go`` (Apache-2.0) publishes machine-readable specs for the vRA 8
services, but they are **Swagger 2.0**, and MEHO's ingest parser accepts OpenAPI
3.0/3.1 only (#2090) — so the surface cannot be operator-ingested. The read core is
**typed** (CLAUDE.md postulate 1), the ``vcf-automation`` tenant-plane / ``fleet-lcm``
/ ``vcd`` posture. The spec-reconcile lane is therefore a **manifest pin + evidenced
exclusion** (see ``docs/decisions/spec-reconcile-guards-standard.md``), not a
served-op-id compare.

Auth — two-step token exchange, single bearer
---------------------------------------------

vRA 8 mints a bearer via a **two-step** exchange (:mod:`._auth`), distinct from VCFA
9's ``/cloudapi/1.0.0/sessions/provider`` + ``/oauth/*`` flow (the divergence that
justifies a separate impl): ``POST /csp/gateway/am/api/login?access_token`` (CSP
identity) → a ``refresh_token``, then ``POST /iaas/api/login`` (``{refreshToken}``) →
a short-lived (~8h) bearer ``token`` in the response body. The one bearer
authenticates **every** vRA 8 service (``/iaas`` / ``/deployment`` / ``/blueprint`` /
``/catalog``) uniformly, so :meth:`auth_headers` is path-independent (returns
``Authorization: Bearer <token>``) and reads send a constant ``Accept:
application/json`` (:meth:`_vra_get`) — no per-surface negotiation (unlike ``vcd``).

The bearer is minted lazily on first use, cached per tenant-unique
``(tenant_id, target.id)`` key
(:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`) under a lock,
and re-minted on a 401 via the dispatcher's session-recovery arm
(:meth:`invalidate_session`, #2067) — the SDDC Manager / NSX / vcd session-minting
pattern, so no in-connector retry loop is needed and the base transport's tenacity
retry (5xx / connection only) stays intact for the flaky-legacy-appliance-over-VPN
case. A 401 eviction re-runs both login steps (the refresh token is not cached
separately — simplest state, rare re-mint).

The ``?access_token`` CSP query flag and the exact-wire-form of the CSP domain field
are pinned from the vRA 8 Programming Guide (not the vra-sdk-go spec, which omits the
CSP identity service); **live-appliance verification against a vRA 8 lab is a
documented deferred tail** (no vRA 8 lab was dialled during the build).

Fingerprint / probe
-------------------

``GET /iaas/api/about`` is vRA 8's IaaS API-capabilities endpoint (the same surface
the ``vcf-automation`` tenant probe reads). The connector fingerprints against it
**without minting a session** (pure reachability). ``version`` is left ``None``:
``/iaas/api/about`` reports the *API* version (a date label, e.g. ``2021-07-15``),
not the product build (8.11 vs 8.18), and the connector does not fabricate a
resolvable product version from a date — dual-impl resolution relies on the
operator-asserted target version (documented above). If a given appliance requires
auth on ``/iaas/api/about``, the fingerprint reports unreachable; resolution via the
asserted version is unaffected.

Operations
----------

A curated **6-op typed read core** ships here — the vRA 8 migration-inventory
surface: ``vcfa.vra8.project.list`` / ``.deployment.list`` / ``.deployment.get`` /
``.about`` (inventory group) and ``.blueprint.list`` / ``.catalog-item.list``
(content group). All read-only, ``safety_level=safe``, ungated; registered as
``source_kind="typed"`` (:mod:`.typed_ops` / :mod:`.typed_reads`, per-op
``llm_instructions`` in :mod:`._llm_instructions`) so they dispatch on a fresh boot
with **zero catalog ingest**. Set-shaped results are reduced to a JSONFlux handle by
the dispatcher. A write surface is deliberately out of scope for this read core.
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
from meho_backplane.connectors.vra8._paths import _ABOUT_PATH
from meho_backplane.connectors.vra8.session import (
    Vra8CredentialsLoader,
    Vra8TargetLike,
    load_credentials_from_vault,
)

__all__ = ["Vra8Connector"]

_log = structlog.get_logger(__name__)

# Reachability probe: vRA 8's IaaS API-capabilities endpoint. The reconcile lane
# (test_connectors_vra8_spec_reconcile.py) introspects this constant and pins
# GET:/iaas/api/about in the hand-coded manifest.
_VRA8_PROBE_METHOD = "GET /iaas/api/about (vRA 8 IaaS supported-API-versions endpoint)"

# Constant Accept for every read — one bearer, one media type across all vRA 8
# services (unlike vcd's per-surface split).
_JSON_ACCEPT = "application/json"


class Vra8Connector(HttpConnector):
    """vRealize Automation 8.x REST connector — two-step bearer session mint.

    Per-target minted bearers cached in :attr:`_session_tokens` (keyed on the
    tenant-unique ``(tenant_id, target.id)`` tuple) under :attr:`_session_lock`, so
    two same-named targets in different tenants never share a token
    (evoila/meho#1642/#1672). :meth:`auth_headers` returns ``Authorization: Bearer
    <token>``; :meth:`_vra_get` adds the constant ``Accept: application/json``.

    :attr:`priority` is ``1`` — the shim-tie-break convention every hand-rolled
    connector uses. The ``(product, version, impl_id)`` triple is ``("vcfa", "8.0",
    "vcfa-vra8")``; the ``connector_id`` ``"vcfa-vra8-8.0"`` parses back to it via
    ``parse_connector_id`` (``product`` = first hyphen-segment of ``impl_id`` =
    ``"vcfa"``, the round-trip invariant ``register_connector_v2`` enforces).
    """

    # G0.6 v2 registry metadata. Legacy dual-impl of product ``vcfa``: distinct
    # impl_id ``vcfa-vra8`` and a DISJOINT band ``>=8.0,<9.0`` (modern ``vcfa-rest``
    # is ``>=9.0,<10.0``), so an 8.x target resolves here and a 9.x target to modern
    # with no specificity tie. This impl does NOT register the ``("vcfa","","")``
    # wildcard — the modern ``vcf_automation`` package owns it.
    product = "vcfa"
    version = "8.0"
    impl_id = "vcfa-vra8"
    supported_version_range = ">=8.0,<9.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: Vra8CredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._credentials_loader: Vra8CredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        # Per-target minted-bearer cache, keyed on ``target_cache_key`` so the
        # tenant-isolation invariant holds; a single lock serialises concurrent
        # first-use callers so they don't double-run the two-step login.
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # auth_headers — ABC override (Bearer; Accept added per-read by _vra_get)
    # ------------------------------------------------------------------

    async def auth_headers(self, target: Vra8TargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}``, minting the session on first use.

        The minted bearer authenticates every vRA 8 service, so this header is
        path-independent; the constant ``Accept: application/json`` is layered on by
        :meth:`_vra_get`. ``operator`` is threaded to the credential loader so the
        first-use Vault read runs under the operator's identity (reused on warm-path
        calls without re-reading Vault).

        Raises :exc:`NotImplementedError` for any ``target.auth_model`` other than
        ``shared_service_account`` / ``None`` — the shared VCF-family gate.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"Vra8Connector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: Vra8TargetLike, operator: Operator) -> str:
        """Return the cached bearer, establishing it (two-step login) on first use.

        ``operator.raw_jwt`` must be non-empty: the credential read runs under the
        operator's Vault identity, and a system-initiated call (empty JWT) cannot
        resolve per-target vendor credentials. The guard is enforced **before** the
        cache lookup so a token primed by an authenticated caller can never be handed
        to an unauthenticated one — the defense-in-depth cache-scoping contract the
        ``vcf-automation`` provider plane established
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
            # Lazy import so pure fingerprint/probe unit tests don't pay the _auth
            # module's import; the login helper owns the two-step wire POSTs.
            from meho_backplane.connectors.vra8._auth import vra8_login

            token = await vra8_login(
                client,
                creds,
                target,
                request_extensions=self._request_extensions(target),
            )
            self._session_tokens[cache_key] = token
            return token

    async def _vra_get(
        self,
        target: Vra8TargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON, with the bearer + constant JSON ``Accept``.

        Layers ``Accept: application/json`` onto the connector's ``Authorization:
        Bearer`` header via the base transport's ``extra_headers`` seam, so the base
        tenacity retry (5xx / connection) and the ``auth_headers`` mint both stay in
        force. A raw 401 (expired bearer) propagates as
        :class:`httpx.HTTPStatusError` to the dispatcher's session-recovery arm,
        which evicts the token via :meth:`invalidate_session` (#2067) and
        re-dispatches once.
        """
        return await self._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params=params,
            extra_headers={"Accept": _JSON_ACCEPT},
        )

    # ------------------------------------------------------------------
    # fingerprint / probe — unauthenticated GET /iaas/api/about
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: Vra8TargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from the unauthenticated ``GET /iaas/api/about`` probe.

        Reads vRA 8's IaaS API-capabilities endpoint **without** minting a session
        (pure reachability). On success returns a reachable
        :class:`FingerprintResult` (``vendor="vmware"``, ``product="vcfa"``);
        ``version`` is left ``None`` — ``/iaas/api/about`` reports an API date label,
        not the product build, and the connector does not fabricate a resolvable
        product version (dual-impl resolution uses the operator-asserted target
        version). ``extras["latest_api_version"]`` carries the reported API label for
        the operator's information. On transport / status failure returns a
        non-reachable result whose ``extras["error"]`` names the exception — the vcd /
        vcf-fleet pattern. ``operator`` is unused (the probe is unauthenticated) but
        kept for signature parity with the credentialed connectors.
        """
        del operator  # the /iaas/api/about probe is unauthenticated
        probed_at = datetime.now(UTC)
        try:
            client = await self._http_client(target)
            resp = await client.get(
                _ABOUT_PATH,
                headers={"Accept": _JSON_ACCEPT},
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="vcfa",
                reachable=False,
                probed_at=probed_at,
                probe_method=_VRA8_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = {}
        latest_api_version = payload.get("latestApiVersion") if isinstance(payload, dict) else None
        supported_apis = payload.get("supportedApis") if isinstance(payload, dict) else None
        return FingerprintResult(
            vendor="vmware",
            product="vcfa",
            version=None,
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_VRA8_PROBE_METHOD,
            extras={
                "about_status": resp.status_code,
                "latest_api_version": latest_api_version,
                "supported_apis_count": (
                    len(supported_apis) if isinstance(supported_apis, list) else None
                ),
                "product_lineage": "vrealize-automation",
                "api_surface": "vra-8",
            },
        )

    async def probe(self, target: Vra8TargetLike) -> ProbeResult:
        """Lightweight reachability check — delegates to :meth:`fingerprint`.

        ``GET /iaas/api/about`` is the single unauthenticated reachability surface;
        reusing the fingerprint result keeps probe + fingerprint pinned to one
        round-trip, the vcd / vcf-fleet delegation shape.
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
        target: Vra8TargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Post-G0.6 callers (``/api/v1/operations/call``, MCP ``call_operation``, the
        CLI verbs) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't reach this
        method. The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``: ``"vcfa-vra8-8.0"`` ->
        (product=``"vcfa"``, version=``"8.0"``, impl_id=``"vcfa-vra8"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vcfa-vra8-connector-shim",
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
    # Typed read ops (#3058, initiative #3056)
    #
    # Thin bound-method shims for the migration-inventory read core. Each delegates
    # to the module-level ``_impl`` body in ``.typed_reads`` (lazy import so pure
    # fingerprint/probe unit tests don't pay the operations package's
    # embedding-pipeline import). The dispatcher resolves each persisted
    # ``module.ClassName.method`` handler_ref back to the bound method and threads
    # ``operator`` / ``target`` / ``params`` by name. All read-only.
    # ------------------------------------------------------------------

    async def project_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.project.list`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_project_list_impl

        return await vra8_project_list_impl(self, operator, target, params)

    async def deployment_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.deployment.list`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_deployment_list_impl

        return await vra8_deployment_list_impl(self, operator, target, params)

    async def deployment_get(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.deployment.get`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_deployment_get_impl

        return await vra8_deployment_get_impl(self, operator, target, params)

    async def blueprint_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.blueprint.list`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_blueprint_list_impl

        return await vra8_blueprint_list_impl(self, operator, target, params)

    async def catalog_item_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.catalog-item.list`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_catalog_item_list_impl

        return await vra8_catalog_item_list_impl(self, operator, target, params)

    async def about(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.about`` shim (#3058)."""
        from meho_backplane.connectors.vra8.typed_reads import vra8_about_impl

        return await vra8_about_impl(self, operator, target, params)

    # ------------------------------------------------------------------
    # Dispatch-path session-recovery hooks (#2067 / #2396)
    # ------------------------------------------------------------------

    async def invalidate_session(self, target: Vra8TargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path (#2067).

        **The load-bearing 401-recovery hook.** On an auth-class status (vRA 8's
        ``401`` from an expired bearer), the dispatcher's data-path recovery arm calls
        this hook — keyed on ``invalidate_session``, *not* ``invalidate_credentials``
        — then re-dispatches the op once
        (:func:`~meho_backplane.operations.dispatcher._retry_after_session_invalidation`).
        Dropping the cached bearer here makes the retry's :meth:`auth_headers`
        cache-miss and re-run the two-step login, so a mid-session token expiry
        self-heals on the next dispatch without a backplane restart — the SDDC Manager
        / NSX / vcd session-minting precedent (a connector *without* this hook falls
        straight through to ``connector_auth_failed`` and wedges on the stale token,
        dispatcher.py #2067). Holds :attr:`_session_lock` so a concurrent
        re-establish doesn't race the eviction.
        """
        await self._evict_session_token(target)

    async def invalidate_credentials(self, target: Vra8TargetLike) -> None:
        """Establish-auth-failure companion hook (#2396).

        The dispatcher calls this on an establish-time
        :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError` (a
        login 401/403). vRA 8 re-reads Vault on every mint, so it keeps no separate
        credential-bytes cache to evict; the only per-target state is the bearer, so
        this also drops it — a no-op when the establish already failed before caching,
        harmless otherwise. Kept so the connector advertises both dispatch-path
        recovery hooks.
        """
        await self._evict_session_token(target)

    async def _evict_session_token(self, target: Vra8TargetLike) -> None:
        """Drop the cached bearer for *target* under the session lock."""
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def aclose(self) -> None:
        """Clear cached session tokens, then tear down the httpx pool.

        No server-side session revoke — the Bearer header is sent per request. The
        token cache is cleared so a post-aclose reuse of the same connector instance
        re-mints cleanly.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await super().aclose()
