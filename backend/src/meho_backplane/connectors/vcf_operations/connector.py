# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VcfOperationsConnector — hand-rolled HttpConnector subclass for vROps 9.0.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in G3.6-T2 (#833) via G0.7 spec ingestion against the vROps
``/suite-api`` OpenAPI spec into the ``endpoint_descriptor`` table.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vcf_operations.__init__`. The G0.7 auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`)
no-ops on subsequent ingests against the same
``(product="vrops", version="9.0", impl_id="vrops-rest")`` triple.

Auth
----

vROps' ``/suite-api/api/*`` surface accepts HTTP Basic on every request — no
session cookie or token is established. The connector caches the raw
service-account credentials (loaded once per target from Vault via an
injectable loader) and computes the ``Authorization: Basic`` header on each
:meth:`auth_headers` call.

Optional ``auth-source`` routing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

vROps can federate identity through multiple sources (the local realm,
``vIDM``, an Active Directory realm name, etc.). When ``target.auth_source``
is set, the connector appends ``?auth-source=<value>`` as a query parameter
on every authenticated request so vROps can route the Basic-auth challenge
to the named identity domain. When ``target.auth_source`` is ``None`` (the
default), the query parameter is omitted and vROps falls back to its local
realm. The value is passed through verbatim.

The auth-source query parameter rides alongside any caller-supplied params
through an :meth:`_request_json` override that merges the auth-supplied
mapping into the caller-supplied one. Authenticated requests therefore
always carry ``?auth-source=...`` when configured, regardless of which
operation handler issued them; unauthenticated transport (none in the
skeleton, since :meth:`fingerprint` and :meth:`probe` both go through
:meth:`_get_json` with Basic auth attached) is unaffected.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested
mode. Lifted from
:func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
so all G3.6 skeletons enforce the same gate identically.

No 401-retry-once wrapper
-------------------------

