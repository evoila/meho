# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak-credential :class:`SecretEndpoint` sink — the broker's second kind.

Registered under kind ``"keycloak"`` (see :func:`register_secret_endpoint`
at module import), this adapter is a **sink only**: it sets a Keycloak
user's password credential from a :class:`SecretMaterial` the broker
already holds. It is the second connector kind the secret broker
(Initiative #581) gains after the vault-kv pair (#1577), so a cross-kind
move ``vault:secret/db/prod#password`` → ``keycloak:<target>/<realm>/<user>#password``
can be proven end to end — the broker's "≥2 kinds" definition of done.

Why sink-only
=============

Keycloak never serves a stored password back over the Admin REST API
(credentials are write-only by design — Keycloak hashes them and the
plaintext is unrecoverable), so a keycloak **source** has nothing to
read. :meth:`~KeycloakCredentialSecretEndpoint.read_secret` therefore
raises :class:`NotImplementedError`; the dispatcher maps it to a
``connector_error`` naming the kind, never a value.

Reuse, not reimplementation
===========================

The sink does **not** open its own HTTP client. It resolves the
:class:`KeycloakConnector` instance from the dispatcher's instance cache
(:func:`~meho_backplane.operations._handler_resolve.get_or_create_connector_instance`)
and drives the *existing* admin-write path —
:meth:`KeycloakConnector._find_user_uuid` (username→UUID) +
:meth:`KeycloakConnector._write_admin` (``PUT
.../users/{id}/reset-password`` with a CredentialRepresentation
``{type:"password", value, temporary}``) — exactly as
:func:`~meho_backplane.connectors.keycloak.ops_write.keycloak_user_reset_password`
does. The admin Bearer is minted under the connector's own admin
credential; the operator's JWT authorises only the per-operator target
resolution and never reaches Keycloak.

Ref grammar
===========

The store-specific ``ref`` (the part after ``keycloak:``) addresses one
user credential as::

    <target>/<realm>/<username>#<field>

* ``<target>`` — the MEHO target *name* (or alias) the Keycloak admin
  connection is configured under; resolved tenant-scoped via
  :func:`~meho_backplane.targets.resolver.resolve_target`.
* ``<realm>`` — the Keycloak realm the user lives in. Taken from the ref
  (the operator addresses it explicitly), not from the target's
  ``extras["managed_realm"]`` default.
* ``<username>`` — the human username; resolved to the internal UUID via
  the Admin REST ``?username=&exact=true`` lookup.
* ``#<field>`` (required) — must be ``password``; keycloak credentials
  are the only writable field here. Any other field is rejected so a
  malformed move fails before the admin write.

No secret in logs / response / audit
====================================

The value lives only inside the :class:`SecretMaterial` between the
source read and this sink write. It is read **once**, whitespace-stripped
by :func:`strip_credential_value` (so a trailing newline never rides into
the Keycloak password — the same artifact the credential loaders strip),
placed into the Admin REST PUT body, and never returned. The adapter's
structlog events carry only the target name, realm, and username — never
the value, and never the bytes the :class:`SecretMaterial` wraps. The
``secret.move`` handler returns only ``status`` + ``value_sha256`` +
``length``; the audit row stores a ``params_hash`` of the (value-free)
references, so the value reaches neither the response nor the audit row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import structlog

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.session import KeycloakTargetLike, quote_segment
from meho_backplane.connectors.secret.endpoints import register_secret_endpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.secret.endpoints import SecretMaterial

__all__ = [
    "KeycloakCredentialSecretEndpoint",
    "KeycloakSecretRefError",
]

_log = structlog.get_logger(__name__)

#: The only credential field this sink writes. Keycloak's CredentialRepresentation
#: distinguishes credential types; the broker moves a password, so the ref's
#: ``#<field>`` fragment must name it. A non-password field is rejected rather
#: than silently coerced.
_SUPPORTED_FIELD = "password"


class KeycloakSecretRefError(ValueError):
    """A keycloak ``ref`` is malformed or names an unsupported field.

    Raised for a ref missing the required ``#<field>`` fragment, a ref
    without the ``<target>/<realm>/<username>`` triple, an empty segment,
    or a ``#<field>`` other than ``password``. A :class:`ValueError` so
    the dispatcher's ``connector_error`` branch surfaces
    ``exception_class="KeycloakSecretRefError"``. The message names the
    ref's address parts, never the value.
    """


def _parse_keycloak_ref(ref: str) -> tuple[str, str, str]:
    """Split ``"<target>/<realm>/<username>#<field>"`` into ``(target, realm, username)``.

    The ``#<field>`` fragment is split off first (on the **last** ``#``,
    so a username may contain a ``#``) and validated to be ``password``.
    The remaining address is split into exactly three ``/``-separated
    segments. Every segment must be non-empty after stripping.
    """
    address, sep, field = ref.rpartition("#")
    if not sep or not address:
        raise KeycloakSecretRefError(
            f"malformed keycloak secret ref {ref!r}: expected "
            "'<target>/<realm>/<username>#password' "
            "(e.g. 'keycloak:rdc-keycloak/evba/operator-a#password')"
        )
    field = field.strip()
    if field != _SUPPORTED_FIELD:
        raise KeycloakSecretRefError(
            f"keycloak secret ref {ref!r} names unsupported field {field!r}: "
            f"only {_SUPPORTED_FIELD!r} credentials are writable here"
        )
    segments = [seg.strip() for seg in address.split("/")]
    if len(segments) != 3 or not all(segments):
        raise KeycloakSecretRefError(
            f"malformed keycloak secret ref {ref!r}: expected a "
            "'<target>/<realm>/<username>' address before '#password'"
        )
    target_name, realm, username = segments
    return target_name, realm, username


class KeycloakCredentialSecretEndpoint:
    """A keycloak credential **sink** addressing one user's password.

    Constructed per move from the parsed ``ref``. Sink-only:
    :meth:`read_secret` raises; :meth:`write_secret` sets the addressed
    user's password by reusing the connector's admin-write path.
    """

    def __init__(self, ref: str) -> None:
        self._target_name, self._realm, self._username = _parse_keycloak_ref(ref)

    async def _resolve_target(self, operator: Operator) -> KeycloakTargetLike:
        """Resolve the ref's ``<target>`` to a ``KeycloakTargetLike``, tenant-scoped.

        The ORM row structurally satisfies ``KeycloakTargetLike`` (name,
        host, port, secret_ref, auth_model, extras) — the same row the
        dispatcher hands the keycloak write ops at runtime. mypy can't
        confirm the structural match across the ORM's mutable column types
        (the ``AuthModel`` enum vs the Protocol's ``str | None``), so the
        narrow is made explicit at this single reuse site.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        return cast(KeycloakTargetLike, row)

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Unsupported — keycloak credentials are write-only.

        Keycloak hashes credentials and never serves the plaintext back,
        so there is nothing for a source read to return. Raised here (not
        at construction) so the kind can still be a move **sink**; the
        dispatcher maps it to a ``connector_error`` naming the kind.
        """
        raise NotImplementedError(
            "keycloak is a write-only secret sink: credentials cannot be read "
            "back from the Keycloak Admin REST API, so it cannot be a move source"
        )

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Set the addressed user's password from *material* server-side.

        Resolves the target (tenant-scoped, by name) and the user's UUID,
        then PUTs ``.../users/{id}/reset-password`` with a permanent
        password CredentialRepresentation. The value is read once from the
        :class:`SecretMaterial`, decoded, and whitespace-stripped via
        :func:`strip_credential_value` (matching the existing
        reset-password path so source and sink agree byte-for-byte); it
        never enters op params, the response, a log event, or the audit
        row. ``idempotent_conflict=False`` mirrors
        :func:`~meho_backplane.connectors.keycloak.ops_write.keycloak_user_reset_password`
        — a credential set is not a create, so a 409 should surface rather
        than be swallowed.
        """
        connector = get_or_create_connector_instance(KeycloakConnector)
        if not isinstance(connector, KeycloakConnector):  # pragma: no cover -- registry invariant
            raise TypeError(
                "secret broker keycloak sink resolved a non-KeycloakConnector instance; "
                "the connector instance cache is misconfigured"
            )

        target = await self._resolve_target(operator)
        uuid = await connector._find_user_uuid(
            target, self._realm, self._username, operator=operator
        )
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"keycloak secret sink: no user with username={self._username!r} "
                f"in realm {self._realm!r} on target {self._target_name!r}"
            )

        # Read the value exactly once, here, on the write path; strip the
        # trailing-newline artifact as the existing reset-password path does.
        credential = {
            "type": "password",
            "value": strip_credential_value(material.value.decode("utf-8")),
            "temporary": False,
        }
        _log.debug(
            "secret_broker.keycloak.write",
            target=self._target_name,
            realm=self._realm,
            username=self._username,
        )
        await connector._write_admin(
            target,
            "PUT",
            f"/admin/realms/{quote_segment(self._realm)}/users/"
            f"{quote_segment(uuid)}/reset-password",
            operator=operator,
            json=credential,
            idempotent_conflict=False,
        )


# Register the keycloak credential sink under kind ``"keycloak"`` at import
# time. The ``connectors/secret`` package ``__init__`` imports this module so
# the registration lands before the lifespan runs the move op's registrar —
# mirroring how ``vault_endpoint`` registers the ``"vault"`` kind.
register_secret_endpoint("keycloak", KeycloakCredentialSecretEndpoint)
