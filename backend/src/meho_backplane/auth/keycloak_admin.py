# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak Admin REST API client — thin async wrapper for G11.2-T1 (#815).

This module provides :class:`KeycloakAdminClient`, an async context-manager
that authenticates against the Keycloak Admin REST API using the
``client_credentials`` flow and exposes the three operations the agent-
principal lifecycle service needs:

* :meth:`create_client` — POST a new Keycloak client with
  ``kind=agent`` in its attributes and return the Keycloak-assigned id.
* :meth:`disable_client` — GET-then-PUT the client's ``enabled=false``
  (kill switch): the full representation is round-tripped so the PUT
  does not wipe collection fields like ``attributes`` (see the method
  docstring; keycloak#24920). Tokens already issued remain valid until
  their ``exp``; ``enabled=false`` blocks *new* token grants immediately.
* :meth:`delete_client` — DELETE the client outright. Used to roll back
  a created client when the DB row that records it cannot be written, so
  register never leaves an orphaned, unrevocable token-issuing identity.

Design decisions
----------------

* **No ``python-keycloak`` dependency** — that library pulls in an
  outdated ``requests`` (sync) stack and the backplane is async
  everywhere. A thin httpx wrapper over the Admin REST API is ~100 LOC
  and has no hidden coupling.
* **Per-call ``client_credentials`` token** — the admin client secret
  is sensitive. Keeping the token ephemeral (fetched at client-enter,
  discarded at client-exit) limits the blast radius of a logging
  regression. v0.2 dogfood load is low (register/revoke are rare ops);
  caching the admin token is a v0.3 optimisation if the endpoint shows up
  in profiling.
* **Fail-open with 503 when admin is not configured** — ``KEYCLOAK_ADMIN_URL``
  is optional. When it is not set, the service layer raises
  :class:`KeycloakAdminNotConfiguredError`. Routes translate that to 503
  ``keycloak_admin_not_configured``.
* **No retries** — each caller wraps the client in a ``try/except``
  and surfaces the error. Transient failures are visible in audit logs.
  Adding tenacity-backed retries is straightforward but deferred.

HTTP timeout
------------

Uses :data:`_ADMIN_HTTP_TIMEOUT_SECONDS`. Matches the JWKS-fetch timeout
in :mod:`meho_backplane.auth.jwt`; tight enough to fail-closed quickly
without starving the request slot.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from meho_backplane.settings import Settings, get_settings

__all__ = [
    "KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL",
    "KeycloakAdminClient",
    "KeycloakAdminError",
    "KeycloakAdminNotConfiguredError",
    "KeycloakClientConflictError",
    "KeycloakClientNotFoundError",
]

_ADMIN_HTTP_TIMEOUT_SECONDS: float = 10.0

#: Realm default-default client scopes an agent client must carry. Clients
#: created through the Admin REST ``POST /clients`` do **not** inherit the
#: realm's default scopes the way the Admin Console "Create" button does
#: (the request body must set ``defaultClientScopes`` explicitly), so the
#: ``basic`` scope — which carries the ``sub`` protocol mapper Keycloak 25+
#: moved out of the hardcoded token path — is absent unless named here. A
#: token without ``sub`` is rejected by ``verify_jwt_for_audience``
#: (``missing_sub``, RFC 9068 §2.2.1). ``roles``/``web-origins``/``acr`` are
#: the rest of the realm default set; they are cheap and keep the agent
#: client byte-identical to a console-created one.
_AGENT_DEFAULT_CLIENT_SCOPES: tuple[str, ...] = ("basic", "roles", "web-origins", "acr")

#: Gold-standard 503 detail surfaced by ``POST /api/v1/agent-principals``
#: (and any other admin-surfaced route that catches
#: :class:`KeycloakAdminNotConfiguredError`) when the Keycloak admin
#: client is unwired. Symmetric with the
#: :data:`~meho_backplane.ui.auth.flow.MISSING_CLIENT_SECRET_DETAIL`
#: shape (G0.14-T7 #1148 — three-clause: domain code + named env vars
#: + doc reference). Compliant with the convention codified in
#: ``docs/codebase/error-message-shape.md`` (G0.14-T11 #1141). The
#: constant lives here, not at the route, so any future admin-using
#: route catches the same exception and emits the same message
#: verbatim.
KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL: str = (
    "keycloak_admin_not_configured: KEYCLOAK_ADMIN_URL / "
    "KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_CLIENT_SECRET are unset. "
    "Provision the confidential admin client per "
    "docs/cross-repo/keycloak-agent-client.md before defining agent "
    "principals."
)


class KeycloakAdminError(Exception):
    """Base class for Keycloak Admin API failures."""


class KeycloakAdminNotConfiguredError(KeycloakAdminError):
    """Raised when ``KEYCLOAK_ADMIN_URL`` / credentials are not set."""


class KeycloakClientConflictError(KeycloakAdminError):
    """Raised when a client with the given ``client_id`` already exists."""


class KeycloakClientNotFoundError(KeycloakAdminError):
    """Raised when the target client does not exist (404 from Keycloak)."""


def _hardcoded_claim_mapper(name: str, claim_name: str, claim_value: str) -> dict[str, Any]:
    """Build an ``oidc-hardcoded-claim-mapper`` representation.

    The ``config`` keys are Keycloak's dotted protocol-mapper config names
    (not camelCase); they mirror the ``agent:test-bot`` rows in the
    live-Keycloak integration realm so the API-created client is
    byte-equivalent to the fixture that already authenticates end-to-end.
    The claim is stamped onto the **access** token only — the
    ``client_credentials`` grant issues no ID or userinfo token.
    """
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": claim_name,
            "claim.value": claim_value,
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "false",
        },
    }