vROps Basic auth is stateless — there is no session token to refresh and no
``acquire`` round-trip to re-run on a 401. A 401 from the appliance always
means "bad credentials" (or a misconfigured auth-source); retrying with the
same credentials would not help, and retrying with different credentials is
outside the connector's contract. The shared
:class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
exposes :meth:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache.invalidate`
for a future rotation-event admin endpoint to call between the rotation and
the next dispatch; that path is the right place to drop the cache, not a
transport-layer retry loop.

Contrast vRLI (#830) and Fleet (#831): vRLI establishes a session and uses
the shared :func:`~meho_backplane.connectors._shared.vcf_auth.vcf_session_login`
helper, with the 401-retry-once loop in the consumer connector around its
downstream calls. vROps doesn't need that — same reason Harbor doesn't.

Fingerprint
-----------

``GET /suite-api/api/versions/current`` returns ``{"releaseName": "...",
"buildNumber": ...}`` shaped JSON. The connector lifts ``releaseName`` into
:attr:`FingerprintResult.version` and ``buildNumber`` into
:attr:`FingerprintResult.build`. Extras carry ``humanlyReadableReleaseName``
when the appliance returns it (some 9.0 builds do, some don't) for
operator-visible audit display.

The version endpoint is unauthenticated on vROps; the connector still sends
Basic auth on the call because (a) the appliance ignores unsolicited auth
headers on unauthenticated paths, (b) keeping a single
``_request_json``-shaped transport path simplifies auditing, and (c) the
Harbor / SDDC Manager / NSX precedents all do the same. The behaviour is
identical with or without ``target.auth_source`` set.

Probe
-----

Delegates to :meth:`fingerprint` — same endpoint, same predicate
(``reachable=True`` ⇒ ``ok=True``). vROps does not expose a dedicated
``/health`` endpoint distinct from the version surface; the SDDC Manager
and NSX precedents established the "probe delegates to fingerprint" shape
for this case. Harbor's purpose-built ``/api/v2.0/health`` is the
exception, not the rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

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
from meho_backplane.connectors.vcf_operations.session import (
    VcfOperationsCredentialsLoader,
    VcfOperationsTargetLike,
    load_credentials_from_vault,
)

__all__ = ["VcfOperationsConnector"]

_log = structlog.get_logger(__name__)

#: Spec-relative paths the typed read ops (#2303) hit on the connector's
#: HTTP Basic (+ optional auth-source) session.
_VERSIONS_CURRENT_PATH = "/suite-api/api/versions/current"
_ALERTS_PATH = "/suite-api/api/alerts"
_RESOURCES_QUERY_PATH = "/suite-api/api/resources/query"


class VcfOperationsConnector(HttpConnector):
    """vROps 9.0 REST connector with HTTP Basic auth (+ optional ``auth-source``).

    Per-target credentials cached in :attr:`_creds` (loaded once via the
    injectable :class:`VcfOperationsCredentialsLoader`). HTTP Basic auth is
    sent on every request via ``Authorization: Basic <b64>`` — no session
    token is established and no 401-driven re-login is needed (see the module
    docstring's "No 401-retry-once wrapper" section).

    The :attr:`priority` is set to ``1`` so a future ``GenericRestConnector``
    auto-shim that somehow registers for the same triple loses the registry's
    tie-break ladder.
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
        self._creds = CredentialsCache(
            credentials_loader if credentials_loader is not None else load_credentials_from_vault,
            product_label="vrops",
        )

    async def auth_headers(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target*, caches
        them (via the shared :class:`CredentialsCache`), and reuses the cached
        values on subsequent calls. The full ``operator`` is threaded into
        the loader so the live default
        (:func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`)
        reads the per-target KV-v2 secret under the operator's Vault
        Identity entity via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator` —
        the locked Option A decision. An injected test loader receives
        the same ``(target, operator)`` pair.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``. Same predicate as
        Harbor / NSX / SDDC Manager — all G3.6 skeletons share it via
        :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`.

        The ``auth-source`` query parameter is **not** part of this method's
        return value — query parameters are merged in :meth:`_request_json`
        via :meth:`_auth_query_params`. Keeping headers and query-params on
        separate seams matches httpx's own API surface
        (``client.request(..., headers=..., params=...)``).
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VcfOperationsConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._creds.get(target, operator)
        return {"Authorization": basic_auth_header(creds["username"], creds["password"])}

    def _auth_query_params(self, target: VcfOperationsTargetLike) -> dict[str, str]:
        """Return the auth-source query-parameter mapping for *target*.

        ``{"auth-source": target.auth_source}`` when ``target.auth_source`` is
        a non-empty string, ``{}`` otherwise. Empty strings are treated as
        unset — vROps rejects an empty ``?auth-source=`` and the silent-omit
        behaviour is the friendlier default for an operator with a partially
        populated target row.
        """
        auth_source = getattr(target, "auth_source", None)
        if not auth_source:
            return {}
        return {"auth-source": auth_source}

    async def _request_json(
        self,
        target: VcfOperationsTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Merge the auth-source query param into *params* before the base call.

        The base :meth:`HttpConnector._request_json` carries the
        :mod:`tenacity` retry decorator; overriding it here keeps the
        decorator intact while threading the connector-specific query-param
        contribution through every call site (including the indirect ones
        via :meth:`_get_json`). Caller-supplied params win on key conflict —
        an operation handler that explicitly sets ``auth-source`` overrides
        the per-target default; nothing today exercises that path, but the
        ordering documents the intended precedence. ``operator`` is forwarded
        to the base method (and thence :meth:`auth_headers`) unchanged.
        """
        merged_params = dict(self._auth_query_params(target))
        if params:
            merged_params.update(params)
        # An empty mapping is identical-to-None for httpx's params merge, but
        # ``None`` keeps tests' ``request.url.params`` assertions clean when
        # auth-source is unset and no caller params are supplied.
        final_params = merged_params or None
        return await super()._request_json(
            target,
            method,
            path,
            operator=operator,
            params=final_params,
            json=json,
            extra_headers=extra_headers,
        )

    async def _post_json(
        self,
        target: VcfOperationsTargetLike,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Thread the auth-source (and caller) query params onto a non-idempotent request.

        The base :meth:`HttpConnector._post_json` neither routes through
        :meth:`_request_json` (so the auth-source merge there is bypassed)
        nor accepts a ``params`` mapping. vROps still needs
        ``?auth-source=<value>`` on **every** authenticated request when the
        target federates identity, so this override merges the per-target
        auth-source contribution (:meth:`_auth_query_params`) with any
        caller-supplied ``params`` and encodes them onto the URL before
        delegating to the base implementation. Caller params win on a key
        clash — the same precedence the :meth:`_request_json` override
        documents. ``doseq=True`` so a list value (a repeated query param)
        serialises to repeated ``key=a&key=b`` pairs rather than a bracketed
        string.
        """
        merged: dict[str, Any] = dict(self._auth_query_params(target))
        if params:
            merged.update({key: value for key, value in params.items() if value is not None})
        if merged:
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}{urlencode(merged, doseq=True)}"
        return await super()._post_json(
            target,
            path,
            operator=operator,
            verb=verb,
            json=json,
            data=data,
            extra_headers=extra_headers,
        )

    async def fingerprint(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /suite-api/api/versions/current``.

        The response payload's ``releaseName`` becomes ``version`` and
        ``buildNumber`` becomes ``build``. ``extras`` carries
        ``humanlyReadableReleaseName`` when present (some 9.0 builds emit it).

        On transport or status failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message — same pattern Harbor / SDDC Manager / NSX
        established for transport-failure fingerprinting.

        ``operator`` (optional, G0.16-T4 #1306) is forwarded to the
        credentials loader so the per-target Vault read happens under
        the operator's identity, matching the dispatch path. ``None``
        falls back to a system operator whose placeholder JWT fails
        closed at the live Vault round-trip.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json(
                target, "/suite-api/api/versions/current", operator=eff_operator
            )
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
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs
        once #837 lands) construct a real :class:`Operator` and call
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
    # Each handler is a thin read directly on the connector's HTTP Basic
    # (+ optional auth-source) session — no dispatch_child, no ingested
    # descriptor — so the op works on a fresh boot with zero catalog
    # ingest (the #2262 invariant). The dispatcher binds these bound
    # methods to the per-process connector instance and threads
    # ``operator`` / ``target`` / ``params`` by name (see
    # :func:`~meho_backplane.operations._branches.dispatch_typed`). The op
    # metadata + registrar live in
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
        as an agent-callable typed op. The auth-source query param is merged
        by the :meth:`_request_json` override.
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
        query params (auth-source merged by the :meth:`_request_json`
        override). ``resourceId`` is a list — httpx serialises it to repeated
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
        params. The auth-source query param is merged by the
        :meth:`_post_json` override (the base ``_post_json`` bypasses the
        ``_request_json`` auth-source seam).
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
        return await self._post_json(
            target,
            _RESOURCES_QUERY_PATH,
            operator=operator,
            json=body,
            params=query or None,
        )

    async def aclose(self) -> None:
        """Clear cached credentials, then tear down the httpx pool.

        No server-side session to revoke — HTTP Basic is stateless. The
        credential cache is cleared so a post-aclose reuse of the same
        connector instance (e.g. a test that builds one connector across two
        contexts) starts clean. Mirrors Harbor's ``aclose`` shape — the
        shared :class:`CredentialsCache.clear` does the locked-mutation under
        the hood so concurrent in-flight ``get(t)`` calls can't sneak a stale
        entry past the clear.
        """
        await self._creds.clear()
        await super().aclose()
