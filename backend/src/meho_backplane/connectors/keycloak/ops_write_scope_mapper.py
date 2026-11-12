# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change one protocol mapper of a client scope: ``keycloak.client_scope.protocol_mapper.update``.

Example: add one more value to the hardcoded-claim mapper that lists the
document collections a person may open. The op finds the scope by id or
exact name and the mapper by id or exact name (both inside the managed
realm), reads the mapper, puts the caller's fields on top and writes the
whole mapper back
(:func:`~meho_backplane.connectors.keycloak.ops_write_merge.read_merge_write`).
A field the caller does not send stays as it was. The ``config`` map merges
key by key, so sending ``{"config": {"claim.value": "..."}}`` changes that
one key and keeps the others.

A name that matches no scope or mapper raises
:exc:`~meho_backplane.connectors.keycloak.ops_write.KeycloakUserNotFoundError`
(the connector's shared "not found" error) and nothing is written. A name
that matches more than one raises :exc:`ValueError`. The mapper's ``id``
cannot be changed.

Safe to run twice: the same fields written again is a success.

The op-metadata row is in
:mod:`~meho_backplane.connectors.keycloak.ops_write_scope_mapper_schemas`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_SCOPE_PROTOCOL_MAPPER_PATH,
    _CLIENT_SCOPE_PROTOCOL_MAPPERS_PATH,
    _CLIENT_SCOPES_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_merge import read_merge_write
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["keycloak_client_scope_protocol_mapper_update"]

_OP_ID = "keycloak.client_scope.protocol_mapper.update"


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def _pick_id_by_name(rows: list[Any], name: str, *, what: str) -> str | None:
    """Return the id of the one row named *name* (exact match), or ``None``."""
    ids = [
        row["id"]
        for row in rows
        if isinstance(row, dict)
        and row.get("name") == name
        and isinstance(row.get("id"), str)
        and row["id"]
    ]
    if len(ids) > 1:
        raise ValueError(f"{_OP_ID}: more than one {what} is named {name!r}; pass its id instead")
    return ids[0] if ids else None


async def _scope_id(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    params: dict[str, Any],
) -> str:
    scope_id = _opt_str(params.get("scope_id"))
    if scope_id is not None:
        return scope_id
    name = _opt_str(params.get("scope_name"))
    if name is None:
        raise ValueError(f"{_OP_ID} requires either 'scope_id' (UUID) or 'scope_name'")
    rows = await connector._get_admin_list(
        target, fill_path(_CLIENT_SCOPES_PATH, {"realm": realm}), operator=operator, strict=True
    )
    found = _pick_id_by_name(rows, name, what="client scope")
    if found is None:
        raise KeycloakUserNotFoundError(
            f"{_OP_ID}: no client scope named {name!r} in realm {realm!r}"
        )
    return found


async def _mapper_id(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    scope_id: str,
    params: dict[str, Any],
) -> str:
    mapper_id = _opt_str(params.get("mapper_id"))
    if mapper_id is not None:
        return mapper_id
    name = _opt_str(params.get("mapper_name"))
    if name is None:
        raise ValueError(f"{_OP_ID} requires either 'mapper_id' (UUID) or 'mapper_name'")
    rows = await connector._get_admin_list(
        target,
        fill_path(
            _CLIENT_SCOPE_PROTOCOL_MAPPERS_PATH,
            {"realm": realm, "client-scope-id": quote_segment(scope_id)},
        ),
        operator=operator,
        strict=True,
    )
    found = _pick_id_by_name(rows, name, what="protocol mapper")
    if found is None:
        raise KeycloakUserNotFoundError(
            f"{_OP_ID}: no protocol mapper named {name!r} on client scope {scope_id!r} "
            f"in realm {realm!r}"
        )
    return found


async def keycloak_client_scope_protocol_mapper_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Change one protocol mapper of a client scope (read, merge, ``PUT``).

    Op-id: ``keycloak.client_scope.protocol_mapper.update``. ``representation``
    holds only the fields to change. The result names the scope and the
    mapper and lists the fields that changed, by name only. It never returns
    a config value.
    """
    realms = resolve_realm_config(target)
    realm = realms.managed_realm
    representation = params.get("representation")
    if not isinstance(representation, dict) or not representation:
        raise ValueError(f"{_OP_ID} requires a non-empty 'representation' object")
    if "id" in representation:
        raise ValueError(f"{_OP_ID}: the mapper 'id' cannot be changed; remove it")
    scope_id = await _scope_id(self, operator, target, realm, params)
    mapper_id = await _mapper_id(self, operator, target, realm, scope_id, params)
    path = fill_path(
        _CLIENT_SCOPE_PROTOCOL_MAPPER_PATH,
        {
            "realm": realm,
            "client-scope-id": quote_segment(scope_id),
            "id": quote_segment(mapper_id),
        },
    )
    try:
        check = await read_merge_write(self, operator, target, path=path, requested=representation)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise KeycloakUserNotFoundError(
                f"{_OP_ID}: no protocol mapper with id={mapper_id!r} on client scope "
                f"{scope_id!r} in realm {realm!r}"
            ) from exc
        raise
    return {
        "scope_id": scope_id,
        "scope_name": _opt_str(params.get("scope_name")),
        "mapper_id": mapper_id,
        "mapper_name": _opt_str(params.get("mapper_name")),
        "changed_fields": check["changed_fields"],
        "unrequested_changes": check["unrequested_changes"],
        "read_back_checked": check["read_back_checked"],
    }
