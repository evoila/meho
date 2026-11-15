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

A request that gives both an id and a name for the scope (or for the
mapper) must name one object: the name stored in Keycloak for that id must
be the name sent. Otherwise the request is refused before it waits for
approval, and again when it runs. Nothing is written.

The approval card shows the ids and the names stored in Keycloak, also when
the caller sent only one of them. After approval, the op acts on the ids on
the card. It does not look a name up again. When the stored name of an id
is no longer the one on the card, the run is refused. A card without both
ids and both names is refused too.

Safe to run twice: the same fields written again is a success.

The op-metadata row is in
:mod:`~meho_backplane.connectors.keycloak.ops_write_scope_mapper_schemas`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_SCOPE_PATH,
    _CLIENT_SCOPE_PROTOCOL_MAPPER_PATH,
    _CLIENT_SCOPE_PROTOCOL_MAPPERS_PATH,
    _CLIENT_SCOPES_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import get_or_none
from meho_backplane.connectors.keycloak.ops_write_merge import read_merge_write
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._park_check import register_park_check
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

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


_ID_NAME_KEYS = (
    ("scope_id", "scope_name", "client scope"),
    ("mapper_id", "mapper_name", "protocol mapper"),
)


@dataclass(frozen=True)
class _Resolved:
    """The scope and the mapper of a request, with the names stored in Keycloak."""

    scope_id: str
    scope_name: str | None
    mapper_id: str
    mapper_name: str | None
    mapper_path: str
    mapper: dict[str, Any]


def _check_name(
    stored: Any, asked: str | None, *, keys: tuple[str, str, str], approved: bool
) -> None:
    """Raise :exc:`ValueError` when *asked* is set and is not the stored name."""
    if asked is None or _opt_str(stored) == asked:
        return
    id_key, name_key, what = keys
    if approved:
        raise ValueError(
            f"{_OP_ID}: the {what} on the approval card has another name now. "
            "Nothing was changed. Check it and send a new request."
        )
    raise ValueError(
        f"{_OP_ID}: {id_key!r} and {name_key!r} name different {what}s. "
        f"Nothing was changed. Send the request with one {what}."
    )


async def _resolve_scope(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    params: dict[str, Any],
    *,
    approved: bool,
) -> tuple[str, str | None]:
    """Return the scope's ``(id, stored name)``."""
    scope_id = _opt_str(params.get("scope_id"))
    name = _opt_str(params.get("scope_name"))
    if scope_id is None:
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
        return found, name
    path = fill_path(
        _CLIENT_SCOPE_PATH, {"realm": realm, "client-scope-id": quote_segment(scope_id)}
    )
    scope = await get_or_none(connector, target, path, operator)
    if scope is None:
        raise KeycloakUserNotFoundError(
            f"{_OP_ID}: no client scope with id={scope_id!r} in realm {realm!r}"
        )
    _check_name(scope.get("name"), name, keys=_ID_NAME_KEYS[0], approved=approved)
    return scope_id, _opt_str(scope.get("name"))


async def _resolve(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    approved: bool = False,
) -> _Resolved:
    """Find the scope and the mapper, read the mapper, and check the names.

    Raises :exc:`KeycloakUserNotFoundError` when one does not exist, and
    :exc:`ValueError` when a name sent with an id is not the stored name.
    """
    realm = resolve_realm_config(target).managed_realm
    scope_id, scope_name = await _resolve_scope(
        connector, operator, target, realm, params, approved=approved
    )
    mapper_id = _opt_str(params.get("mapper_id"))
    name = _opt_str(params.get("mapper_name"))
    if mapper_id is None:
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
        mapper_id = _pick_id_by_name(rows, name, what="protocol mapper")
        if mapper_id is None:
            raise KeycloakUserNotFoundError(
                f"{_OP_ID}: no protocol mapper named {name!r} on client scope {scope_id!r} "
                f"in realm {realm!r}"
            )
    path = fill_path(
        _CLIENT_SCOPE_PROTOCOL_MAPPER_PATH,
        {
            "realm": realm,
            "client-scope-id": quote_segment(scope_id),
            "id": quote_segment(mapper_id),
        },
    )
    mapper = await get_or_none(connector, target, path, operator)
    if mapper is None:
        raise KeycloakUserNotFoundError(
            f"{_OP_ID}: no protocol mapper with id={mapper_id!r} on client scope "
            f"{scope_id!r} in realm {realm!r}"
        )
    _check_name(mapper.get("name"), name, keys=_ID_NAME_KEYS[1], approved=approved)
    return _Resolved(
        scope_id=scope_id,
        scope_name=scope_name,
        mapper_id=mapper_id,
        mapper_name=_opt_str(mapper.get("name")),
        mapper_path=path,
        mapper=mapper,
    )


