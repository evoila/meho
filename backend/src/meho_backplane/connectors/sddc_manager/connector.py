# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SddcManagerConnector — hand-rolled HttpConnector subclass for SDDC Manager 9.0.

Auth + fingerprint + probe + the G0.6 dispatch shim. Ingested operations
dispatch through the same transport + ``auth_headers`` seam.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.sddc_manager.__init__`. The connector *occupies*
the ``(product="sddc", version="9.0", impl_id="sddc-rest")`` triple, so the
boot-time profile stamp (#2288/#2320) no-ops on it — the typed class stays the
resolution winner, preserving the #1750/#1798 product-shadowing invariant while
its session auth is derived from the shipped profile.

Auth — token session derived from the shipped ExecutionProfile
--------------------------------------------------------------

SDDC Manager is **token-only**: it rejects HTTP Basic outright (live 401;
Broadcom KBs 435716/387124/372387). The real flow is a JSON credential login —
``POST /v1/tokens`` with a ``{username, password}`` body — that returns
``accessToken``, sent as ``Authorization: Bearer <accessToken>`` on every
subsequent request.

That flow is the ``session_login_token`` named scheme (#2287). The connector
derives its session-login path, request headers, and session-expiry status set
from the reviewed
:data:`~meho_backplane.connectors.sddc_manager.profile.SDDC_EXECUTION_PROFILE`
via that scheme's
:class:`~meho_backplane.connectors._shared.profile_auth.SessionSchemeSpec`, so
the typed connector and a profile-stamped
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` share one
declaration of the login mechanics rather than two literals that could drift —
the same posture :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
takes against ``VRLI_EXECUTION_PROFILE`` (#1974).

The per-target session token is cached (single-flight under a lock). On a
downstream session-expiry status (:data:`_SESSION_EXPIRED_STATUSES` — ``401``,
the SDDC Manager expired-token signal) the cached token is evicted and the
login re-run once: the helper-level :meth:`_get_json_with_session_retry` covers
the fingerprint/probe round-trip, and the public duck-typed
:meth:`invalidate_session` hook is the seam the generic-ingested dispatch path
calls (#2067) before re-dispatching the op once. Credentials themselves are
loaded once per target from Vault (a ``401`` means the *token* expired, not the
credential, so the credential cache is left intact across an eviction).

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested
mode.

Fingerprint
-----------

``GET /v1/sddc-managers`` returns a pagination envelope
``{"elements": [{id, fqdn, version, domain: {id, name}, ...}], ...}``.
:meth:`fingerprint` reads ``elements[0]`` (SDDC Manager is typically a
singleton appliance). ``version`` carries the full version string (e.g.
``"5.2.0.0-24276214"``); ``build`` is extracted from a separate ``build``
field when present (VCF 9.x may surface it explicitly), otherwise ``None``.
``extras["management_domain"]`` carries the management domain name. The
fingerprint GET authenticates via the token session (SDDC Manager has no
unauthenticated version endpoint), so it goes through
:meth:`_get_json_with_session_retry`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import SESSION_SCHEME_SPECS
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError, vcf_session_login
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.sddc_manager.profile import SDDC_EXECUTION_PROFILE
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    load_credentials_from_vault,
)

# Re-export ``SessionLoginError`` so callers that catch the login helper's
# structured failure don't have to reach into the shared module.
__all__ = ["SddcManagerConnector", "SessionLoginError"]

_log = structlog.get_logger(__name__)

# The ``session_login_token`` scheme spec (#2287), selected by the shipped
# profile. Supplies the login path, request headers, credential-body builder,
# and token extractor — the single declaration the typed connector and a
# profiled SDDC connector both read, so the login mechanics cannot drift.
_SESSION_SPEC = SESSION_SCHEME_SPECS[SDDC_EXECUTION_PROFILE.auth.scheme]

# ``POST /v1/tokens`` — the SDDC Manager token-issue endpoint, sourced from the
# scheme spec's login-path builder rather than a bare literal.
_SESSION_CREATE_PATH = _SESSION_SPEC.login_path(SDDC_EXECUTION_PROFILE.auth)

# Static headers the login POST carries (``Content-Type`` / ``Accept`` JSON).
_SESSION_REQUEST_HEADERS: dict[str, str] = dict(_SESSION_SPEC.request_headers)

# Downstream statuses meaning "the cached session token is no longer accepted;
# re-login and retry once". SDDC Manager signals an expired token with a plain
# ``401`` (no vendor-specific expiry code — the refresh-token leg is an
# initiative Non-goal), so this is the profile's default ``{401}``. Sourced
# from the profile's ``expiry_statuses`` so the helper retry layer and the
# dispatcher's auth-class arm narrow the same closed set from one declaration.
_SESSION_EXPIRED_STATUSES: frozenset[int] = SDDC_EXECUTION_PROFILE.expiry_statuses


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3 targets).
    Any other value is rejected by the caller. Same predicate the NSX and
    vSphere precedents use; lifted into this module to keep connectors
    decoupled.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _sddc_login_body(username: str, password: str) -> dict[str, Any]:
    """Build SDDC Manager's ``{username, password}`` login body.

    Delegates to the ``session_login_token`` scheme spec's body builder so the
    wire-key contract lives in one place (``profile_auth``), shared with a
    profiled SDDC connector. The shared :func:`vcf_session_login` helper's
    ``payload_builder`` contract is ``(username, password) -> dict``; the scheme
    spec's builder reads them out of a secret bundle, so this closure bridges
    the two shapes.
    """
    return dict(
        _SESSION_SPEC.build_body(
            SDDC_EXECUTION_PROFILE.auth,
            {"username": username, "password": password},
        )
    )


