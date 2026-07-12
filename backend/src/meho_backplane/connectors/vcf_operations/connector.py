# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcfOperationsConnector — hand-rolled HttpConnector subclass for vROps 9.0.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vcf_operations.__init__`. The G0.7 auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`)
no-ops on subsequent ingests against the same
``(product="vrops", version="9.0", impl_id="vrops-rest")`` triple.

Auth — acquired-token session (``OpsToken``)
--------------------------------------------

VCF Operations 9.0.2 rejects stateless HTTP Basic on ``/suite-api/api/*``
(the earlier skeleton's "Basic on every request" design assumption was false
against a live appliance — #2395). The connector establishes a session token
and presents it on every request:

* **Acquire**: ``POST /suite-api/api/auth/token/acquire`` with a JSON body
  ``{"username", "password"}`` (plus ``"authSource"`` when the target
  federates identity — see below). ``Content-Type`` /
  ``Accept: application/json``. The 200 response carries
  ``{"token", "validity", "expiresAt", "roles"}``; the connector extracts
  ``token``.
* **Present**: ``Authorization: OpsToken <token>`` on every authenticated
  request. The 9.x-native scheme is preferred over the legacy
  ``vRealizeOpsToken`` alias — the connector pins ``>=9.0,<10.0`` (every
  supported appliance is VCF Operations 9.x where ``OpsToken`` is
  product-native). Neither ``Basic`` nor ``Bearer`` is accepted by the
  appliance.

The token is cached per target under the tenant-unique
``(tenant_id, target.id)`` key and reused until it is evicted by
:meth:`invalidate_session` (the dispatcher's #2067 seam) — the appliance
extends the token's six-hour validity on each call, so a re-acquire happens
only after an idle expiry or a rotation.

The login round-trip delegates to the shared
:func:`~meho_backplane.connectors._shared.vcf_auth.vcf_session_login` helper —
the same helper vRLI (#830) uses — so the token-session shape is declared
once. A 401/403 at acquire wraps into a
:class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError` the
dispatcher maps to ``connector_auth_failed`` (cause
``session_establish_401`` / ``session_establish_403``).

Optional ``authSource`` federation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

vROps can federate identity through multiple sources (the local realm,
``vIDM``, an Active Directory realm name, etc.). When ``target.auth_source``
is set, it rides the **acquire body** as ``"authSource"`` — its token-era
home. When ``target.auth_source`` is ``None`` (or an empty string), the field
is omitted and vROps authenticates against its default local realm. The value
is passed through verbatim. (The pre-token skeleton rode this as a
``?auth-source=`` query parameter on every request; that mechanism is gone —
the appliance reads ``authSource`` only at ``token/acquire``.)

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested
mode. Lifted from
:func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
so all the VCF management-plane connectors enforce the same gate identically.

Session-expiry recovery (the #2067 seam)
----------------------------------------

The connector advertises a duck-typed
:meth:`invalidate_session` hook. On an auth-class status (401) from a
dispatched op, the generic-ingested dispatch path evicts the cached token via
this hook and re-dispatches the op exactly once (G0.29-T2 #2067) — so an
idle-expired session re-acquires there rather than failing until restart. A
second auth failure (the re-acquire also failed) falls through to
``connector_auth_failed``. The typed connector carries no
:class:`~meho_backplane.connectors.profile.ExecutionProfile`, so the
dispatcher classifies its 401 against the typed-connector global
:data:`~meho_backplane.operations._errors._AUTH_FAILED_STATUSES`.

Fingerprint
-----------

``GET /suite-api/api/versions/current`` returns ``{"releaseName": "...",
"buildNumber": ...}`` shaped JSON. The connector lifts ``releaseName`` into
:attr:`FingerprintResult.version` and ``buildNumber`` into
:attr:`FingerprintResult.build`. Extras carry ``humanlyReadableReleaseName``
when the appliance returns it (some 9.0 builds do, some don't) for
operator-visible audit display. The call rides the connector's ``OpsToken``
session like every other read; a credential / session failure surfaces as a
non-reachable :class:`FingerprintResult` whose ``extras["error"]`` carries the
exception class + message.

Probe
-----

Delegates to :meth:`fingerprint` — same endpoint, same predicate
(``reachable=True`` ⇒ ``ok=True``). vROps does not expose a dedicated
``/health`` endpoint distinct from the version surface; the SDDC Manager
and NSX precedents established the "probe delegates to fingerprint" shape
for this case.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import (
    CredentialsCache,
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
from meho_backplane.connectors.vcf_operations.session import (
    VcfOperationsCredentialsLoader,
    VcfOperationsTargetLike,
    load_credentials_from_vault,
)

# Re-export ``SessionLoginError`` so callers that catch the helper's
# structured failure don't have to reach into the shared module.
__all__ = ["SessionLoginError", "VcfOperationsConnector"]

_log = structlog.get_logger(__name__)

#: Session-establish endpoint. POST with JSON body ``{username, password}``
#: (plus ``authSource`` when the target federates identity); a 200 carries
#: ``{"token", "validity", "expiresAt", "roles"}``. Verified against the VCF
#: Operations 9.0 API reference (``POST /suite-api/api/auth/token/acquire``).
_SESSION_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"

#: The 9.x-native authorization scheme for a suite-api token. Preferred over
#: the legacy ``vRealizeOpsToken`` alias — the connector pins ``>=9.0,<10.0``
#: where ``OpsToken`` is product-native (#2395). Not ``Bearer``, not ``Basic``.
_OPS_TOKEN_SCHEME = "OpsToken"

#: Spec-relative paths the typed read ops (#2303) hit on the connector's
#: ``OpsToken`` session.
_VERSIONS_CURRENT_PATH = "/suite-api/api/versions/current"
_ALERTS_PATH = "/suite-api/api/alerts"
_RESOURCES_QUERY_PATH = "/suite-api/api/resources/query"


def _vrops_acquire_payload_builder(
    auth_source: str | None,
) -> Callable[[str, str], dict[str, Any]]:
    """Return a payload-builder closure binding *auth_source* into the acquire body.

    The shared :func:`vcf_session_login` helper's
    :data:`~meho_backplane.connectors._shared.vcf_auth.SessionPayloadBuilder`
    is ``(username, password) -> dict``; vROps adds an optional ``authSource``
    (its token-era home), so we close over the per-target value. A falsy
    ``auth_source`` (``None`` or an empty string) omits the field — vROps
    then authenticates against its default local realm.
    """

    def _build(username: str, password: str) -> dict[str, Any]:
        body: dict[str, Any] = {"username": username, "password": password}
        if auth_source:
            body["authSource"] = auth_source
        return body

    return _build


def _extract_ops_token(resp: httpx.Response) -> str | None:
    """Pull ``token`` out of the ``token/acquire`` response body.

    The 200 response is ``{"token": "<t>", "validity": <epoch-ms>,
    "expiresAt": "<human>", "roles": [...]}``; the connector consumes only
    ``token``. Returns ``None`` for the missing / null / empty / malformed
    case and lets :func:`vcf_session_login` raise the consistent
    target-named :exc:`SessionLoginError`.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("token")
    if not isinstance(value, str) or not value:
        return None
    return value


class VcfOperationsConnector(HttpConnector):
    """vROps 9.0 REST connector with acquired-token (``OpsToken``) auth.

    Per-target session token cached in :attr:`_session_tokens`; per-target
    credentials cached in :attr:`_creds` (the shared :class:`CredentialsCache`
    instance). The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the same
    triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"vrops-rest-9.0"`` -> (``"vrops"``, ``"9.0"``, ``"vrops-rest"``).
    product = "vrops"
    version = "9.0"
    impl_id = "vrops-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: VcfOperationsCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._creds = CredentialsCache(
            credentials_loader if credentials_loader is not None else load_credentials_from_vault,
            product_label="vrops",
        )

    async def auth_headers(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        """Return ``{"Authorization": "OpsToken <token>"}`` for the request.

        Lazily acquires the session token on first call against *target*
        (``POST /suite-api/api/auth/token/acquire``); subsequent calls reuse
        the cached token. The full ``operator`` is threaded into
        :meth:`_session_token` so the live credentials loader (the default
        :func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`)
        reads the per-target KV-v2 secret under the operator's Vault Identity
        entity via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator` — the
        locked Option A decision. An injected test loader receives the same
        ``(target, operator)`` pair.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``. Same predicate as
        the other VCF management-plane connectors via
        :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcfOperationsConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"{_OPS_TOKEN_SCHEME} {token}"}

    async def _session_token(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator,
    ) -> str:
        """Return the cached session token for *target*, acquiring on first use.

        The lock serialises concurrent first-use callers for one target; the
        cache fast-path means subsequent callers are bounded only by the lock
        acquisition. The slow ``POST /suite-api/api/auth/token/acquire`` call
        runs under the lock so two concurrent first-use callers against the
        same target don't both pay the round-trip.

        Credentials come from the shared :class:`CredentialsCache` (which
        raises :exc:`RuntimeError` naming the target if the loader returns a
        dict missing ``"username"``/``"password"``). The login round-trip
        delegates to the shared :func:`vcf_session_login` helper, which wraps
        a non-2xx into :exc:`SessionLoginError` (401/403 into the structured
        :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`)
        and chains the underlying :exc:`httpx.HTTPStatusError`.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty — a defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard and the sibling
        check in :meth:`CredentialsCache.get`, raised before the cache lookup
        so a token primed by an authenticated caller cannot leak to a
        system-initiated caller via a cache hit.
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
            creds = await self._creds.get(target, operator)
            auth_source = getattr(target, "auth_source", None) or None
            client = await self._http_client(target)
            token = await vcf_session_login(
                client,
                _SESSION_ACQUIRE_PATH,
                username=creds["username"],
                password=creds["password"],
                target_name=target.name,
                payload_builder=_vrops_acquire_payload_builder(auth_source),
                token_extractor=_extract_ops_token,
                request_headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            self._session_tokens[cache_key] = token
            _log.info(
                "vrops_session_established",
                target=target.name,
                host=target.host,
                auth_source=auth_source,
            )
            return token

    async def invalidate_session(self, target: VcfOperationsTargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path.

        The seam the generic-ingested dispatch path calls on an auth-class
        status (vROps' 401) before re-dispatching the op once (G0.29-T2
        #2067). Delegates to :meth:`_invalidate_session` so the dispatch-path
        recovery has a single eviction implementation. A connector with no
        session state would expose no such hook and never be retried; vROps
        now does, so an idle-expired token re-acquires there.
        """
        await self._invalidate_session(target)

    async def _invalidate_session(self, target: VcfOperationsTargetLike) -> None:
        """Drop the cached session token for *target*.

        Called by the public :meth:`invalidate_session` dispatch-path hook
        (#2067) so the subsequent :meth:`_session_token` re-issues
        ``POST /suite-api/api/auth/token/acquire`` from a clean state. Holds
        the lock so a concurrent re-acquire doesn't race the invalidation.
        The credentials cache is left intact — a 401 means the *session token*
        expired or was rejected, not that the credentials are wrong.
        """
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def fingerprint(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /suite-api/api/versions/current``.

        The response payload's ``releaseName`` becomes ``version`` and
        ``buildNumber`` becomes ``build``. ``extras`` carries
        ``humanlyReadableReleaseName`` when present (some 9.0 builds emit it).

        On transport, session-establish, or status failure, returns a
        non-reachable :class:`FingerprintResult` whose ``extras["error"]``
        carries the exception class + message — same pattern Harbor / SDDC
        Manager / NSX established for transport-failure fingerprinting.

        ``operator`` (optional, G0.16-T4 #1306) is forwarded to the session
        establish so the per-target Vault read happens under the operator's
        identity, matching the dispatch path. ``None`` falls back to a system
        operator whose placeholder JWT fails closed at the live Vault
        round-trip.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json(target, _VERSIONS_CURRENT_PATH, operator=eff_operator)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="vrops",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /suite-api/api/versions/current",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product="vrops",
            version=payload.get("releaseName") or None,
            build=str(payload["buildNumber"]) if payload.get("buildNumber") is not None else None,
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /suite-api/api/versions/current",
            extras={
                "humanly_readable_release_name": payload.get("humanlyReadableReleaseName"),
            },
        )

    async def probe(self, target: VcfOperationsTargetLike) -> ProbeResult:
        """Reachability check — delegates to :meth:`fingerprint`.

        vROps does not expose a dedicated ``/health`` endpoint distinct from
        the version surface, so the fingerprint call is the right reachability
        probe. Reuses the fingerprint's try/except shape: ``reachable=True``
        ⇒ ``ok=True``; ``reachable=False`` ⇒ ``ok=False`` with the same
        ``extras["error"]`` string surfaced as the probe's ``reason``.

        Same shape SDDC Manager and NSX use; Harbor is the exception with
        its purpose-built ``/api/v2.0/health`` endpoint.
        """
        probed_at = datetime.now(UTC)
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=probed_at)
        # ``extras["error"]`` is populated on every unreachable fingerprint
        # result (see ``fingerprint`` above). Fall back to a generic string
        # only as defence-in-depth.
        reason = fp.extras.get("error") if fp.extras else None
        return ProbeResult(
            ok=False,
            reason=str(reason) if reason else "vcf-operations fingerprint failed",
            probed_at=probed_at,
        )

    async def execute(
        self,
        target: VcfOperationsTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`HarborConnector.execute`'s shape. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs)
        construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"vrops-rest-9.0"`` → (product=``"vrops"``,
        version=``"9.0"``, impl_id=``"vrops-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vcf-operations-connector-shim",
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
    # Typed read ops (Initiative #2266 T3, #2303)
    #
    # Each handler is a thin read directly on the connector's ``OpsToken``
    # session — no dispatch_child, no ingested descriptor — so the op works
    # on a fresh boot with zero catalog ingest (the #2262 invariant). The
    # dispatcher binds these bound methods to the per-process connector
    # instance and threads ``operator`` / ``target`` / ``params`` by name
    # (see :func:`~meho_backplane.operations._branches.dispatch_typed`). The
    # op metadata + registrar live in
    # :mod:`meho_backplane.connectors.vcf_operations.typed_ops`.
    # ------------------------------------------------------------------

    async def liveness(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.liveness`` — ``GET /suite-api/api/versions/current``.

        Reachability + identity probe. Returns the appliance's
        ``releaseName`` / ``buildNumber`` (and ``humanlyReadableReleaseName``
        when present) — the same surface :meth:`fingerprint` reads, exposed
        as an agent-callable typed op on the connector's ``OpsToken`` session.
        """
        del params  # schema declares the param object empty
        return await self._get_json(target, _VERSIONS_CURRENT_PATH, operator=operator)

    async def alert_list(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.alert.list`` — ``GET /suite-api/api/alerts``.

        Optional ``activeOnly`` / ``alertCriticality`` / ``alertStatus`` /
        ``resourceId`` filters and ``page`` / ``pageSize`` pagination ride as
        query params on the connector's ``OpsToken`` session. ``resourceId``
        is a list — httpx serialises it to repeated
        ``resourceId=a&resourceId=b`` pairs.
        """
        query: dict[str, Any] = {}
        for key in ("activeOnly", "alertCriticality", "alertStatus", "page", "pageSize"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        resource_ids = [rid for rid in (params.get("resourceId") or []) if isinstance(rid, str)]
        if resource_ids:
            query["resourceId"] = resource_ids
        return await self._get_json(target, _ALERTS_PATH, operator=operator, params=query or None)

    async def resource_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.resource.query`` — ``POST /suite-api/api/resources/query``.

        A body-shaped POST: the ``ResourceQuerySpec`` fields
        (:data:`~meho_backplane.connectors.vcf_operations.typed_ops.VROPS_RESOURCE_QUERY_BODY_FIELDS`)
        form the JSON request body; ``page`` / ``pageSize`` ride as query
        params encoded onto the path (the base ``_post_json`` takes no
        ``params`` mapping). The request rides the connector's ``OpsToken``
        session via :meth:`auth_headers`. ``doseq=True`` so a repeated value
        serialises to ``key=a&key=b`` rather than a bracketed string.
        """
        from meho_backplane.connectors.vcf_operations.typed_ops import (
            VROPS_RESOURCE_QUERY_BODY_FIELDS,
        )

        body: dict[str, Any] = {}
        for key in VROPS_RESOURCE_QUERY_BODY_FIELDS:
            value = params.get(key)
            if value is not None:
                body[key] = value
        query: dict[str, Any] = {}
        for key in ("page", "pageSize"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        path = _RESOURCES_QUERY_PATH
        if query:
            path = f"{path}?{urlencode(query, doseq=True)}"
        return await self._post_json(target, path, operator=operator, json=body)

    async def aclose(self) -> None:
        """Clear cached session tokens + credentials, then tear down the httpx pool.

        No server-side revoke is issued — the vROps token idle-expires on the
        appliance, and a per-target network call during lifespan shutdown is
        more risk than benefit (same posture NSX / vRLI take). The token cache
        is cleared so a post-aclose reuse of the same connector instance
        starts clean; the credentials cache is cleared so secrets don't
        outlive the connector instance. The shared
        :class:`CredentialsCache.clear` does the locked-mutation under the
        hood so concurrent in-flight ``get(t)`` calls can't sneak a stale
        entry past the clear.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        await self._creds.clear()
        await super().aclose()