def _approved_ids(params: dict[str, Any]) -> dict[str, Any] | None:
    """The request with the ids and names on the approval card, during the approved run.

    ``None`` when the call is not the approved run of a request. A card
    without both ids and both names is refused: MEHO cannot tell which
    objects the approver saw.
    """
    card = approved_preview(_OP_ID)
    if card is None:
        return None
    pinned = {key: _opt_str(card.get(key)) for keys in _ID_NAME_KEYS for key in keys[:2]}
    if any(value is None for value in pinned.values()):
        raise ValueError(
            f"{_OP_ID}: the approval card does not show the ids and names of the client "
            "scope and the mapper, so MEHO cannot tell what was approved. Nothing was "
            "changed. Send a new request."
        )
    return {**params, **pinned}


async def keycloak_client_scope_protocol_mapper_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Change one protocol mapper of a client scope (read, merge, ``PUT``).

    Op-id: ``keycloak.client_scope.protocol_mapper.update``. ``representation``
    holds only the fields to change. The result names the scope and the
    mapper (ids and the names stored in Keycloak) and lists the fields that
    changed, by name only. It never returns a config value.
    """
    representation = params.get("representation")
    if not isinstance(representation, dict) or not representation:
        raise ValueError(f"{_OP_ID} requires a non-empty 'representation' object")
    if "id" in representation:
        raise ValueError(f"{_OP_ID}: the mapper 'id' cannot be changed; remove it")
    pinned = _approved_ids(params)
    found = await _resolve(self, operator, target, pinned or params, approved=pinned is not None)
    try:
        check = await read_merge_write(
            self,
            operator,
            target,
            path=found.mapper_path,
            requested=representation,
            before=found.mapper,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise KeycloakUserNotFoundError(
                f"{_OP_ID}: no protocol mapper with id={found.mapper_id!r} on client scope "
                f"{found.scope_id!r}"
            ) from exc
        raise
    return {
        "scope_id": found.scope_id,
        "scope_name": found.scope_name,
        "mapper_id": found.mapper_id,
        "mapper_name": found.mapper_name,
        "changed_fields": check["changed_fields"],
        "unrequested_changes": check["unrequested_changes"],
        "read_back_checked": check["read_back_checked"],
    }


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if isinstance(connector, KeycloakConnector) and ctx.target is not None:
        return connector
    return None


async def _scope_mapper_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card: the scope and the mapper (ids and stored names) and the fields to change.

    When the objects cannot be read (no live connector, an unknown name, a
    fault), the card still shows the request and says
    ``current_available: false``. The approved run then refuses, because the
    card has no ids.
    """
    representation = ctx.params.get("representation")
    representation = representation if isinstance(representation, dict) else {}
    preview: dict[str, Any] = {
        "resource": "client_scope_protocol_mapper",
        "scope_id": _opt_str(ctx.params.get("scope_id")),
        "scope_name": _opt_str(ctx.params.get("scope_name")),
        "mapper_id": _opt_str(ctx.params.get("mapper_id")),
        "mapper_name": _opt_str(ctx.params.get("mapper_name")),
        "changed_fields": sorted(representation),
        "representation": redact_secret_fields(representation),
        "current_available": False,
    }
    connector = _live_connector(ctx)
    if connector is None:
        return preview
    try:
        preview["realm"] = resolve_realm_config(ctx.target).managed_realm
        found = await _resolve(connector, ctx.operator, ctx.target, ctx.params)
    except Exception:
        return preview
    preview.update(
        scope_id=found.scope_id,
        scope_name=found.scope_name,
        mapper_id=found.mapper_id,
        mapper_name=found.mapper_name,
        current_available=True,
    )
    return preview


async def _refuse_names_of_other_objects(ctx: PreviewContext) -> str | None:
    """Refuse a request whose id and name name different scopes or mappers.

    Runs before the request waits for approval, and only when an id and a
    name are both sent. An object that does not exist is let through: the
    run then fails with "not found". Without a live Keycloak connector MEHO
    cannot check, so it refuses.
    """
    if not any(
        _opt_str(ctx.params.get(id_key)) and _opt_str(ctx.params.get(name_key))
        for id_key, name_key, _ in _ID_NAME_KEYS
    ):
        return None
    connector = _live_connector(ctx)
    if connector is None:
        return (
            f"{_OP_ID}: MEHO could not check that each id and name sent name the same "
            "object. Send only one of them."
        )
    try:
        await _resolve(connector, ctx.operator, ctx.target, ctx.params)
    except KeycloakUserNotFoundError:
        return None
    except ValueError as exc:
        return str(exc)
    return None


register_park_check(_OP_ID, _refuse_names_of_other_objects)
register_preview_builder(_OP_ID, _scope_mapper_preview)
