# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak :class:`SecretEndpoint` adapters — the broker's ``keycloak`` kind.

Kind ``"keycloak"`` carries **two** ref shapes, dispatched by
:func:`build_keycloak_secret_endpoint` (the callable registered under the
kind, see :func:`register_secret_endpoint` at module import) on the ref's
structure:

* **User-password sink** — ``<target>/<realm>/<username>#password``.
  :class:`KeycloakCredentialSecretEndpoint` sets a Keycloak user's
  password credential from a :class:`SecretMaterial` the broker already
  holds (#1578). It is the second connector kind the secret broker
  (Initiative #581) gained after the vault-kv pair (#1577), so a
  cross-kind move ``vault:secret/db/prod#password`` →
  ``keycloak:<target>/<realm>/<user>#password`` can be proven end to end
  — the broker's "≥2 kinds" definition of done.
* **Client-secret source** — ``<target>/<realm>/clients/<clientId>#secret``.
  :class:`KeycloakClientSecretSourceEndpoint` reads a confidential
  client's secret (#3619) so a move can land it in a sink store (Vault)
  without the value ever transiting a human or agent context. The literal
  ``clients/`` segment is what distinguishes a source ref from the sink
  ref above; both parse under the one kind.

Why the sink is write-only but the source is legitimate
=======================================================

Keycloak never serves a stored **user password** back over the Admin
REST API (credentials are write-only by design — Keycloak hashes them
and the plaintext is unrecoverable), so the user-password endpoint has
nothing for a source read to return:
:meth:`KeycloakCredentialSecretEndpoint.read_secret` raises
:class:`NotImplementedError` and the dispatcher maps it to a
``connector_error`` naming the kind, never a value.

A confidential **client secret** is different: Keycloak *does* serve it
over ``GET /admin/realms/{realm}/clients/{uuid}/client-secret`` →
``{"type":"secret","value":…}``, so a broker **source** is legitimate.
:meth:`KeycloakClientSecretSourceEndpoint.write_secret` is the mirror
image — a client-secret ref is a source, not a sink, so it raises
:class:`NotImplementedError`.

Reuse, not reimplementation
===========================

Neither adapter opens its own HTTP client. Both resolve the
:class:`KeycloakConnector` instance from the dispatcher's instance cache
(:func:`~meho_backplane.operations._handler_resolve.get_or_create_connector_instance`)
and drive its *existing* admin path:

* the **sink** uses :meth:`KeycloakConnector._find_user_uuid`
  (username→UUID) + :meth:`KeycloakConnector._write_admin` (``PUT
  .../users/{id}/reset-password`` with a CredentialRepresentation
  ``{type:"password", value, temporary}``) — exactly as
  :func:`~meho_backplane.connectors.keycloak.ops_write.keycloak_user_reset_password`
  does;
* the **source** uses :meth:`KeycloakConnector._find_client_uuid`
  (clientId→UUID via ``GET .../clients?clientId=<id>`` exact lookup) +
  :meth:`KeycloakConnector._get_admin_json` (``GET
  .../clients/{uuid}/client-secret``), reading the ``value`` off the
  ``{"type":"secret","value":…}`` representation.

The admin Bearer is minted under the connector's own admin credential;
the operator's JWT authorises only the per-operator target resolution and
never reaches Keycloak.

Ref grammar
===========

The store-specific ``ref`` (the part after ``keycloak:``) has two shapes,
routed by :func:`build_keycloak_secret_endpoint`:

**Sink** (user password) — one user credential::

    <target>/<realm>/<username>#password

* ``<target>`` — the MEHO target *name* (or alias) the Keycloak admin
  connection is configured under; resolved tenant-scoped via
  :func:`~meho_backplane.targets.resolver.resolve_target`.
* ``<realm>`` — the Keycloak realm the user lives in. Taken from the ref
  (the operator addresses it explicitly), not from the target's
  ``extras["managed_realm"]`` default.
* ``<username>`` — the human username; resolved to the internal UUID via
  the Admin REST ``?username=&exact=true`` lookup.
* ``#password`` (required) — the only writable field. Any other field is
  rejected so a malformed move fails before the admin write.

**Source** (client secret) — one confidential client's secret::

    <target>/<realm>/clients/<clientId>#secret

* ``<target>`` / ``<realm>`` — as above.
* the literal ``clients/`` segment is what distinguishes a source ref
  from the sink ref; it is how :func:`build_keycloak_secret_endpoint`
  routes.
* ``<clientId>`` — the human ``clientId``; resolved to the internal UUID
  via the Admin REST ``?clientId=<id>`` exact lookup.
* ``#secret`` (required) — the only field a client-secret source reads.
  Any other field on a ``clients/`` ref is rejected with a value-free
  error.

No secret in logs / response / audit
====================================

The value lives only inside the :class:`SecretMaterial` between the
source read and the sink write. It is read **once** on each side,
whitespace-stripped by :func:`strip_credential_value` (so a trailing
newline never rides into the credential — the same artifact the
credential loaders strip), and never returned. Each adapter's structlog
events carry only the target name, realm, and username / clientId —
never the value, and never the bytes the :class:`SecretMaterial` wraps.
The ``secret.move`` handler returns only ``status`` + ``value_sha256`` +
``length``; the audit row stores a ``params_hash`` of the (value-free)
references, so the value reaches neither the response nor the audit row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import structlog

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_SECRET_PATH,
    _USER_RESET_PASSWORD_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.session import KeycloakTargetLike, quote_segment
from meho_backplane.connectors.secret.endpoints import SecretMaterial, register_secret_endpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.secret.endpoints import SecretEndpoint

__all__ = [
    "KeycloakClientNotFoundError",
    "KeycloakClientSecretSourceEndpoint",
    "KeycloakCredentialSecretEndpoint",
    "KeycloakSecretRefError",
    "build_keycloak_secret_endpoint",
]

_log = structlog.get_logger(__name__)

#: The only credential field this sink writes. Keycloak's CredentialRepresentation
#: distinguishes credential types; the broker moves a password, so the ref's
#: ``#<field>`` fragment must name it. A non-password field is rejected rather
#: than silently coerced.
_SUPPORTED_FIELD = "password"

#: The only field a client-secret **source** reads. Mirrors the
#: ``{"type":"secret","value":…}`` client-secret representation Keycloak
#: serves; a ``clients/`` ref naming any other field is rejected.
_SOURCE_FIELD = "secret"

#: The literal address segment (position 2) that marks a ref as a
#: **client-secret source** rather than a **user-password sink**. A source
#: ref is ``<target>/<realm>/clients/<clientId>`` (four segments), a sink
#: ref ``<target>/<realm>/<username>`` (three) — so a user literally named
#: ``clients`` still routes to the sink (three segments).
_CLIENTS_SEGMENT = "clients"


class KeycloakSecretRefError(ValueError):
    """A keycloak ``ref`` is malformed or names an unsupported field.

    Raised for a sink ref missing the required ``#password`` fragment or
    without the ``<target>/<realm>/<username>`` triple, a source ref
    missing the ``#secret`` fragment or without the
    ``<target>/<realm>/clients/<clientId>`` quad, an empty segment, or a
    ``#<field>`` that does not match the ref's shape. A :class:`ValueError`
    so the dispatcher's ``connector_error`` branch surfaces
    ``exception_class="KeycloakSecretRefError"``. The message names the
    ref's address parts, never the value.
    """


class KeycloakClientNotFoundError(Exception):
    """A client-secret source addressed a ``clientId`` that does not exist.

    Raised when the Admin REST ``?clientId=<id>`` exact lookup returns no
    client, so there is no ``uuid`` to read a secret for. The dispatcher's
    ``connector_error`` branch surfaces
    ``exception_class="KeycloakClientNotFoundError"``. The message names
    the clientId / realm / target, never a secret value.
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


def _parse_keycloak_client_ref(ref: str) -> tuple[str, str, str]:
    """Split ``"<target>/<realm>/clients/<clientId>#secret"`` into ``(target, realm, clientId)``.

    The ``#<field>`` fragment is split off first (on the **last** ``#``, so
    a ``clientId`` may contain one) and validated to be ``secret`` — the
    only field the Keycloak client-secret endpoint serves. The remaining
    address must be exactly four ``/``-separated segments whose third is
    the literal ``clients``; every segment must be non-empty after
    stripping.
    """
    address, sep, field = ref.rpartition("#")
    if not sep or not address:
        raise KeycloakSecretRefError(
            f"malformed keycloak client-secret ref {ref!r}: expected "
            "'<target>/<realm>/clients/<clientId>#secret' "
            "(e.g. 'keycloak:my-keycloak/example-realm/clients/launcher-app#secret')"
        )
    field = field.strip()
    if field != _SOURCE_FIELD:
        raise KeycloakSecretRefError(
            f"keycloak client-secret ref {ref!r} names unsupported field {field!r}: "
            f"a client-secret source reads only the {_SOURCE_FIELD!r} field"
        )
    segments = [seg.strip() for seg in address.split("/")]
    if len(segments) != 4 or not all(segments) or segments[2] != _CLIENTS_SEGMENT:
        raise KeycloakSecretRefError(
            f"malformed keycloak client-secret ref {ref!r}: expected a "
            "'<target>/<realm>/clients/<clientId>' address before '#secret'"
        )
    target_name, realm, _clients_segment, client_id = segments
    return target_name, realm, client_id


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
            fill_path(
                _USER_RESET_PASSWORD_PATH,
                {"realm": quote_segment(self._realm), "user-id": quote_segment(uuid)},
            ),
            operator=operator,
            json=credential,
            idempotent_conflict=False,
        )


class KeycloakClientSecretSourceEndpoint:
    """A keycloak client-secret **source** addressing one confidential client's secret.

    Constructed per move from the parsed ``ref``. Source-only:
    :meth:`read_secret` resolves ``clientId``→UUID and reads the secret via
    the connector's existing admin-read path; :meth:`write_secret` raises
    (a client-secret ref is a move source, not a sink).
    """

    def __init__(self, ref: str) -> None:
        self._target_name, self._realm, self._client_id = _parse_keycloak_client_ref(ref)

    async def _resolve_target(self, operator: Operator) -> KeycloakTargetLike:
        """Resolve the ref's ``<target>`` to a ``KeycloakTargetLike``, tenant-scoped.

        Identical narrow to the sink's: the ORM row structurally satisfies
        ``KeycloakTargetLike`` (name, host, port, secret_ref, auth_model,
        extras) — the same row the dispatcher hands the keycloak read ops
        at runtime — but mypy cannot confirm the structural match across
        the ORM's mutable column types, so the narrow is explicit here.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        return cast(KeycloakTargetLike, row)

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Read the addressed client's secret and wrap it in a :class:`SecretMaterial`.

        Resolves the target (tenant-scoped, by name) and the client's UUID
        via :meth:`KeycloakConnector._find_client_uuid` (the ``?clientId=``
        exact lookup), then reads ``GET .../clients/{uuid}/client-secret``
        through :meth:`KeycloakConnector._get_admin_json` — reusing the
        connector's admin session-cache and Bearer mint exactly as the
        sink reuses the admin-write path; it opens no HTTP client of its
        own. The ``value`` is read off the ``{"type":"secret","value":…}``
        representation, whitespace-stripped via
        :func:`strip_credential_value` (so source and sink agree
        byte-for-byte), and wrapped. The value never enters a log event,
        the response, an exception message, or the audit row: the
        structlog event carries only target / realm / clientId, and a
        missing-value error names the address, never the value.
        """
        connector = get_or_create_connector_instance(KeycloakConnector)
        if not isinstance(connector, KeycloakConnector):  # pragma: no cover -- registry invariant
            raise TypeError(
                "secret broker keycloak source resolved a non-KeycloakConnector instance; "
                "the connector instance cache is misconfigured"
            )

        target = await self._resolve_target(operator)
        uuid = await connector._find_client_uuid(
            target, self._realm, self._client_id, operator=operator
        )
        if uuid is None:
            raise KeycloakClientNotFoundError(
                f"keycloak client-secret source: no client with clientId={self._client_id!r} "
                f"in realm {self._realm!r} on target {self._target_name!r}"
            )

        _log.debug(
            "secret_broker.keycloak.read",
            target=self._target_name,
            realm=self._realm,
            client_id=self._client_id,
        )
        payload = await connector._get_admin_json(
            target,
            fill_path(
                _CLIENT_SECRET_PATH,
                {"realm": quote_segment(self._realm), "client-uuid": quote_segment(uuid)},
            ),
            operator=operator,
        )
        value = payload.get("value")
        if not isinstance(value, str) or not value:
            raise KeycloakSecretRefError(
                "keycloak client-secret source: the Admin REST client-secret read for "
                f"clientId={self._client_id!r} in realm {self._realm!r} returned no "
                "'secret' value (the client may be public / bearer-only rather than "
                "confidential)"
            )
        return SecretMaterial(strip_credential_value(value))

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Unsupported — a client-secret ref is a move source, not a sink.

        Raised here (not at construction) so the kind can still be a move
        **source**; the dispatcher maps it to a ``connector_error`` naming
        the kind. To *write* a Keycloak credential, use the user-password
        sink ref ``<target>/<realm>/<username>#password``.
        """
        raise NotImplementedError(
            "keycloak client-secret refs are a move source, not a sink: a client "
            "secret cannot be set through this ref. Use the user-password sink ref "
            "'<target>/<realm>/<username>#password' to write a keycloak credential"
        )


def _looks_like_client_secret_ref(ref: str) -> bool:
    """Return ``True`` when *ref*'s address is a ``clients/`` client-secret source.

    The address is the part before the last ``#`` (a ref without a ``#``
    is taken whole so the source parser can raise the missing-field
    error). A source ref is ``<target>/<realm>/clients/<clientId>`` — four
    ``/``-separated segments whose third is the literal ``clients``; every
    other shape is a sink ref.
    """
    address = ref.rpartition("#")[0] or ref
    segments = [seg.strip() for seg in address.split("/")]
    return len(segments) == 4 and segments[2] == _CLIENTS_SEGMENT


def build_keycloak_secret_endpoint(ref: str) -> SecretEndpoint:
    """Registered factory for kind ``"keycloak"`` — dual-dispatch on the ref shape.

    Routes on the ref's structure, not its ``#field``, so a malformed
    ``clients/`` ref still lands in the source parser (which rejects a
    non-``secret`` field with a clear, value-free error):

    * a **client-secret source** ref
      (``<target>/<realm>/clients/<clientId>``, four address segments
      whose third is the literal ``clients``) →
      :class:`KeycloakClientSecretSourceEndpoint`;
    * any other ref → the existing user-password
      :class:`KeycloakCredentialSecretEndpoint` sink, whose parse and
      behaviour are unchanged.

    A user literally named ``clients`` still routes to the sink: a sink
    ref has three address segments, a source ref four.
    """
    if _looks_like_client_secret_ref(ref):
        return KeycloakClientSecretSourceEndpoint(ref)
    return KeycloakCredentialSecretEndpoint(ref)


# Register the keycloak secret adapters under kind ``"keycloak"`` at import
# time via the dual-dispatch factory (user-password sink + client-secret
# source). The ``connectors/secret`` package ``__init__`` imports this module
# so the registration lands before the lifespan runs the move op's registrar —
# mirroring how ``vault_endpoint`` registers the ``"vault"`` kind.
register_secret_endpoint("keycloak", build_keycloak_secret_endpoint)
