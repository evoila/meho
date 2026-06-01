# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KeycloakConnector -- HttpConnector subclass for the Keycloak 26.x Admin REST API.

G3.13-T1 (#1393) skeleton -- admin credential loader + fingerprint +
dual registration. Read ops (T2) and onboarding docs (T3) layer on top;
this module ships **zero** typed operations.

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

This module ships zero operations -- the G0.6 dispatch shim
:meth:`execute` exists for ABC compatibility and the
:meth:`register_operations` classmethod is the seam T2 fills in. Until
then the connector is registered + discoverable but ``execute`` against
any ``op_id`` resolves to ``unknown_op`` at the dispatcher.

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
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import is_acceptable_auth_model
from meho_backplane.connectors.adapters.http import HttpConnector
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

__all__ = ["KeycloakAdminTokenError", "KeycloakConnector"]

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


class KeycloakAdminTokenError(RuntimeError):
    """The admin token endpoint round-trip failed.

    Raised when ``POST /realms/{admin_realm}/protocol/openid-connect/token``
    returns a non-2xx response or a body without a usable
    ``access_token``. The message names the target and the admin realm;
    it never echoes a credential value. Chains the underlying
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

    # -- typed-op registrar seam (T2 fills this in) ---------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Typed-op registrar seam -- ships **no** ops in T1 (G3.13-T1 #1393).

        T1 is the substrate: the connector class, the admin credential
        loader, fingerprint, and dual registration. Read ops land in T2,
        which walks a ``KEYCLOAK_OPS`` table here exactly as
        :meth:`~meho_backplane.connectors.pfsense.connector.PfSenseConnector.register_operations`
        walks ``PFSENSE_OPS``. Wiring the no-op registrar now means the
        lifespan's ``run_typed_op_registrars`` call already drives
        Keycloak, so T2 only fills the op walk -- the seam doesn't move.

        Idempotent (a no-op is trivially idempotent across pod restarts).
        """
        _log.info(
            "keycloak_operations_registered",
            count=0,
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
            note="T1 substrate ships zero ops; read ops land in G3.13-T2",
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
