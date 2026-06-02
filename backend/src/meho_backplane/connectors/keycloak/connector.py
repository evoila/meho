# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing module size (>600 lines before
# this change); #1474 adds a small token-error-detail helper for a bugfix, so
# splitting the connector module is out of scope here.

"""KeycloakConnector -- HttpConnector subclass for the Keycloak 26.x Admin REST API.

G3.13-T1 (#1393) shipped the substrate -- admin credential loader +
fingerprint + dual registration. G3.13-T2 (#1394) layers the six curated
read ops onto that surface (realm/client/client-scope/user/role-mapping);
onboarding docs (T3) and the approval-gated write surface (T4) follow.

The load-bearing design point -- admin-vs-operator credential split
=====================================================================

MEHO is its own IdP: the backplane authenticates its callers with
operator-OIDC tokens that Keycloak issues. The connector that *manages*
that Keycloak must not authenticate through the same path, or it could
never bootstrap a freshly deployed Keycloak whose operator-login clients
aren't configured yet (the chicken-and-egg the issue body calls out).

So the connector authenticates to the Keycloak **Admin REST API** with a
**separate admin credential** loaded from Vault (consumer path
``secret/rdc-hetzner-dc/keycloak/admin``) via
:func:`~meho_backplane.connectors.keycloak.session.load_admin_credentials_from_vault`.
It mints an admin access token through Keycloak's own token endpoint
(``POST /realms/{admin_realm}/protocol/openid-connect/token``) and caches
it with a TTL-driven refresh. The operator's OIDC token
(``operator.raw_jwt``) authorises the *Vault read* (operator-context
KV-v2, the locked Option A decision) but is **never** sent to Keycloak.
The split is asserted in
``tests/test_connectors_keycloak_auth.py`` -- a test proves the operator
token does not appear on any admin call.

Auth model gating
=================

The connector locks to :attr:`~meho_backplane.connectors.schemas.AuthModel.SHARED_SERVICE_ACCOUNT`
(or ``None`` for pre-G0.3 targets), mirroring the VCF / Harbor / gh-rest
precedents: the admin credential is a shared service account, not a
per-operator identity. :meth:`auth_headers` rejects any other value with
a clear :exc:`NotImplementedError` naming the target and the requested
mode.

Token lifecycle
===============

The admin token is cached per target with a refresh margin
(:data:`_TOKEN_REFRESH_MARGIN_SECONDS`) subtracted from the
``expires_in`` Keycloak returns, so a near-expiry token is re-minted
before it fails a downstream call rather than after. :meth:`aclose`
clears the token cache and tears down the httpx pool but issues no
logout-revoke -- the access token is short-lived and a logout round-trip
during lifespan shutdown is more risk than benefit (same posture the
NSX / vRLI precedents established).

Operations
==========

The six T2 read ops live in
:mod:`~meho_backplane.connectors.keycloak.ops_read` (metadata +
handler logic); the connector exposes a thin bound-method shim per op
and walks ``READ_OPS`` in :meth:`register_operations`. Each op dispatches
via :meth:`auth_headers` (the admin-token Bearer) and scrubs secret
material from its result. The G0.6 dispatch shim :meth:`execute` routes
``op_id`` through the operator-aware dispatcher.

References
----------

* Issue: https://github.com/evoila/meho/issues/1393
* Parent initiative: https://github.com/evoila/meho/issues/1388
* Token endpoint + client_credentials grant:
  https://www.keycloak.org/securing-apps/oidc-layers
* Realm representation (GET /admin/realms/{realm}):
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/idm/RealmRepresentation.html
* ServerInfo (GET /admin/serverinfo, systemInfo.version):
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/info/ServerInfoRepresentation.html
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import is_acceptable_auth_model
from meho_backplane.connectors.adapters.http import HttpConnector, _retryable
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakAdminCredentialsLoader,
    KeycloakClientCredentials,
    KeycloakTargetLike,
    RealmConfig,
    load_admin_credentials_from_vault,
    resolve_realm_config,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["KeycloakAdminTokenError", "KeycloakConnector", "KeycloakWriteResult"]

_log = structlog.get_logger(__name__)

#: Seconds shaved off the token's ``expires_in`` so a near-expiry token
#: is re-minted before a downstream call would fail on it. A 30 s margin
#: comfortably covers clock skew + the round-trip of the admin call the
#: token is fetched for.
_TOKEN_REFRESH_MARGIN_SECONDS: float = 30.0

#: Fallback TTL when Keycloak's token response omits / malforms
#: ``expires_in``. Keycloak's admin access-token lifespan defaults to
#: 60 s; we use that as a conservative floor so a malformed response
#: doesn't pin a token forever.
_DEFAULT_TOKEN_TTL_SECONDS: float = 60.0

#: Mutating verbs ``_write_admin`` accepts. Kept distinct from the
#: inherited ``_IDEMPOTENT_METHODS`` so a write never rides the
#: idempotent-GET retry decorator (re-firing a side effect on a transient
#: 5xx is the bug that guard prevents).
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT"})


@dataclass(frozen=True)
class KeycloakWriteResult:
    """Outcome of an admin-auth mutating request.

    ``status_code`` is the HTTP status of the write; ``location`` is the
    ``Location`` header Keycloak returns on a create (its trailing segment
    is the new object's UUID); ``conflict`` is ``True`` when the write hit
    an already-exists 409 that :meth:`KeycloakConnector._write_admin`
    swallowed for idempotency.
    """

    status_code: int
    location: str | None
    conflict: bool

    def created_uuid(self) -> str | None:
        """Return the new object's UUID parsed from the ``Location`` header.

        Keycloak create endpoints respond ``201`` with
        ``Location: .../{resource}/{uuid}``; the UUID is the final
        non-empty path segment. Returns ``None`` when there is no
        ``Location`` (e.g. an idempotent 409, or an update that returns
        ``204`` with no body).
        """
        if not self.location:
            return None
        segment = self.location.rstrip("/").rsplit("/", 1)[-1]
        return segment or None


class KeycloakAdminTokenError(RuntimeError):
    """The admin token endpoint round-trip failed.

    Raised when ``POST /realms/{admin_realm}/protocol/openid-connect/token``
    returns a non-2xx response or a body without a usable
    ``access_token``. The message names the target and the admin realm;
    it never echoes a credential value. On a non-2xx with an OAuth2 error
    body it also echoes Keycloak's ``{error, error_description}`` (the two
    non-secret keys that name the failure class -- bad secret vs.
    client-not-allowed-the-grant vs. wrong realm) via
    :func:`_upstream_token_error_detail`, so the operator can tell those
    apart without backplane logs. Chains the underlying
    :exc:`httpx.HTTPStatusError` when one is available so the operator
    can see the upstream status code.
    """


def _token_request_body(creds: KeycloakAdminCredentials) -> dict[str, str]:
    """Render the form body for the token request from *creds*.

    ``client_credentials`` grant for :class:`KeycloakClientCredentials`;
    ``password`` grant (direct access) for
    :class:`KeycloakPasswordCredentials`. The body is form-encoded
    (``application/x-www-form-urlencoded``) -- Keycloak's token endpoint
    does not accept JSON.
    """
    if isinstance(creds, KeycloakClientCredentials):
        return {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
    # KeycloakPasswordCredentials -- direct-access-grant password flow.
    return {
        "grant_type": "password",
        "client_id": creds.client_id,
        "username": creds.username,
        "password": creds.password,
    }


def _parse_token_response(payload: Any) -> tuple[str, float]:
    """Extract ``(access_token, ttl_seconds)`` from a token response body.

    Returns the access token and the effective TTL (``expires_in`` minus
    the refresh margin, floored at 1 s). Falls back to
    :data:`_DEFAULT_TOKEN_TTL_SECONDS` when ``expires_in`` is missing or
    not a number. Raises :exc:`KeycloakAdminTokenError` when the body
    carries no usable ``access_token`` -- caller adds the target context.
    """
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise KeycloakAdminTokenError("token response carried no usable 'access_token' field")
    expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
    ttl = float(expires_in) if isinstance(expires_in, (int, float)) else _DEFAULT_TOKEN_TTL_SECONDS
    effective = max(1.0, ttl - _TOKEN_REFRESH_MARGIN_SECONDS)
    return token, effective


#: Cap on the upstream ``error_description`` echoed into
#: :exc:`KeycloakAdminTokenError`. Keycloak's descriptions are short
#: human-readable strings; the cap guards against a pathological body
#: bloating the exception message / log line.
_MAX_ERROR_DESCRIPTION_CHARS: int = 200

#: The RFC 6749 §5.2 token-endpoint error codes. ``_upstream_token_error_detail``
#: echoes an ``error_description`` only when the body is an
#: ``application/json`` object whose ``error`` is one of these -- i.e. an actual
#: OAuth2 token error. ``error_description`` is non-secret *only under the OAuth2
#: schema*, so an unrelated gateway/proxy JSON envelope that happens to carry an
#: ``error`` key must not have its fields echoed (the no-secret-in-logs
#: invariant). An unrecognised code degrades safely to the bare status message.
_OAUTH2_TOKEN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)


def _upstream_token_error_detail(response: httpx.Response) -> str:
    """Render Keycloak's ``{error, error_description}`` as an operator hint.

    Keycloak's token endpoint returns an OAuth2 error body (RFC 6749 §5.2)
    on a 4xx -- e.g. ``{"error": "unauthorized_client", "error_description":
    "Invalid client or Invalid client credentials"}``. Those two fields name
    the *class* of failure (bad secret vs. client-not-allowed-the-grant vs.
    wrong realm) and are **not** secret-bearing -- echoing them turns a bare
    ``returned HTTP 401`` into a one-look diagnosis.

    Returns a leading-space-prefixed ``" (error=..., error_description=...)"``
    fragment ready to append to the message, or ``""`` unless the body is a
    genuine OAuth2 token error: an ``application/json`` object whose ``error``
    is one of :data:`_OAUTH2_TOKEN_ERROR_CODES`. That gate keeps a non-OAuth2
    gateway/proxy envelope (whose ``error_description`` is *not* schema-bound to
    be non-secret) from injecting noise or leaking a value -- the
    no-secret-in-logs invariant. The ``error_description`` is length-capped; no
    other body field is ever echoed, keeping the surface to OAuth2's two
    well-known non-secret keys.
    """
    if "application/json" not in response.headers.get("content-type", "").lower():
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, str) or error not in _OAUTH2_TOKEN_ERROR_CODES:
        return ""
    parts = [f"error={error!r}"]
    description = body.get("error_description")
    if isinstance(description, str) and description:
        truncated = description[:_MAX_ERROR_DESCRIPTION_CHARS]
        if len(description) > _MAX_ERROR_DESCRIPTION_CHARS:
            truncated += "…"
        parts.append(f"error_description={truncated!r}")
    return f" ({', '.join(parts)})"


class _CachedToken:
    """An admin access token plus the monotonic clock time it expires at."""

    __slots__ = ("expires_at", "token")

    def __init__(self, token: str, expires_at: float) -> None:
        self.token = token
        self.expires_at = expires_at

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at


class KeycloakConnector(HttpConnector):
    """Keycloak 26.x Admin REST API connector with admin-token Bearer auth.

    Registry v2 triple: ``("keycloak", "26.x", "keycloak-admin")``. The
    ``26.x`` version targets the Keycloak 26 release series; a future
    ``("keycloak", "27.x", ...)`` entry can ship alongside without
    disturbing 26.x targets.

    Per-target admin token cached in :attr:`_admin_tokens` with a
    TTL-driven refresh; per-target admin credentials are loaded fresh on
    each token mint (the secret read is cheap relative to the token
    round-trip and avoids holding the credential in memory between
    refreshes). The :attr:`priority` is ``1`` so a future generic-REST
    auto-shim that somehow registers for the same triple loses the
    resolver tie-break.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"keycloak-admin-26.x"`` -> ("keycloak", "26.x", "keycloak-admin").
    product = "keycloak"
    version = "26.x"
    impl_id = "keycloak-admin"
    supported_version_range = ">=26.0,<27.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: KeycloakAdminCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._admin_tokens: dict[str, _CachedToken] = {}
        self._token_lock = asyncio.Lock()
        self._credentials_loader: KeycloakAdminCredentialsLoader = (
            credentials_loader
            if credentials_loader is not None
            else load_admin_credentials_from_vault
        )

    # -- auth -----------------------------------------------------------

    async def auth_headers(self, target: KeycloakTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <admin_token>"}`` for the request.

        Lazily mints the admin token on first call against *target* and
        on TTL expiry; otherwise reuses the cached token. The admin token
        is obtained via the **admin** credential path -- a Vault-sourced
        service-account / admin credential exchanged at Keycloak's token
        endpoint -- and is deliberately distinct from the operator-OIDC
        token. ``operator.raw_jwt`` authorises only the operator-context
        Vault read of the admin credential; it is never sent to Keycloak.

        Raises :exc:`NotImplementedError` (naming the target + requested
        mode) when ``target.auth_model`` is anything other than
        ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"KeycloakConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._admin_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _admin_token(self, target: KeycloakTargetLike, operator: Operator) -> str:
        """Return the cached admin token for *target*, minting on first use / expiry.

        Fail-closed against an empty ``operator.raw_jwt`` *before* the
        cache lookup -- a system-initiated caller (empty JWT) cannot read
        the admin credential out of Vault, so it must never receive a
        token primed by an authenticated caller. This mirrors the
        defense-in-depth guard the VCF / gh credential caches enforce
        (the loader's ``vault_client_for_operator`` chain is the primary
        gate; this is the cache fast-path's enforcement of the same
        invariant).

        The lock serialises concurrent first-use / refresh callers for
        one target; the slow token round-trip runs under the lock so two
        concurrent callers don't both pay it.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read the Keycloak admin credential)"
            )
        async with self._token_lock:
            now = time.monotonic()
            cached = self._admin_tokens.get(target.name)
            if cached is not None and cached.is_fresh(now):
                return cached.token
            token, ttl = await self._mint_admin_token(target, operator)
            self._admin_tokens[target.name] = _CachedToken(token, now + ttl)
            return token

    async def _mint_admin_token(
        self, target: KeycloakTargetLike, operator: Operator
    ) -> tuple[str, float]:
        """Load the admin credential + exchange it at the token endpoint.

        Returns ``(access_token, effective_ttl_seconds)``. The credential
        read is operator-context (the operator's JWT -> Vault JWT/OIDC);
        the token POST is form-encoded against
        ``/realms/{admin_realm}/protocol/openid-connect/token`` with NO
        ``Authorization`` header (the grant carries the credentials in
        the body). A non-2xx response or a missing ``access_token``
        raises :exc:`KeycloakAdminTokenError` naming the target + admin
        realm.
        """
        realms = resolve_realm_config(target)
        creds = await self._credentials_loader(target, operator)
        client = await self._http_client(target)
        token_path = f"/realms/{realms.admin_realm}/protocol/openid-connect/token"
        try:
            resp = await client.post(
                token_path,
                data=_token_request_body(creds),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPStatusError as exc:
            raise KeycloakAdminTokenError(
                f"admin token request for target {target.name!r} against admin realm "
                f"{realms.admin_realm!r} returned HTTP {exc.response.status_code}"
                f"{_upstream_token_error_detail(exc.response)}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise KeycloakAdminTokenError(
                f"admin token request for target {target.name!r} against admin realm "
                f"{realms.admin_realm!r} failed: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            token, ttl = _parse_token_response(payload)
        except KeycloakAdminTokenError as exc:
            raise KeycloakAdminTokenError(
                f"admin token request for target {target.name!r} against admin realm "
                f"{realms.admin_realm!r}: {exc}"
            ) from exc

        _log.info(
            "keycloak_admin_token_minted",
            target=target.name,
            host=getattr(target, "host", None),
            admin_realm=realms.admin_realm,
            grant=(
                "client_credentials" if isinstance(creds, KeycloakClientCredentials) else "password"
            ),
            ttl_seconds=ttl,
        )
        return token, ttl

    # -- fingerprint / probe -------------------------------------------

    async def fingerprint(
        self,
        target: KeycloakTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Authenticate via the admin path and round-trip ``GET /admin/realms/{realm}``.

        Mints an admin token (admin credential path, **not** operator-OIDC),
        then GETs the managed realm's top-level representation. The
        returned :class:`~meho_backplane.connectors.schemas.FingerprintResult`
        carries the realm metadata (``realm``, ``enabled``, ``sslRequired``,
        ``loginTheme``) under ``extras`` plus, best-effort, the Keycloak
        server version from ``GET /admin/serverinfo`` (``systemInfo.version``).
        The serverinfo call is non-fatal: an older Keycloak that 404s the
        undocumented endpoint still produces a reachable fingerprint with
        ``version=None``.

        Unlike the unauthenticated-version-probe connectors (vRLI), every
        Keycloak admin endpoint requires the admin token, so the
        fingerprint needs a real ``operator`` to read the admin credential
        from Vault. A ``None`` operator (background caller) falls back to
        the synthesised system operator, which fails closed at the live
        Vault round-trip -- surfaced here as ``reachable=False`` +
        ``extras["error"]`` rather than an unhandled exception.

        Transport / auth failure → ``reachable=False`` with
        ``extras["error"]`` carrying ``"<ExcType>: <message>"``.
        """
        from meho_backplane.connectors._shared.system_operator import (
            synthesise_system_operator,
        )

        probed_at = datetime.now(UTC)
        realms = resolve_realm_config(target)
        probe_method = f"GET /admin/realms/{realms.managed_realm}"
        effective_operator = operator if operator is not None else synthesise_system_operator()

        try:
            realm_repr = await self._get_json(
                target,
                f"/admin/realms/{realms.managed_realm}",
                operator=effective_operator,
            )
        except (
            httpx.HTTPError,
            OSError,
            ValueError,
            KeycloakAdminTokenError,
            VaultCredentialsReadError,
        ) as exc:
            return FingerprintResult(
                vendor="keycloak",
                product="keycloak",
                reachable=False,
                probed_at=probed_at,
                probe_method=probe_method,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        server_version = await self._server_version(target, effective_operator)

        return FingerprintResult(
            vendor="keycloak",
            product="keycloak",
            version=server_version,
            reachable=True,
            probed_at=probed_at,
            probe_method=probe_method,
            extras=_realm_extras(realm_repr, realms),
        )

    async def _server_version(self, target: KeycloakTargetLike, operator: Operator) -> str | None:
        """Best-effort Keycloak server version from ``GET /admin/serverinfo``.

        ``/admin/serverinfo`` is the (undocumented but stable) endpoint
        the admin console's "Server Info" page reads; ``systemInfo.version``
        carries the Keycloak version (e.g. ``"26.0.5"``). Any failure
        (404 on an older server, transport error, malformed body) returns
        ``None`` rather than failing the fingerprint -- the realm
        round-trip is the canonical reachability signal, the version is a
        nice-to-have.
        """
        try:
            info = await self._get_json(target, "/admin/serverinfo", operator=operator)
        except (httpx.HTTPError, OSError, ValueError, KeycloakAdminTokenError):
            return None
        system_info = info.get("systemInfo") if isinstance(info, dict) else None
        version = system_info.get("version") if isinstance(system_info, dict) else None
        return version if isinstance(version, str) and version else None

    async def probe(self, target: KeycloakTargetLike) -> ProbeResult:
        """Reachability + admin-auth check.

        Delegates to :meth:`fingerprint` with the synthesised system
        operator -- one admin round-trip covers both "is Keycloak up" and
        "do the admin credentials work". A failed Vault read / token mint
        surfaces as ``ok=False`` with the error reason, matching the
        NSX / vRLI precedents. Because every Keycloak admin endpoint is
        authenticated, ``ok=True`` implies the admin credential is valid,
        not merely that the socket is open.
        """
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error", "unreachable")),
            probed_at=fp.probed_at,
        )

    # -- admin-auth JSON GET helpers ------------------------------------

    async def _get_admin_json(
        self,
        target: KeycloakTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried admin-auth GET for an **object** response.

        Thin alias over the inherited
        :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._get_json`
        so the read-op handlers read uniformly against object endpoints
        (``GET /admin/realms/{realm}``, one client, role-mappings). The
        admin Bearer is applied by this connector's
        :meth:`auth_headers`; the operator authorises only the Vault read
        behind the token mint.
        """
        return await self._get_json(target, path, operator=operator, params=params)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    async def _get_admin_list(
        self,
        target: KeycloakTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Retried admin-auth GET for an **array** response.

        Several Keycloak Admin REST list endpoints (``/clients``,
        ``/client-scopes``, ``/users``) return a JSON **array**, not an
        object, so the inherited :meth:`_get_json` (typed ``dict``) is the
        wrong shape. This helper mirrors its retry / timeout / auth
        contract — same idempotent-GET backoff as
        :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._request_json`
        — but returns the parsed list. A non-list body (an error envelope
        Keycloak occasionally returns as an object) yields an empty list
        so a handler never iterates a dict.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.request("GET", path, params=params, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    # -- admin-auth write helpers (G3.13-T4 #1406) ----------------------

    async def _write_admin(
        self,
        target: KeycloakTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
        idempotent_conflict: bool = True,
    ) -> KeycloakWriteResult:
        """Issue an admin-auth mutating request (POST/PUT) — never retried.

        Returns a :class:`KeycloakWriteResult` carrying the HTTP status,
        the ``Location`` header (Keycloak's create endpoints return the
        new object's URL there — the trailing path segment is the new
        object's UUID), and a flag recording whether the request hit an
        already-exists conflict.

        Idempotency contract (issue acceptance criterion): when
        *idempotent_conflict* is ``True`` (the default) an HTTP **409**
        is swallowed and surfaced as ``conflict=True`` rather than raised,
        so a re-run of a create is a no-op-equivalent success rather than
        an error. Every other non-2xx status raises
        :exc:`httpx.HTTPStatusError` (the dispatcher's ``connector_error``
        branch records it). Mutating verbs are **not** retried — a 5xx on a
        non-idempotent write must surface, not silently re-fire the side
        effect.

        The admin Bearer is applied by :meth:`auth_headers`; the operator
        authorises only the Vault read behind the token mint and is never
        sent to Keycloak.
        """
        verb = method.upper()
        if verb not in _MUTATING_METHODS:
            raise ValueError(
                f"_write_admin only accepts mutating methods {sorted(_MUTATING_METHODS)}; "
                f"got {verb!r}"
            )
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.request(verb, path, json=json, headers=headers)
        if idempotent_conflict and resp.status_code == 409:
            return KeycloakWriteResult(status_code=409, location=None, conflict=True)
        resp.raise_for_status()
        return KeycloakWriteResult(
            status_code=resp.status_code,
            location=resp.headers.get("location"),
            conflict=False,
        )

    async def _find_client_uuid(
        self,
        target: KeycloakTargetLike,
        managed_realm: str,
        client_id: str,
        *,
        operator: Operator,
    ) -> str | None:
        """Resolve a client's internal UUID from its human ``clientId``.

        Keycloak addresses clients by an internal UUID ``id``, never the
        human ``clientId`` — so every client write keys on the UUID, which
        is discovered via ``GET .../clients?clientId=<clientId>`` (exact
        match). Returns the first matching row's ``id`` or ``None`` when no
        client carries that ``clientId``.
        """
        rows = await self._get_admin_list(
            target,
            f"/admin/realms/{managed_realm}/clients",
            operator=operator,
            params={"clientId": client_id},
        )
        for row in rows:
            if isinstance(row, dict) and row.get("clientId") == client_id:
                uuid_value = row.get("id")
                if isinstance(uuid_value, str) and uuid_value:
                    return uuid_value
        return None

    async def _find_user_uuid(
        self,
        target: KeycloakTargetLike,
        managed_realm: str,
        username: str,
        *,
        operator: Operator,
    ) -> str | None:
        """Resolve a user's internal UUID from their ``username``.

        Keycloak's ``GET .../users?username=<u>`` filter is a substring
        match by default, so the rows are re-filtered to the **exact**
        username (case-insensitive, matching Keycloak's own username
        casing rules) before returning a UUID. Returns ``None`` when no
        exact match exists.
        """
        rows = await self._get_admin_list(
            target,
            f"/admin/realms/{managed_realm}/users",
            operator=operator,
            params={"username": username, "exact": "true"},
        )
        wanted = username.casefold()
        for row in rows:
            if isinstance(row, dict) and str(row.get("username", "")).casefold() == wanted:
                uuid_value = row.get("id")
                if isinstance(uuid_value, str) and uuid_value:
                    return uuid_value
        return None

    async def _find_realm_role(
        self,
        target: KeycloakTargetLike,
        managed_realm: str,
        role_name: str,
        *,
        operator: Operator,
    ) -> dict[str, Any] | None:
        """Resolve a realm role's full representation by name.

        The role-mapping assign endpoint takes the full RoleRepresentation
        (``{id, name, ...}``) in its body, so the assign handler must fetch
        it first via ``GET .../roles/{role-name}``. Returns ``None`` when
        the role does not exist (a 404), so the handler can surface a clean
        operator-actionable error rather than a raw transport failure.
        """
        try:
            role = await self._get_admin_json(
                target,
                f"/admin/realms/{managed_realm}/roles/{role_name}",
                operator=operator,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return role if isinstance(role, dict) else None

    # -- typed-op handler shims (G3.13-T2 #1394) ------------------------

    async def realm_get(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.realm.get`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_realm_get

        return await keycloak_realm_get(self, operator, target, params)

    async def client_list(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.client.list`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_client_list

        return await keycloak_client_list(self, operator, target, params)

    async def client_get(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.client.get`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_client_get

        return await keycloak_client_get(self, operator, target, params)

    async def client_scope_list(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.client_scope.list`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_client_scope_list

        return await keycloak_client_scope_list(self, operator, target, params)

    async def user_list(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.user.list`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_user_list

        return await keycloak_user_list(self, operator, target, params)

    async def role_mapping_get(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for the ``keycloak.role_mapping.get`` op (G3.13-T2 #1394)."""
        from meho_backplane.connectors.keycloak.ops_read import keycloak_role_mapping_get

        return await keycloak_role_mapping_get(self, operator, target, params)

    # -- typed-op write handler shims (G3.13-T4 #1406) ------------------

    async def realm_create(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.realm.create`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_realm_create

        return await keycloak_realm_create(self, operator, target, params)

    async def realm_update(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.realm.update`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_realm_update

        return await keycloak_realm_update(self, operator, target, params)

    async def client_create(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.client.create`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_client_create

        return await keycloak_client_create(self, operator, target, params)

    async def client_update(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.client.update`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_client_update

        return await keycloak_client_update(self, operator, target, params)

    async def client_scope_create(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.client_scope.create`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_client_scope_create

        return await keycloak_client_scope_create(self, operator, target, params)

    async def protocol_mapper_create(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.protocol_mapper.create`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_protocol_mapper_create

        return await keycloak_protocol_mapper_create(self, operator, target, params)

    async def user_create(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.user.create`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_user_create

        return await keycloak_user_create(self, operator, target, params)

    async def user_reset_password(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.user.reset_password`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_user_reset_password

        return await keycloak_user_reset_password(self, operator, target, params)

    async def role_mapping_assign(
        self, operator: Operator, target: KeycloakTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound-method shim for ``keycloak.role_mapping.assign`` (G3.13-T4 #1406)."""
        from meho_backplane.connectors.keycloak.ops_write import keycloak_role_mapping_assign

        return await keycloak_role_mapping_assign(self, operator, target, params)

    # -- typed-op registrar (G3.13-T2 #1394 fills the read-op walk) ------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`READ_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.keycloak.ops_read.READ_OPS` and
        routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`,
        which derives ``handler_ref`` from the bound method's
        ``__module__`` + ``__qualname__``, inserts on first call, and
        skips the embedding compute on re-call with unchanged
        summary / description / tags. Idempotent across pod restarts --
        mirrors the bind9 / pfSense
        :meth:`register_operations` shape.

        G3.13-T2 (#1394) lands the six read ops
        (``keycloak.realm.get`` / ``client.list`` / ``client.get`` /
        ``client_scope.list`` / ``user.list`` / ``role_mapping.get``).
        G3.13-T4 (#1406) layers the approval-gated write ops
        (``realm.create`` / ``realm.update`` / ``client.create`` /
        ``client.update`` / ``client_scope.create`` /
        ``protocol_mapper.create`` / ``user.create`` /
        ``user.reset_password`` / ``role_mapping.assign``) onto the same
        walk — every write registers ``requires_approval=True``.
        """
        # Lazy imports: the operations package pulls in the embedding
        # pipeline (ONNX runtime + a 100 MB+ model on first touch) that a
        # pure-fingerprint / pure-probe unit test should not pay. Lifespan
        # callers already have the embedding service warmed by the time
        # this runs.
        from meho_backplane.connectors.keycloak.ops_read import (
            READ_OPS,
            WHEN_TO_USE_BY_GROUP,
        )
        from meho_backplane.connectors.keycloak.ops_write import (
            WHEN_TO_USE_WRITE_BY_GROUP,
            WRITE_OPS,
        )
        from meho_backplane.operations.typed_register import register_typed_operation

        # The two when_to_use maps are disjoint by group_key suffix
        # (read groups are bare nouns; write groups carry a ``_write``
        # suffix) so a merge never clobbers a read blurb with a write one.
        when_to_use_by_group = {**WHEN_TO_USE_BY_GROUP, **WHEN_TO_USE_WRITE_BY_GROUP}

        bindings: list[tuple[Any, Any]] = []
        for op in (*READ_OPS, *WRITE_OPS):
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"KeycloakConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = when_to_use_by_group.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"KeycloakConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry to "
                        f"WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.keycloak.ops_read so "
                        f"list_operation_groups surfaces a real selection "
                        f"signal instead of the auto-derive template."
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
            "keycloak_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    # -- dispatcher shim ------------------------------------------------

    async def execute(
        self,
        target: KeycloakTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatcher shim -- delegates to the G0.6 operator-aware dispatch path.

        Mirrors :meth:`VcfLogsConnector.execute`. The connector ships no
        ops in T1, so any ``op_id`` resolves to ``unknown_op`` at the
        dispatcher; the shim exists for ABC compatibility and so T2's ops
        are reachable without touching this method. Synthesises a minimal
        system :class:`~meho_backplane.auth.operator.Operator`; the
        connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``
        (``"keycloak-admin-26.x"`` -> ("keycloak", "26.x",
        "keycloak-admin")).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator as _Operator
        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = _Operator(
            sub="system:keycloak-admin-connector-shim",
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
        """Clear cached admin tokens then tear down the httpx pool.

        No logout-revoke is issued -- the admin access token is
        short-lived (the refresh margin keeps it fresh, expiry retires it)
        and a per-target logout call during lifespan shutdown is more risk
        than benefit (same posture the NSX / vRLI precedents established).
        The token cache is cleared so a post-aclose reuse of the same
        connector instance starts clean.
        """
        async with self._token_lock:
            self._admin_tokens.clear()
        await super().aclose()


def _realm_extras(realm_repr: Any, realms: RealmConfig) -> dict[str, Any]:
    """Project the realm representation into the fingerprint ``extras`` bag.

    Pulls the operator-meaningful top-level fields
    (``realm``/``enabled``/``sslRequired``/``loginTheme``) out of the
    ``GET /admin/realms/{realm}`` body and records the resolved
    admin-realm / managed-realm pair so an operator reading the
    fingerprint can see which realm was probed and where the admin client
    authenticated. Tolerates a non-dict body defensively.
    """
    repr_dict = realm_repr if isinstance(realm_repr, dict) else {}
    return {
        "realm": repr_dict.get("realm"),
        "enabled": repr_dict.get("enabled"),
        "ssl_required": repr_dict.get("sslRequired"),
        "login_theme": repr_dict.get("loginTheme"),
        "admin_realm": realms.admin_realm,
        "managed_realm": realms.managed_realm,
    }
