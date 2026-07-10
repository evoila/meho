# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential resolution for the proxmox connector (G0.x #2238).

The hand-rolled
:class:`~meho_backplane.connectors.proxmox.connector.ProxmoxConnector`
reads its Proxmox VE credentials from the target's Vault ``secret_ref`` and
supports **two** upstream auth protocols, discriminated by which fields the
operator stored:

* **API token (preferred)** — the operator stored ``token_id`` +
  ``token_secret``. The connector sends
  ``Authorization: PVEAPIToken=<token_id>=<token_secret>`` on every request.
  ``token_id`` is the full ``USER@REALM!TOKENID`` triple minted via
  *Datacenter → Permissions → API Tokens* (or ``pveum user token add``);
  ``token_secret`` is the UUID printed **once** at creation. API tokens are
  **CSRF-exempt**: POST/PUT/DELETE need no ``CSRFPreventionToken`` — the
  load-bearing reason the token path is preferred over the ticket path for a
  read/write connector.

* **Ticket / cookie (fallback)** — the operator stored ``username`` +
  ``password`` (optionally ``realm``, default ``pam``). The connector
  ``POST``\\s ``/api2/json/access/ticket`` to mint a ticket, sends it as the
  ``PVEAuthCookie`` cookie, and — because ticket auth is **not** CSRF-exempt
  — attaches the returned ``CSRFPreventionToken`` header on every write. The
  ticket login round-trip lives on the connector (it needs the pooled HTTP
  client); this module only resolves the stored material and decides which
  protocol the target is configured for.

The discriminator mirrors the gh-rest connector's App-vs-PAT split: the
resolver reads the raw KV-v2 payload via
:func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`
(operator-context Vault read, two-phase error contract) and inspects which
fields are present. Token material wins when both a token and a
username/password pair are stored, so a target can carry both and the
CSRF-exempt path is chosen automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)

__all__ = [
    "PASSWORD_FIELD",
    "REALM_FIELD",
    "TOKEN_ID_FIELD",
    "TOKEN_SECRET_FIELD",
    "USERNAME_FIELD",
    "ProxmoxCredentials",
    "ProxmoxCredentialsLoader",
    "ProxmoxTargetLike",
    "build_ticket_username",
    "load_credentials_from_vault",
]

#: KV-v2 secret field names. Kept as module constants so the connector, the
#: default loader, and the tests share one source of truth for what an
#: operator must store under ``target.secret_ref``.
TOKEN_ID_FIELD = "token_id"
TOKEN_SECRET_FIELD = "token_secret"
USERNAME_FIELD = "username"
PASSWORD_FIELD = "password"
REALM_FIELD = "realm"

#: Default Proxmox authentication realm for ticket auth when the operator
#: stored a bare ``username`` with no ``@realm`` suffix and no ``realm``
#: field. ``pam`` is the Linux-PAM realm every PVE install ships.
_DEFAULT_REALM = "pam"


@runtime_checkable
class ProxmoxTargetLike(Protocol):
    """Minimum target shape :class:`ProxmoxConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` satisfies it unchanged. ``id`` /
    ``tenant_id`` form the tenant-unique credential-cache key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    so two same-named targets in different tenants never share a cached
    token or ticket. ``secret_ref`` is the Vault path the loader resolves;
    ``auth_model`` is checked at the boundary. ``port`` defaults to 8006 for
    Proxmox VE; :meth:`HttpConnector._base_url` includes it verbatim (it is
    not 443). ``verify_tls`` / ``tls_ca_pin`` drive the base HTTP adapter's
    per-target self-signed-TLS handling.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


@dataclass(frozen=True)
class ProxmoxCredentials:
    """Resolved Proxmox credential material, discriminated by ``mode``.

    ``mode="token"`` carries ``token_id`` + ``token_secret`` (the CSRF-exempt
    API-token path). ``mode="ticket"`` carries ``username`` (already
    ``user@realm``-qualified via :func:`build_ticket_username`) + ``password``
    (the ticket-login path). The connector never logs any of these values.
    """

    mode: Literal["token", "ticket"]
    token_id: str | None = None
    token_secret: str | None = None
    username: str | None = None
    password: str | None = None


ProxmoxCredentialsLoader = Callable[[ProxmoxTargetLike, Operator], Awaitable[ProxmoxCredentials]]
"""Async callable resolving a (target, operator) pair to Proxmox credentials.

Production uses :func:`load_credentials_from_vault`; unit and integration
tests inject a loader returning a pre-built :class:`ProxmoxCredentials`.
"""


def build_ticket_username(username: str, realm: str | None) -> str:
    """Return the ``user@realm`` login string Proxmox's ticket API expects.

    When *username* already carries an ``@realm`` suffix it is returned
    unchanged (the stored value wins). Otherwise *realm* (or the ``pam``
    default) is appended. Proxmox's ``/access/ticket`` rejects a bare
    ``username`` with no realm, so this normalisation is load-bearing for
    the ticket path.
    """
    if "@" in username:
        return username
    return f"{username}@{realm or _DEFAULT_REALM}"


async def load_credentials_from_vault(
    target: ProxmoxTargetLike,
    operator: Operator,
) -> ProxmoxCredentials:
    """Default loader — operator-context Vault read + token/ticket discriminator.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to Vault's
    JWT/OIDC auth method) via
    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`,
    then inspects which fields are present:

    * ``token_id`` + ``token_secret`` → ``ProxmoxCredentials(mode="token")``
      (preferred; CSRF-exempt).
    * else ``username`` + ``password`` →
      ``ProxmoxCredentials(mode="ticket")`` with the login string normalised
      to ``user@realm``.

    Raises :class:`VaultCredentialsReadError` when the payload carries
    neither a complete token pair nor a complete username/password pair, so a
    half-configured target fails closed with an operator-actionable message
    rather than a bare ``KeyError`` at request time. Vault login-phase
    failures (:class:`~meho_backplane.auth.vault.VaultClientError` subclasses)
    propagate verbatim.
    """
    secret_data = await load_vault_secret_data(target, operator)

    token_id = _optional_field(secret_data, TOKEN_ID_FIELD)
    token_secret = _optional_field(secret_data, TOKEN_SECRET_FIELD)
    if token_id and token_secret:
        return ProxmoxCredentials(mode="token", token_id=token_id, token_secret=token_secret)

    username = _optional_field(secret_data, USERNAME_FIELD)
    password = _optional_field(secret_data, PASSWORD_FIELD)
    if username and password:
        realm = _optional_field(secret_data, REALM_FIELD)
        return ProxmoxCredentials(
            mode="ticket",
            username=build_ticket_username(username, realm),
            password=password,
        )

    raise VaultCredentialsReadError(
        f"proxmox target {target.name!r}: secret at secret_ref carries neither a "
        f"complete API-token pair ({TOKEN_ID_FIELD!r} + {TOKEN_SECRET_FIELD!r}) "
        f"nor a complete ticket pair ({USERNAME_FIELD!r} + {PASSWORD_FIELD!r}). "
        f"Store an API token (preferred) or username/password to authenticate."
    )


def _optional_field(secret_data: dict[str, object], field: str) -> str | None:
    """Return *field* from the KV-v2 payload as a stripped str, or ``None``.

    A missing key, an empty string, or a whitespace-only value all map to
    ``None`` so the discriminator treats a blank field as "not configured"
    rather than a valid-but-empty credential.
    """
    raw = secret_data.get(field)
    if raw is None:
        return None
    value = strip_credential_value(raw)
    return value or None