def _agent_protocol_mappers(
    *,
    audience: str,
    tenant_id: str,
    tenant_role: str,
) -> list[dict[str, Any]]:
    """Return the protocol mappers an agent client needs to authenticate.

    Clones the mapper set the working ``meho-backplane`` client / the
    ``agent:test-bot`` integration-realm fixture carry (#1487):

    * an ``oidc-audience-mapper`` stamping *audience* into ``aud`` via the
      ``included.custom.audience`` config (the only way to land an
      arbitrary audience on a ``client_credentials`` token — the RFC 8707
      request param is ignored without a configured mapper);
    * hardcoded-claim mappers for ``tenant_id`` / ``tenant_role`` and
      ``principal_kind=agent`` so the Operator chain resolves the agent's
      tenant scope and ``PrincipalKind.AGENT`` discriminator.
    """
    return [
        {
            "name": "audience-mapper",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": audience,
                "id.token.claim": "false",
                "access.token.claim": "true",
            },
        },
        _hardcoded_claim_mapper("tenant-id-claim", "tenant_id", tenant_id),
        _hardcoded_claim_mapper("tenant-role-claim", "tenant_role", tenant_role),
        _hardcoded_claim_mapper("principal-kind-claim", "principal_kind", "agent"),
    ]


class KeycloakAdminClient:
    """Async context manager for Keycloak Admin REST API calls.

    The client authenticates via ``client_credentials`` on
    :func:`__aenter__` and discards the token on :func:`__aexit__`.
    All network errors are surfaced as :class:`KeycloakAdminError`
    subclasses so callers can map them to structured HTTP responses
    without importing httpx.

    Usage::

        async with KeycloakAdminClient.from_settings() as kc:
            internal_id = await kc.create_client(
                client_id="agent:my-bot",
                name="my-bot",
                tenant_id=str(tenant_id),
                owner_sub=operator.sub,
                audience=settings.keycloak_audience,
                tenant_role="tenant_admin",
            )
    """

    def __init__(
        self,
        *,
        admin_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._admin_url = admin_url.rstrip("/")
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._http: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> KeycloakAdminClient:
        """Build from the process-wide :class:`Settings`.

        Raises :class:`KeycloakAdminNotConfiguredError` immediately when
        ``keycloak_admin_url`` or ``keycloak_admin_client_id`` is empty
        so the service layer can surface a 503 before making any HTTP
        call.
        """
        if settings is None:
            settings = get_settings()
        if not settings.keycloak_admin_url:
            raise KeycloakAdminNotConfiguredError(
                "KEYCLOAK_ADMIN_URL is not set; "
                "the agent-principal lifecycle surface is unavailable."
            )
        if not settings.keycloak_admin_client_id:
            raise KeycloakAdminNotConfiguredError(
                "KEYCLOAK_ADMIN_CLIENT_ID is not set; "
                "the agent-principal lifecycle surface is unavailable."
            )
        if not settings.keycloak_admin_client_secret:
            raise KeycloakAdminNotConfiguredError(
                "KEYCLOAK_ADMIN_CLIENT_SECRET is not set; "
                "the agent-principal lifecycle surface is unavailable."
            )
        # Derive the token endpoint from the issuer URL. The admin API URL
        # is ``{issuer_url}/admin/realms/{realm}``; the token endpoint is
        # at ``{protocol_root}/realms/{realm}/protocol/openid-connect/token``.
        # Simpler: the backplane already has KEYCLOAK_ISSUER_URL; build
        # the token URL from the issuer + the standard OIDC token path.
        issuer = str(settings.keycloak_issuer_url).rstrip("/")
        token_url = f"{issuer}/protocol/openid-connect/token"
        return cls(
            admin_url=settings.keycloak_admin_url,
            token_url=token_url,
            client_id=settings.keycloak_admin_client_id,
            client_secret=settings.keycloak_admin_client_secret,
        )

    async def __aenter__(self) -> KeycloakAdminClient:
        self._http = httpx.AsyncClient(timeout=_ADMIN_HTTP_TIMEOUT_SECONDS)
        try:
            await self._authenticate()
        except BaseException:
            # __aexit__ never runs when __aenter__ raises, so close the
            # just-opened client here or every failed auth leaks a socket.
            await self._http.aclose()
            self._http = None
            self._token = None
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._token = None
        self._http = None

    async def _authenticate(self) -> None:
        """Obtain an admin access token via client_credentials."""
        assert self._http is not None
        log = structlog.get_logger(__name__)
        try:
            resp = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "keycloak_admin_auth_failed",
                status=exc.response.status_code,
            )
            raise KeycloakAdminError(
                f"Keycloak admin auth failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("keycloak_admin_auth_unreachable", error=type(exc).__name__)
            raise KeycloakAdminError(
                f"Keycloak admin auth unreachable: {type(exc).__name__}"
            ) from exc
        data: Any = resp.json()
        self._token = data.get("access_token", "")
        if not self._token:
            raise KeycloakAdminError("Keycloak admin auth returned no access_token")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def create_client(
        self,
        *,
        client_id: str,
        name: str,
        tenant_id: str,
        owner_sub: str,
        audience: str,
        tenant_role: str,
    ) -> str:
        """Register a new Keycloak client tagged ``kind=agent``.

        Returns the Keycloak-assigned *internal* UUID (the ``id`` field in
        the representation, distinct from the OAuth ``clientId``). The
        caller stores this as ``keycloak_internal_id`` on the
        :class:`~meho_backplane.db.models.AgentPrincipal` row for later
        disable / delete calls.

        The created client is configured as a **confidential
        service-account** (``serviceAccountsEnabled=true``,
        ``publicClient=false``) with no redirect URIs — it authenticates
        via ``client_credentials`` and never involves a browser. Custom
        attributes ``kind=agent``, ``tenant_id``, ``owner_sub`` are added
        so the realm admin console and IaC tooling can identify agent clients.

        Token-claim provisioning (the fix for #1487): the client is created
        with the **same** protocol-mapper + default-client-scope set the
        working ``meho-backplane`` client carries, so its
        ``client_credentials`` token validates through
        :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` with no
        manual Keycloak surgery. Without these, a scheduled agent run dies
        at JWT verify (pre-dispatch) because the token lacks ``aud``
        (``missing_audience``), ``sub`` (carried by the ``basic`` scope's
        subject mapper — Keycloak 25+ moved it out of the hardcoded path),
        and the ``tenant_id`` / ``tenant_role`` claims the Operator chain
        requires:

        * an ``oidc-audience-mapper`` stamping *audience* into ``aud`` —
          stock Keycloak does **not** honour the RFC 8707 ``audience``
          request param on a ``client_credentials`` grant without this
          mapper, so requesting the audience at mint time is not enough;
        * ``oidc-hardcoded-claim-mapper`` rows for ``tenant_id`` /
          ``tenant_role`` / ``principal_kind=agent`` (the same shape the
          live-Keycloak integration realm injects on ``agent:test-bot``);
        * the realm default client scopes
          (:data:`_AGENT_DEFAULT_CLIENT_SCOPES`) that carry ``sub``.

        Raises :class:`KeycloakClientConflictError` when a client with the
        same ``clientId`` already exists (Keycloak 409).
        """
        assert self._http is not None
        assert self._token
        log = structlog.get_logger(__name__)
        payload: dict[str, Any] = {
            "clientId": client_id,
            "name": name,
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "attributes": {
                "kind": "agent",
                "tenant_id": tenant_id,
                "owner_sub": owner_sub,
            },
            "protocolMappers": _agent_protocol_mappers(
                audience=audience,
                tenant_id=tenant_id,
                tenant_role=tenant_role,
            ),
            "defaultClientScopes": list(_AGENT_DEFAULT_CLIENT_SCOPES),
        }
        try:
            resp = await self._http.post(
                f"{self._admin_url}/clients",
                content=json.dumps(payload),
                headers={**self._auth_headers(), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(
                f"Keycloak create_client network error: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 409:
            raise KeycloakClientConflictError(f"Keycloak client {client_id!r} already exists")
        if resp.status_code not in (200, 201):
            log.warning(
                "keycloak_create_client_failed",
                client_id=client_id,
                status=resp.status_code,
            )
            raise KeycloakAdminError(f"Keycloak create_client failed: HTTP {resp.status_code}")
        # Keycloak 201 returns the new client UUID in the Location header:
        # ``/admin/realms/{realm}/clients/{uuid}``.
        location = resp.headers.get("location", "")
        internal_id = location.rstrip("/").rsplit("/", 1)[-1] if "/" in location else ""
        if not internal_id:
            raise KeycloakAdminError(
                "Keycloak create_client succeeded but returned no Location header"
            )
        return internal_id

    async def get_client_secret(self, keycloak_internal_id: str) -> str:
        """Return the ``client_credentials`` secret for an existing client.

        Calls ``GET /clients/{id}/client-secret`` (G0.19-T2 #1478). The
        Keycloak Admin REST API returns a ``CredentialRepresentation``
        ``{"type": "secret", "value": "<secret>"}``; this method extracts
        and returns the ``value``. Used by the agent-principal register
        path to capture the secret Keycloak generated for the new
        confidential client (``create_client`` only returns the internal
        UUID — the generated secret is never echoed there) so it can be
        persisted to Vault for the operator-less scheduler to read.

        The secret is **never** logged or surfaced in an error message —
        only its absence is.

        Raises
        ------
        KeycloakClientNotFoundError
            The internal id is unknown (Keycloak 404).
        KeycloakAdminError
            A non-404 failure, or a 200 whose body carries no usable
            ``value`` (a public client / a client without a secret).
        """
        assert self._http is not None
        assert self._token
        log = structlog.get_logger(__name__)
        url = f"{self._admin_url}/clients/{keycloak_internal_id}/client-secret"
        try:
            resp = await self._http.get(url, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(
                f"Keycloak get_client_secret network error: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            raise KeycloakClientNotFoundError(f"Keycloak client {keycloak_internal_id!r} not found")
        if resp.status_code != 200:
            log.warning(
                "keycloak_get_client_secret_failed",
                keycloak_internal_id=keycloak_internal_id,
                status=resp.status_code,
            )
            raise KeycloakAdminError(f"Keycloak get_client_secret failed: HTTP {resp.status_code}")
        representation: dict[str, Any] = resp.json()
        secret = str(representation.get("value", "")).strip()
        if not secret:
            # A confidential client always has a secret; an empty value
            # means the client is public (no secret) or the realm is
            # misconfigured. Surface it as an admin error rather than
            # persisting an empty credential the scheduler can't use.
            raise KeycloakAdminError(
                f"Keycloak get_client_secret returned no secret value for "
                f"client {keycloak_internal_id!r} (public client or "
                "misconfigured realm?)"
            )
        return secret

    async def disable_client(self, keycloak_internal_id: str) -> None:
        """Disable the Keycloak client identified by *keycloak_internal_id*.

        Sets ``enabled=false`` on the client representation — this is the
        kill switch: Keycloak stops issuing new tokens for the client
        immediately, while in-flight tokens remain valid until their ``exp``.
        The MEHO service layer also marks the :class:`~meho_backplane.db.models.AgentPrincipal`
        row as ``revoked=true`` in the same transaction.

        The Keycloak Admin REST API ``PUT /clients/{id}`` **replaces** the
        entire client representation — a partial payload (only
        ``{"enabled": false}``) would wipe all other attributes, including the
        ``kind=agent`` custom attribute that the principal-kind discriminator
        relies on. This method therefore GETs the current representation first,
        sets ``enabled=False``, and PUTs the full representation back.

        Raises :class:`KeycloakClientNotFoundError` when the internal id
        is unknown (Keycloak 404). This is expected when a client was
        already cleaned up out-of-band.
        """
        assert self._http is not None
        assert self._token
        log = structlog.get_logger(__name__)
        url = f"{self._admin_url}/clients/{keycloak_internal_id}"
        try:
            get_resp = await self._http.get(url, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(
                f"Keycloak disable_client GET network error: {type(exc).__name__}"
            ) from exc
        if get_resp.status_code == 404:
            raise KeycloakClientNotFoundError(f"Keycloak client {keycloak_internal_id!r} not found")
        if get_resp.status_code != 200:
            raise KeycloakAdminError(
                f"Keycloak disable_client GET failed: HTTP {get_resp.status_code}"
            )
        representation: dict[str, Any] = get_resp.json()
        representation["enabled"] = False
        try:
            put_resp = await self._http.put(
                url,
                content=json.dumps(representation),
                headers={**self._auth_headers(), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(
                f"Keycloak disable_client PUT network error: {type(exc).__name__}"
            ) from exc
        if put_resp.status_code == 404:
            raise KeycloakClientNotFoundError(f"Keycloak client {keycloak_internal_id!r} not found")
        if put_resp.status_code not in (200, 204):
            log.warning(
                "keycloak_disable_client_failed",
                keycloak_internal_id=keycloak_internal_id,
                status=put_resp.status_code,
            )
            raise KeycloakAdminError(
                f"Keycloak disable_client PUT failed: HTTP {put_resp.status_code}"
            )

    async def delete_client(self, keycloak_internal_id: str) -> None:
        """Delete the Keycloak client identified by *keycloak_internal_id*.

        Used to roll back a half-completed :meth:`create_client` when the
        agent-principal DB row cannot be written: a created client that is
        never recorded in MEHO is an orphaned, token-issuing identity with
        no kill switch, so register deletes it before surfacing the error.
        Unlike :meth:`disable_client`, this fully removes the client so a
        subsequent register with the same name is not permanently blocked
        by a Keycloak 409.

        Raises :class:`KeycloakClientNotFoundError` when the internal id
        is unknown (Keycloak 404) — already gone is success for cleanup.
        """
        assert self._http is not None
        assert self._token
        log = structlog.get_logger(__name__)
        try:
            resp = await self._http.delete(
                f"{self._admin_url}/clients/{keycloak_internal_id}",
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(
                f"Keycloak delete_client network error: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            raise KeycloakClientNotFoundError(f"Keycloak client {keycloak_internal_id!r} not found")
        if resp.status_code not in (200, 204):
            log.warning(
                "keycloak_delete_client_failed",
                keycloak_internal_id=keycloak_internal_id,
                status=resp.status_code,
            )
            raise KeycloakAdminError(f"Keycloak delete_client failed: HTTP {resp.status_code}")
