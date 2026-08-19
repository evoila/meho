# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SddcVcf5Connector — hand-rolled HttpConnector subclass for VCF 5.x SDDC Manager.

The **legacy dual-impl** for **SDDC Manager 5.x** (the SDDC Manager tier of a VMware
Cloud Foundation 5.x estate) — the migration-source estate evoila brings customers
*off* (initiative `evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_,
task #3059). It is a ``product="sddc"`` **second implementation**
(``impl_id="sddc-vcf5"``, band ``>=5.0,<9.0``), sibling to the modern
:mod:`~meho_backplane.connectors.sddc_manager` (``sddc-rest``, ``>=9.0,<10.0``) — the
resolver picks this legacy impl for a VCF 5.x target by fingerprint and the modern
one for a 9.x target. The bands are **disjoint**, so no re-band and no specificity
tie; the ``("sddc","","")`` wildcard is owned by the **modern** package (the ``fleet``
inversion), so an unfingerprinted ``sddc`` target resolves to modern.

Why a separate impl (not just widen the modern band)
----------------------------------------------------

SDDC Manager's REST *path* surface is stable since VCF 4.0 — the ``/v1/*`` reads here
are the same shapes the modern connector reads. The split is driven by **spec format
+ schema drift**: 5.x publishes only a Swagger 2.0 definition (appliance-served,
non-conformant), while OpenAPI 3.x is a VCF-9.0-net-new deliverable (the
``vmware/vcf-api-specs`` repo is tagged 9.0/9.1 only). So 5.x cannot be operator-
ingested (MEHO's parser is OA3-only, #2090) — hence a **typed** read core (like
``vcd`` / ``vcfa-vra8``) — and its response schemas differ enough by point release
(e.g. ``ipAddress`` deprecated in 5.2) that the 9.x connector cannot faithfully serve
a 5.x target.

Auth — token session (``POST /v1/tokens``)
------------------------------------------

Identical to the modern 9.x flow (token auth is VCF 4.0+): SDDC Manager rejects HTTP
Basic outright and mints a bearer via ``POST /v1/tokens`` with a ``{username,
password}`` JSON body, returning ``accessToken`` (sent as ``Authorization: Bearer
<accessToken>``). The login round-trip reuses the shared
:func:`~meho_backplane.connectors._shared.vcf_auth.vcf_session_login` helper (a
non-2xx / token-less 2xx raises the target-named :exc:`SessionLoginError`), the same
helper the modern ``sddc_manager`` connector uses. The per-target token is cached
under a lock and minted lazily on first use; an empty ``operator.raw_jwt`` fails
closed **before** the cache lookup (a system-initiated call cannot read per-target
vendor credentials). On a downstream ``401`` the dispatcher's session-recovery arm
calls :meth:`invalidate_session` (#2067) and re-dispatches once; the fingerprint,
which the dispatcher does not drive, has its own :meth:`_get_json_with_session_retry`.

Fingerprint
-----------

SDDC Manager has **no unauthenticated version endpoint**, so :meth:`fingerprint`
reads ``GET /v1/sddc-managers`` on the token session (via the session-retry helper)
and takes ``elements[0]``. ``version`` carries the full VCF version string (e.g.
``"5.2.0.0-24276214"``) — a *real* product version (unlike ``vcfa-vra8``'s API-date
label), so a VCF 5.x target fingerprints straight into the ``>=5.0,<9.0`` band and
resolves to this impl without an operator-asserted version. An operator-less probe
(no JWT) fails closed and reports unreachable.

Operations
----------

A curated **7-op typed read core** — the VCF 5.x migration-inventory surface:
domains / clusters / hosts / vCenters / NSX-T clusters / SDDC Manager (inventory
group) and tasks. All read-only, ``safety_level=safe``, ungated; registered as
``source_kind="typed"`` (:mod:`.typed_ops` / :mod:`.typed_reads`) so they dispatch on
a fresh boot with zero catalog ingest. The modern connector's credential-read
(secret-bearing) and network-pool host-commissioning reads are out of scope — this
inventories a migration *source*, it does not operate it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import is_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import (
    SessionLoginError,
    is_acceptable_auth_model,
    vcf_session_login,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.sddc_vcf5.typed_reads import _SDDC_MANAGERS_PATH, _TOKENS_PATH

# Re-export ``SessionLoginError`` so callers that catch the login helper's structured
# failure don't have to reach into the shared module.
__all__ = ["SddcVcf5Connector", "SessionLoginError"]

_log = structlog.get_logger(__name__)

# The static headers the login POST carries (JSON in/out).
_SESSION_REQUEST_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Downstream statuses meaning "the cached session token is no longer accepted;
# re-login and retry once". SDDC Manager signals an expired token with a plain 401.
_SESSION_EXPIRED_STATUSES: frozenset[int] = frozenset({401})

# Reachability/version surface — the same authenticated read exposed as
# ``sddc.vcf5.manager.list``. Named here so the reconcile lane links the probe method.
_SDDC_VCF5_PROBE_METHOD = "GET /v1/sddc-managers (VCF SDDC Manager appliance inventory)"


def _sddc5_login_body(username: str, password: str) -> dict[str, Any]:
    """Build SDDC Manager's ``{username, password}`` login body (the ``/v1/tokens`` shape)."""
    return {"username": username, "password": password}


def _extract_access_token(resp: httpx.Response) -> str | None:
    """Pull ``accessToken`` out of the SDDC Manager token-response body.

    Returns ``None`` for a missing / non-string / empty token (or a non-JSON body);
    the shared :func:`vcf_session_login` helper then raises the consistent
    target-named :exc:`SessionLoginError`.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("accessToken")
    return token if isinstance(token, str) and token else None


class SddcVcf5Connector(HttpConnector):
    """VCF 5.x SDDC Manager REST connector — ``POST /v1/tokens`` token-session auth.

    Per-target minted bearers cached in :attr:`_session_tokens` (keyed on the
    tenant-unique ``(tenant_id, target.id)`` tuple) under :attr:`_session_lock`, so
    two same-named targets in different tenants never share a token
    (evoila/meho#1642). :meth:`auth_headers` returns ``Authorization: Bearer
    <accessToken>``.

    :attr:`priority` is ``1`` — the shim-tie-break convention. The
    ``(product, version, impl_id)`` triple ``("sddc", "5.0", "sddc-vcf5")`` round-trips
    through ``parse_connector_id`` from the ``connector_id`` ``"sddc-vcf5-5.0"``
    (``product`` = first hyphen-segment of ``impl_id`` = ``"sddc"``).
    """

    # G0.6 v2 registry metadata. Legacy dual-impl of product ``sddc``: distinct
    # impl_id ``sddc-vcf5`` and a DISJOINT band ``>=5.0,<9.0`` (modern ``sddc-rest`` is
    # ``>=9.0,<10.0``). Registers only the versioned triple — the modern
    # ``sddc_manager`` package owns the ``("sddc","","")`` wildcard.
    product = "sddc"
    version = "5.0"
    impl_id = "sddc-vcf5"
    supported_version_range = ">=5.0,<9.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: SddcCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._credentials_loader: SddcCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        # Per-target minted-bearer cache, keyed on ``target_cache_key`` so the
        # tenant-isolation invariant holds; a single lock serialises concurrent
        # first-use callers so they don't double-POST /v1/tokens.
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()

    async def auth_headers(self, target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <accessToken>"}``, minting on first use.

        Lazily establishes the session on first call against *target*
        (``POST /v1/tokens``); subsequent calls reuse the cached token. ``operator``
        is threaded to the credential loader so the first-use Vault read runs under
        the operator's identity.

        Raises :exc:`NotImplementedError` for any ``target.auth_model`` other than
        ``shared_service_account`` / ``None`` — the shared VCF-family gate.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"SddcVcf5Connector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: SddcTargetLike, operator: Operator) -> str:
        """Return the cached session token, establishing it (``POST /v1/tokens``) on first use.

        ``operator.raw_jwt`` must be non-empty: the credential read runs under the
        operator's Vault identity, and a system-initiated call (empty JWT) cannot
        resolve per-target vendor credentials. The guard is enforced **before** the
        cache lookup so a token primed by an authenticated caller can never be handed
        to an unauthenticated one — the defense-in-depth cache-scoping contract the
        VCF-family connectors established.

        The cache fast-path + write are additionally closed to the synthesised system
        operator (``is_system_operator``): a system/operator-less caller can neither be
        served a warm token a real operator primed nor poison the cache for one (#1008)
        — the modern ``sddc_manager`` posture. Today this is belt-and-suspenders (the
        empty-``raw_jwt`` guard already rejects the operator-less fingerprint's
        placeholder before this point); it guards a future refactor that threads a
        non-empty system-operator placeholder through instead.
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
            if cached is not None and not is_system_operator(operator):
                return cached
            creds = await self._credentials_loader(target, operator)
            try:
                username = creds["username"]
                password = creds["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"sddc-vcf5 credentials loader for target {target.name!r} returned a "
                    f"dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            client = await self._http_client(target)
            token = await vcf_session_login(
                client,
                _TOKENS_PATH,
                username=username,
                password=password,
                target_name=target.name,
                payload_builder=_sddc5_login_body,
                token_extractor=_extract_access_token,
                request_headers=dict(_SESSION_REQUEST_HEADERS),
                request_extensions=self._request_extensions(target),
            )
            if not is_system_operator(operator):
                self._session_tokens[cache_key] = token
            _log.info("sddc_vcf5_session_established", target=target.name, host=target.host)
            return token

    async def _get_json_with_session_retry(
        self,
        target: SddcTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *path* with single session-expiry → re-login → retry-once recovery.

        Wraps the inherited :meth:`HttpConnector._get_json` (which carries tenacity's
        connection-error + 5xx retry). On a ``401`` from the inherited call,
        invalidates the cached token and retries once — the cached token is stale, not
        the credential, so a re-login recovers it. A second ``401`` raises
        :exc:`RuntimeError`. Serves the fingerprint/probe path, which the dispatcher
        does not drive (the data path recovers via the :meth:`invalidate_session`
        dispatch hook instead); the modern ``sddc_manager`` posture.
        """
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _SESSION_EXPIRED_STATUSES:
                raise
            await self._evict_session_token(target)
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SESSION_EXPIRED_STATUSES:
                raise RuntimeError(
                    f"sddc-vcf5 session re-login failed for target {target.name!r}: "
                    f"GET {path} returned HTTP {status_code} after refresh"
                ) from exc
            raise

    async def fingerprint(
        self,
        target: SddcTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /v1/sddc-managers`` (authenticated).

        Reads ``elements[0]`` from the pagination envelope on the token session (SDDC
        Manager has no unauthenticated version endpoint), via
        :meth:`_get_json_with_session_retry` so an idle-expired token is recovered
        with one re-login rather than surfacing as unreachable. ``version`` carries
        the full VCF version string (e.g. ``"5.2.0.0-24276214"``) — a real product
        version, so a VCF 5.x target resolves to this impl by fingerprint alone.
        ``build`` is taken from a ``build`` field when present. On transport / status
        / credential failure (including an operator-less probe, whose empty JWT fails
        closed) returns a non-reachable result whose ``extras["error"]`` names the
        exception.

        ``operator`` (optional) is the request-scoped operator forwarded from the
        probe routes; when ``None`` an empty-JWT placeholder is used, which fails
        closed at the credential read (an operator-less probe cannot authenticate).
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else _placeholder_operator()
        try:
            payload = await self._get_json_with_session_retry(
                target, _SDDC_MANAGERS_PATH, operator=eff_operator
            )
        except (httpx.HTTPError, OSError, RuntimeError, VaultCredentialsReadError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="sddc",
                reachable=False,
                probed_at=probed_at,
                probe_method=_SDDC_VCF5_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        elements = payload.get("elements") or []
        sddc = elements[0] if isinstance(elements, list) and elements else {}
        domain = sddc.get("domain") or sddc.get("managementDomain") or {}
        return FingerprintResult(
            vendor="vmware",
            product="sddc",
            version=sddc.get("version"),
            build=sddc.get("build"),
            reachable=True,
            probed_at=probed_at,
            probe_method=_SDDC_VCF5_PROBE_METHOD,
            extras={
                "id": sddc.get("id"),
                "fqdn": sddc.get("fqdn"),
                "management_domain": domain.get("name") if isinstance(domain, dict) else None,
                "product_lineage": "vmware-cloud-foundation",
                "api_surface": "sddc-manager-vcf5",
            },
        )

    async def probe(self, target: SddcTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check — delegates to :meth:`fingerprint`.

        One authenticated request covers both reachability and auth-challenge, the
        modern ``sddc_manager`` / NSX posture.
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
        target: SddcTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Post-G0.6 callers construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The connector's natural
        key is encoded as the dispatcher's ``connector_id`` per ``parse_connector_id``:
        ``"sddc-vcf5-5.0"`` → (product=``"sddc"``, version=``"5.0"``,
        impl_id=``"sddc-vcf5"``).
        """
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:sddc-vcf5-connector-shim",
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
    # Typed read ops (#3059, initiative #3056)
    #
    # Thin bound-method shims for the migration-inventory read core. Each delegates to
    # the module-level ``_impl`` body in ``.typed_reads`` (lazy import). Reads issue a
    # plain ``self._get_json`` on the token session; a raw 401 propagates to the
    # dispatcher's #2067 recovery arm, which calls ``invalidate_session`` and
    # re-dispatches once. All read-only.
    # ------------------------------------------------------------------

    async def domain_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.domain.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_domain_list_impl

        return await sddc5_domain_list_impl(self, operator, target, params)

    async def cluster_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.cluster.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_cluster_list_impl

        return await sddc5_cluster_list_impl(self, operator, target, params)

    async def host_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.host.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_host_list_impl

        return await sddc5_host_list_impl(self, operator, target, params)

    async def vcenter_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.vcenter.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_vcenter_list_impl

        return await sddc5_vcenter_list_impl(self, operator, target, params)

    async def nsxt_cluster_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.nsxt_cluster.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_nsxt_cluster_list_impl

        return await sddc5_nsxt_cluster_list_impl(self, operator, target, params)

    async def manager_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.manager.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_manager_list_impl

        return await sddc5_manager_list_impl(self, operator, target, params)

    async def task_list(
        self, operator: Operator, target: SddcTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``sddc.vcf5.task.list`` shim (#3059)."""
        from meho_backplane.connectors.sddc_vcf5.typed_reads import sddc5_task_list_impl

        return await sddc5_task_list_impl(self, operator, target, params)

    # ------------------------------------------------------------------
    # Dispatch-path session-recovery hooks (#2067 / #2396)
    # ------------------------------------------------------------------

    async def invalidate_session(self, target: SddcTargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path (#2067).

        **The load-bearing 401-recovery hook.** On an auth-class status (SDDC
        Manager's ``401`` from an expired token), the dispatcher's data-path recovery
        arm calls this hook — keyed on ``invalidate_session``, *not*
        ``invalidate_credentials`` — then re-dispatches the op once. Dropping the
        cached token here makes the retry's :meth:`auth_headers` cache-miss and
        re-mint via ``POST /v1/tokens``, so a mid-session token expiry self-heals on
        the next dispatch without a backplane restart — the modern ``sddc_manager`` /
        NSX precedent (a connector *without* this hook wedges on the stale token).
        """
        await self._evict_session_token(target)

    async def invalidate_credentials(self, target: SddcTargetLike) -> None:
        """Establish-auth-failure companion hook (#2396).

        The dispatcher calls this on an establish-time login failure. This connector
        re-reads Vault on every mint, so it keeps no separate credential-bytes cache;
        the only per-target state is the token, so this also drops it. Kept so the
        connector advertises both dispatch-path recovery hooks.
        """
        await self._evict_session_token(target)

    async def _evict_session_token(self, target: SddcTargetLike) -> None:
        """Drop the cached session token for *target* under the session lock."""
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def aclose(self) -> None:
        """Clear cached session tokens, then tear down the httpx pool.

        No DELETE-revoke — SDDC Manager's session has a server-side lifetime and a
        per-target network call during lifespan shutdown is more risk than benefit
        (the modern ``sddc_manager`` posture). The cache is cleared so a post-aclose
        reuse of the same instance re-mints cleanly.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await super().aclose()


def _placeholder_operator() -> Operator:
    """An empty-JWT operator for an operator-less fingerprint — fails closed at the read.

    The probe routes pass a real operator (G0.16-T4); when they do not, this
    placeholder's empty ``raw_jwt`` trips the fail-closed guard in
    :meth:`SddcVcf5Connector._session_token`, so an operator-less fingerprint reports
    unreachable rather than authenticating as no-one.
    """
    return Operator(
        sub="system:sddc-vcf5-fingerprint",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )
