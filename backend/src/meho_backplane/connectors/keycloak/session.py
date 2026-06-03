# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target shape + admin-credential loader for the keycloak connector.

The load-bearing design point of the Keycloak connector is the
**admin-vs-operator credential split**. The backplane authenticates its
*own* callers through operator-OIDC (a Keycloak-issued JWT carried on
:attr:`~meho_backplane.auth.operator.Operator.raw_jwt`). The connector
that *manages* Keycloak must not depend on that path: if operator-OIDC
were the only way in, the connector could never bootstrap a freshly
deployed Keycloak whose operator-login clients are not yet configured
(the chicken-and-egg the issue body calls out). So the connector
authenticates to the Keycloak Admin REST API with a **separate admin
credential** sourced from Vault at the consumer path
``secret/rdc-hetzner-dc/keycloak/admin``.

Credential-protocol discriminator
==================================

The admin secret carries one of two shapes; the loader picks the path
by inspecting **which fields the operator stored** (the same
payload-shape discriminator the gh-rest connector uses for its
App-vs-PAT picker — see ``connectors/github/session.py``):

* ``client_id`` + ``client_secret`` present →
  :class:`KeycloakClientCredentials` (the preferred
  ``client_credentials`` grant against a service-account client such as
  ``admin-cli`` or a dedicated ``meho-admin`` client).
* ``username`` + ``password`` present (and the client-credentials shape
  is not) → :class:`KeycloakPasswordCredentials` (the
  ``password`` grant against ``admin-cli``, the break-glass fallback).
* Neither shape present → :class:`KeycloakAmbiguousVaultPayloadError`
  with a remediation-bearing message naming both field shapes and which
  fields were present (no values echoed).

Why the Vault read still threads the operator
=============================================

The *secret read itself* is an operator-context Vault KV-v2 read (the
locked Option A decision in ``docs/architecture/connector-auth.md``):
the operator's validated JWT is forwarded to Vault's JWT/OIDC auth
method so the read is attributed to the operator's Vault Identity entity
with per-operator RBAC + audit. That is **not** the same as reusing the
operator's OIDC token to authenticate to *Keycloak* — the operator JWT
authorises the Vault read; the admin credential read out of Vault is
what authenticates to the Keycloak Admin API. The split is enforced in
:class:`~meho_backplane.connectors.keycloak.connector.KeycloakConnector`:
the operator token never appears in a request to the Keycloak admin
surface.

Target configuration
=====================

Base URL is derived from ``target.host`` / ``target.port`` by the
:class:`~meho_backplane.connectors.adapters.http.HttpConnector` base.
The three Keycloak-specific knobs live on ``target.extras`` (a free-form
``Mapping`` on the concrete ``Target`` model) so they are
target-configurable without a schema migration:

* ``admin_realm`` — the realm the admin client authenticates against
  (the admin-cli / service-account client typically lives in
  ``master``). Defaults to :data:`DEFAULT_ADMIN_REALM`.
* ``managed_realm`` — the realm the connector manages and fingerprints
  (``evba`` on the RDC fleet). Defaults to :data:`DEFAULT_MANAGED_REALM`.

Both are read through :func:`resolve_realm_config` which tolerates a
missing ``extras`` attribute (pre-G0.3 stub targets) and falls back to
the defaults.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)

__all__ = [
    "DEFAULT_ADMIN_REALM",
    "DEFAULT_MANAGED_REALM",
    "KeycloakAdminCredentials",
    "KeycloakAdminCredentialsLoader",
    "KeycloakAmbiguousVaultPayloadError",
    "KeycloakClientCredentials",
    "KeycloakPasswordCredentials",
    "KeycloakTargetLike",
    "RealmConfig",
    "load_admin_credentials_from_vault",
    "quote_segment",
    "resolve_realm_config",
]

#: The realm the admin client authenticates against. Keycloak's
#: ``admin-cli`` client and most service-account admin clients live in
#: ``master``; a target can override via ``extras["admin_realm"]``.
DEFAULT_ADMIN_REALM: str = "master"

#: The realm the connector manages + fingerprints. ``evba`` is the RDC
#: fleet's managed realm; a target can override via
#: ``extras["managed_realm"]``.
DEFAULT_MANAGED_REALM: str = "evba"

#: The four field names the credential-shape discriminator inspects. Kept
#: as module constants so the loader and its tests share one source of
#: truth and error messages stay diff-stable.
_CLIENT_FIELDS: tuple[str, str] = ("client_id", "client_secret")
_PASSWORD_FIELDS: tuple[str, str] = ("username", "password")


def quote_segment(value: Any) -> str:
    """Percent-encode a caller-supplied id/uuid for a URL path segment.

    Encodes ``/`` (``safe=""``), so a traversal-shaped id such as
    ``../../../../realms/master/clients`` becomes
    ``..%2F..%2F..%2F..%2Frealms%2Fmaster%2Fclients`` and cannot alter
    the request path structure. Mirrors the ArgoCD connector's
    ``_quote_name`` (``argocd/ops_write.py:130-132``).

    Applied at every site where a caller-supplied id/uuid is interpolated
    into a Keycloak Admin REST path — both read and write ops.
    """
    return quote(str(value), safe="")


@runtime_checkable
class KeycloakTargetLike(Protocol):
    """Minimum target shape :class:`KeycloakConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` satisfies this unchanged (it carries
    ``extras`` as a ``Mapping``); until a test needs the real model it
    can pass a stub dataclass.

    Fields:

    * ``name`` — per-target cache key (admin token + realm config).
    * ``host`` / ``port`` — forwarded to
      :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._base_url`.
    * ``secret_ref`` — Vault KV-v2 path the loader resolves to the admin
      credential payload (``client_id``/``client_secret`` or
      ``username``/``password``).
    * ``auth_model`` — checked at the boundary by
      :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`;
      only ``"shared_service_account"`` / ``None`` accepted.
    * ``extras`` — free-form mapping carrying the optional
      ``admin_realm`` / ``managed_realm`` overrides.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    extras: Mapping[str, Any]


@dataclass(frozen=True)
class RealmConfig:
    """Resolved admin-realm + managed-realm pair for one target."""

    admin_realm: str
    managed_realm: str


def resolve_realm_config(target: KeycloakTargetLike) -> RealmConfig:
    """Resolve a target's admin-realm + managed-realm from ``extras``.

    Reads ``target.extras["admin_realm"]`` / ``["managed_realm"]`` and
    falls back to :data:`DEFAULT_ADMIN_REALM` / :data:`DEFAULT_MANAGED_REALM`
    when the key is absent, empty, or the ``extras`` attribute is missing
    entirely (a pre-G0.3 stub target). Non-string / empty values fall
    back to the default rather than producing a malformed realm path.
    """
    extras = getattr(target, "extras", None) or {}
    admin = extras.get("admin_realm") if isinstance(extras, Mapping) else None
    managed = extras.get("managed_realm") if isinstance(extras, Mapping) else None
    return RealmConfig(
        admin_realm=admin if isinstance(admin, str) and admin else DEFAULT_ADMIN_REALM,
        managed_realm=managed if isinstance(managed, str) and managed else DEFAULT_MANAGED_REALM,
    )


# ---------------------------------------------------------------------------
# Admin credential shapes (the client_credentials-vs-password discriminator)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeycloakClientCredentials:
    """``client_credentials`` grant against a service-account client.

    The preferred admin-auth path: a confidential client (``admin-cli``
    with a secret, or a dedicated ``meho-admin`` client) with the
    ``realm-management`` service-account roles needed to read realm
    metadata.
    """

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class KeycloakPasswordCredentials:
    """``password`` grant against ``admin-cli`` — the break-glass fallback.

    Used when the operator stored an admin username/password rather than
    a service-account client secret. ``client_id`` defaults to
    ``admin-cli`` (the public client Keycloak ships for the
    direct-access-grant password flow) but a target can override it via
    a ``client_id`` field in the Vault secret.
    """

    username: str
    password: str
    client_id: str = "admin-cli"


#: Tagged union of the two admin-credential shapes the connector accepts.
KeycloakAdminCredentials = KeycloakClientCredentials | KeycloakPasswordCredentials

#: Async callable resolving a ``(target, operator)`` pair to admin
#: credentials. Injected on connector construction so production uses the
#: live Vault read while unit tests pass a stub returning a pre-built
#: credential object — the same dependency-injection seam the VCF / gh
#: connectors expose.
KeycloakAdminCredentialsLoader = Callable[
    [KeycloakTargetLike, Operator], Awaitable[KeycloakAdminCredentials]
]


class KeycloakAmbiguousVaultPayloadError(Exception):
    """The admin Vault secret carries neither admin-credential shape.

    Raised when the KV-v2 payload at ``target.secret_ref`` has neither
    the ``client_id`` + ``client_secret`` pair nor the ``username`` +
    ``password`` pair. The message names both shapes and which fields
    were present (no values echoed) so the operator can fix the secret.
    """


async def load_admin_credentials_from_vault(
    target: KeycloakTargetLike,
    operator: Operator,
) -> KeycloakAdminCredentials:
    """Default admin-credential loader — live operator-context Vault read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated JWT is forwarded to Vault's
    JWT/OIDC auth method via the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`
    helper) and picks the admin-auth protocol from the payload shape:

    * ``client_id`` + ``client_secret`` →
      :class:`KeycloakClientCredentials`.
    * ``username`` + ``password`` (when the client-credentials shape is
      absent) → :class:`KeycloakPasswordCredentials`, carrying the
      optional ``client_id`` override (default ``admin-cli``).
    * neither → :class:`KeycloakAmbiguousVaultPayloadError`.

    The error contract for the read itself is the shared helper's:
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    on read-phase failure (empty ``operator.raw_jwt`` for a
    system-initiated call, unset ``secret_ref``, malformed payload) and
    :class:`~meho_backplane.auth.vault.VaultClientError` on login-phase
    failure. Both propagate verbatim so the connector can distinguish
    login from read.

    The returned credential object is ephemeral in-memory state — like
    the shared helper's return, it must never enter a log event, an
    :class:`~meho_backplane.connectors.schemas.OperationResult`, or any
    durable artifact.
    """
    secret_data = await load_vault_secret_data(target, operator)
    present = set(secret_data.keys())

    # Every credential field is whitespace-stripped via
    # ``strip_credential_value`` so a trailing newline (the #1 secret-storage
    # artifact) never reaches Keycloak's token endpoint verbatim — a stray
    # ``\n`` on client_secret surfaces as ``unauthorized_client`` that reads
    # like a permissions/realm problem rather than a storage artifact.
    if all(field in present for field in _CLIENT_FIELDS):
        return KeycloakClientCredentials(
            client_id=strip_credential_value(secret_data["client_id"]),
            client_secret=strip_credential_value(secret_data["client_secret"]),
        )
    if all(field in present for field in _PASSWORD_FIELDS):
        # ``client_id`` is optional in the password shape — Keycloak's
        # direct-access-grant flow defaults to the ``admin-cli`` public
        # client when the operator didn't store an explicit one.
        client_id = secret_data.get("client_id")
        stripped_client_id = strip_credential_value(client_id) if isinstance(client_id, str) else ""
        return KeycloakPasswordCredentials(
            username=strip_credential_value(secret_data["username"]),
            password=strip_credential_value(secret_data["password"]),
            client_id=stripped_client_id or "admin-cli",
        )

    raise KeycloakAmbiguousVaultPayloadError(
        "keycloak_ambiguous_vault_payload: Vault secret for target "
        f"{target.name!r} (secret_ref={target.secret_ref!r}) does not carry "
        "either admin-credential shape the keycloak connector supports. For "
        f"the client_credentials path, populate both {list(_CLIENT_FIELDS)!r}; "
        f"for the password break-glass fallback, populate both "
        f"{list(_PASSWORD_FIELDS)!r}. Fields present in the secret: "
        f"{sorted(present)!r}. See docs/codebase/connectors-keycloak.md "
        "§ 'Admin credential discriminator' for the payload shape on each path."
    )