def _extract_access_token(resp: httpx.Response) -> str | None:
    """Pull ``accessToken`` out of the SDDC Manager token-response body.

    Delegates to the scheme spec's vetted token extractor so the connector and
    a profiled SDDC connector read the same response field. Returns ``None`` for
    a missing / non-string / empty token (or a non-JSON body); the shared
    :func:`vcf_session_login` helper then raises the consistent target-named
    :class:`SessionLoginError`.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    token = _SESSION_SPEC.extract_token(payload)
    return token.token if token is not None else None


class SddcManagerConnector(HttpConnector):
    """SDDC Manager 9.0 REST connector with profile-derived token-session auth.

    Per-target credentials cached in :attr:`_creds_cache` (loaded once via the
    injectable :class:`SddcCredentialsLoader`); the per-target session token
    minted from them cached in :attr:`_session_tokens`. Auth is the
    ``session_login_token`` scheme derived from
    :data:`~meho_backplane.connectors.sddc_manager.profile.SDDC_EXECUTION_PROFILE`
    — ``POST /v1/tokens`` → ``accessToken`` → ``Authorization: Bearer`` — with
    a single re-login on a downstream ``401`` (SDDC Manager rejects HTTP Basic;
    the token is the only path that dispatches).

    The :attr:`priority` is set to ``1`` so a future ``GenericRestConnector``
    auto-shim that somehow registers for the same triple loses the registry's
    tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"sddc-rest-9.0"`` -> (``"sddc"``, ``"9.0"``, ``"sddc-rest"``).
    product = "sddc"
    version = "9.0"
    impl_id = "sddc-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: SddcCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._credentials_loader: SddcCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <accessToken>"}`` for the request.

        Lazily establishes the session on first call against *target*
        (``POST /v1/tokens``); subsequent calls reuse the cached token. The
        full ``operator`` is threaded into :meth:`_session_token` →
        :meth:`_load_credentials` so the live default loader (G3.10-T1 #945)
        reads the per-target Vault secret under the operator's identity
        (``vault_client_for_operator(operator)``).
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` selects the Vault-sourced
        service account once the loader has resolved it; the operator's JWT
        only authenticates the read, not the SDDC Manager request itself.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"SddcManagerConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: SddcTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, establishing on first use.

        The lock serialises concurrent first-use callers for one target so two
        first-use callers don't both pay the ``POST /v1/tokens`` round-trip.
        Credentials are sourced from :meth:`_load_credentials` (its own lock,
        cache, and fail-closed loader); the login round-trip delegates to the
        shared :func:`vcf_session_login` helper, which wraps a non-2xx or a
        token-less 2xx in :exc:`SessionLoginError` naming the target.

        The cache fast-path is closed to the synthesised system operator
        (``is_system_operator``): a system/operator-less caller always
        re-establishes (its :meth:`_load_credentials` re-runs the loader so the
        fail-closed guard applies), and its minted token is never written to the
        shared cache — it can neither be served a warm token a real operator
        primed nor poison the cache for one (#1008).
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            creds = await self._load_credentials(target, operator)
            client = await self._http_client(target)
            token = await vcf_session_login(
                client,
                _SESSION_CREATE_PATH,
                username=creds["username"],
                password=creds["password"],
                target_name=target.name,
                payload_builder=_sddc_login_body,
                token_extractor=_extract_access_token,
                request_headers=dict(_SESSION_REQUEST_HEADERS),
            )
            if not is_system_operator(operator):
                self._session_tokens[cache_key] = token
            _log.info(
                "sddc_manager_session_established",
                target=target.name,
                host=target.host,
            )
            return token

    async def invalidate_session(self, target: SddcTargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path.

        The seam the generic-ingested dispatch path calls on an auth-class
        status (SDDC Manager's ``401``) before re-dispatching the op once
        (#2067) — the path an ingested sddc op actually traverses. Delegates to
        :meth:`_invalidate_session` so the dispatch-path recovery and the
        helper's internal retry share one eviction implementation.
        """
        await self._invalidate_session(target)

    async def _invalidate_session(self, target: SddcTargetLike) -> None:
        """Drop the cached session token for *target*.

        Called by :meth:`_get_json_with_session_retry` on a session-expiry
        status from a downstream call, and by the public
        :meth:`invalidate_session` dispatch-path hook (#2067), so the
        subsequent :meth:`_session_token` re-issues ``POST /v1/tokens`` from a
        clean state. Holds the lock so a concurrent re-establish doesn't race
        with the invalidation. The credential cache is left intact — a ``401``
        means the *session token* expired, not that the credentials are wrong.
        """
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def _get_json_with_session_retry(
        self,
        target: SddcTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *path* with single session-expiry -> re-login -> retry-once recovery.

        Wraps the inherited :meth:`HttpConnector._get_json` (which carries
        tenacity's connection-error + 5xx retry decorator). On a session-expiry
        status (:data:`_SESSION_EXPIRED_STATUSES` — SDDC Manager's ``401``) from
        the inherited call, invalidates the cached token and retries once: the
        cached token is stale, not the credential, so a re-login recovers it. A
        second session-expiry status raises :exc:`RuntimeError` naming the
        target — re-login once on expiry, not a retry loop. Same shape the
        vRLI / NSX precedents established.
        """
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _SESSION_EXPIRED_STATUSES:
                raise
            await self._invalidate_session(target)
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SESSION_EXPIRED_STATUSES:
                raise RuntimeError(
                    f"sddc-manager session re-login failed for target {target.name!r}: "
                    f"GET {path} returned HTTP {status_code} after refresh"
                ) from exc
            raise

    async def _load_credentials(self, target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same target;
        subsequent calls take the fast path under the same lock. The loaded
        dict must contain ``"username"`` and ``"password"`` keys; missing
        keys raise a :exc:`RuntimeError` naming the target and the missing
        key so operators can identify a misconfigured Vault path.

        ``operator`` is forwarded to the
        :class:`SddcCredentialsLoader` so the default loader can read
        the per-target Vault secret under the operator's identity
        (G3.10-T1's live read). The default loader is the thin
        sddc-manager-specific entry point to the shared
        operator-context Vault read; injected test loaders accept the
        same ``(target, operator)`` pair.

        The cache fast-path is closed to the synthesised system operator
        (``is_system_operator``): a system/operator-less caller always
        runs the loader so its fail-closed guard applies, and can never be
        served warm credentials a real operator primed but it could not
        resolve itself (#1008). Real-operator behaviour is unchanged —
        cold load → cache → reuse.
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            try:
                _ = raw["username"]
                _ = raw["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"sddc-manager credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[cache_key] = raw
            _log.info(
                "sddc_manager_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def fingerprint(
        self,
        target: SddcTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /v1/sddc-managers``.

        Reads ``elements[0]`` from the pagination envelope. The GET
        authenticates via the token session and goes through
        :meth:`_get_json_with_session_retry`, so an idle-expired token is
        recovered with one re-login rather than surfacing as unreachable. On
        transport or status failure (including a re-login that still fails),
        returns a non-reachable :class:`FingerprintResult` whose
        ``extras["error"]`` carries the exception class + message — same pattern
        the NSX and vSphere connectors established.

        ``operator`` (optional) is the request-scoped operator forwarded
        from the probe routes. When provided, the credentials loader
        reads the per-target Vault secret under that identity — the
        same code path the dispatch surface uses. ``None`` falls back
        to a system operator whose placeholder JWT fails closed at the
        live Vault round-trip. G0.16-T4 (#1306) converged probe +
        dispatch on this signature; pre-fix the probe path hard-coded
        the placeholder JWT and surfaced as the v0.8.0 dogfood's
        ``malformed jwt: must have three parts`` finding on
        ``vcf9-sddc``.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json_with_session_retry(
                target, "/v1/sddc-managers", operator=eff_operator
            )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="sddc",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /v1/sddc-managers",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        elements = payload.get("elements") or []
        sddc = elements[0] if elements else {}
        domain = sddc.get("domain") or sddc.get("managementDomain") or {}
        return FingerprintResult(
            vendor="vmware",
            product="sddc",
            version=sddc.get("version"),
            build=sddc.get("build"),
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /v1/sddc-managers",
            extras={
                "id": sddc.get("id"),
                "fqdn": sddc.get("fqdn"),
                "management_domain": domain.get("name"),
                "management_domain_id": domain.get("id"),
            },
        )

    async def probe(self, target: SddcTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` — one authenticated request covers
        both reachability and auth-challenge, same posture the vSphere and
        NSX precedents use.
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

        Mirrors :meth:`NsxConnector.execute`'s shape. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs
        once #618 lands) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"sddc-rest-9.0"`` → (product=``"sddc"``,
        version=``"9.0"``, impl_id=``"sddc-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:sddc-rest-connector-shim",
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
        """Clear cached session tokens + credentials, then tear down the httpx pool.

        No DELETE-revoke is issued for the token — SDDC Manager's session has a
        server-side lifetime and a per-target network call during lifespan
        shutdown is more risk than benefit (same posture the vRLI / NSX
        precedents established). Both caches are cleared so a post-aclose reuse
        of the same connector instance (e.g. a test that builds one connector
        across two contexts) starts clean and secrets don't outlive the
        instance.
        """
        async with self._session_lock:
            self._session_tokens.clear()
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
