# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a connector says a target needs: extra settings, login fields, test read.

A connector class can declare four things on the :class:`Connector` base:

* ``default_port``: the port a target of this product usually has.
* ``target_fields``: the extra settings a target needs (keys in its ``extras``).
* ``credential_ways``: the ways to log in, each with the fields of the secret.
* ``test_operation``: the id of one safe read that proves the address, the
  login and the rights work.

The core only reads these declarations. It never changes how a connector logs
in. A value of ``None`` means "not declared". For ``credential_ways`` an empty
tuple means "declared: no credential needed".

:func:`check_connector_declaration` checks the shape of one class. The check of
the test read against the operation rows is in
:mod:`meho_backplane.operations.connector_declaration_check`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "MAX_NAME_LENGTH",
    "ConnectorDeclarationError",
    "CredentialField",
    "CredentialWay",
    "TargetField",
    "TargetFieldKind",
    "check_connector_declaration",
]

#: Same rule as the names in a secret field map: 1 to 200 characters.
MAX_NAME_LENGTH = 200

TargetFieldKind = Literal["text", "number", "boolean", "choice"]
_KINDS: frozenset[str] = frozenset({"text", "number", "boolean", "choice"})


class ConnectorDeclarationError(ValueError):
    """A connector class declares its target form wrongly. The message names the class."""


@dataclass(frozen=True)
class TargetField:
    """One extra setting of a target. The value lives under ``key`` in ``extras``."""

    key: str
    label: str
    help: str = ""
    required: bool = False
    kind: TargetFieldKind = "text"
    #: The allowed values when ``kind`` is ``"choice"``.
    choices: tuple[str, ...] = ()
    default: str | int | bool | None = None


@dataclass(frozen=True)
class CredentialField:
    """One field of the secret a person stores for a target."""

    name: str
    label: str
    help: str = ""
    required: bool = True
    #: Hidden fields are shown as a password box. A secret is hidden by default.
    hidden: bool = True
    #: Several lines, for a key or a kubeconfig.
    multiline: bool = False


@dataclass(frozen=True)
class CredentialWay:
    """One way to log in, for example "key" or "password"."""

    key: str
    label: str
    fields: tuple[CredentialField, ...]


def _bad_name(name: str) -> bool:
    return not name.strip() or len(name) > MAX_NAME_LENGTH


def _check_target_fields(who: str, fields: tuple[TargetField, ...]) -> None:
    seen: set[str] = set()
    for field in fields:
        if _bad_name(field.key):
            raise ConnectorDeclarationError(
                f"{who}: a target field has a blank key or a key longer than "
                f"{MAX_NAME_LENGTH} characters."
            )
        if field.key in seen:
            raise ConnectorDeclarationError(f"{who}: the target field key {field.key!r} is twice.")
        seen.add(field.key)
        if field.kind not in _KINDS:
            raise ConnectorDeclarationError(
                f"{who}: the target field {field.key!r} has the unknown kind {field.kind!r}."
            )
        if field.kind == "choice" and not field.choices:
            raise ConnectorDeclarationError(
                f"{who}: the target field {field.key!r} is a choice and lists no choices."
            )


def _check_credential_ways(who: str, ways: tuple[CredentialWay, ...]) -> None:
    way_keys: set[str] = set()
    for way in ways:
        if _bad_name(way.key):
            raise ConnectorDeclarationError(
                f"{who}: a credential way has a blank key or a key longer than "
                f"{MAX_NAME_LENGTH} characters."
            )
        if way.key in way_keys:
            raise ConnectorDeclarationError(f"{who}: the credential way key {way.key!r} is twice.")
        way_keys.add(way.key)
        names: set[str] = set()
        for field in way.fields:
            if _bad_name(field.name):
                raise ConnectorDeclarationError(
                    f"{who}: the credential way {way.key!r} has a field with a blank name "
                    f"or a name longer than {MAX_NAME_LENGTH} characters."
                )
            if field.name in names:
                raise ConnectorDeclarationError(
                    f"{who}: the credential way {way.key!r} has the field name "
                    f"{field.name!r} twice."
                )
            names.add(field.name)


def check_connector_declaration(cls: type) -> None:
    """Raise :class:`ConnectorDeclarationError` when the class declares its form wrongly.

    Checks the shape only: names, duplicates and kinds. A class that declares
    nothing passes.
    """
    who = f"Connector {cls.__name__}"
    port = getattr(cls, "default_port", None)
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise ConnectorDeclarationError(f"{who}: default_port must be a whole number or None.")
    if port is not None and not 1 <= port <= 65535:
        raise ConnectorDeclarationError(f"{who}: default_port {port} is not between 1 and 65535.")
    target_fields = getattr(cls, "target_fields", None)
    if target_fields is not None:
        _check_target_fields(who, target_fields)
    credential_ways = getattr(cls, "credential_ways", None)
    if credential_ways is not None:
        _check_credential_ways(who, credential_ways)
    test_operation = getattr(cls, "test_operation", None)
    if test_operation is not None and not str(test_operation).strip():
        raise ConnectorDeclarationError(f"{who}: test_operation is blank.")
