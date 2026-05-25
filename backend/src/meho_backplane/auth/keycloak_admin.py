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
    "KeycloakAdminClient",
    "KeycloakAdminError",
    "KeycloakAdminNotConfiguredError",
    "KeycloakClientConflictError",
    "KeycloakClientNotFoundError",
]

_ADMIN_HTTP_TIMEOUT_SECONDS: float = 10.0


class KeycloakAdminError(Exception):
    """Base class for Keycloak Admin API failures."""


class KeycloakAdminNotConfiguredError(KeycloakAdminError):
    """Raised when ``KEYCLOAK_ADMIN_URL`` / credentials are not set."""


class KeycloakClientConflictError(KeycloakAdminError):
    """Raised when a client with the given ``client_id`` already exists."""


class KeycloakClientNotFoundError(KeycloakAdminError):
    """Raised when the target client does not exist (404 from Keycloak)."""


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
