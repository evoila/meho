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
that matches more than one raises :exc:`ValueError`.

``representation`` may hold only ``name`` and ``config``. A change of the
mapper's type (``protocolMapper``) or ``protocol`` makes another kind of
mapper, whose config keys mean other things. The approver then could not
compare the old and the new values on the card. So this op does not do it.

A request that gives both an id and a name for the scope (or for the
mapper) must name one object: the name stored in Keycloak for that id must
be the name sent. Otherwise the request is refused before it waits for
approval, and again when it runs. Nothing is written.

The approval card shows the ids and the names stored in Keycloak, also when
the caller sent only one of them. It also shows the mapper as it is now:
its type, protocol and claim name, and the old value of each field sent
(secrets hidden). After approval, the op acts on the ids on the card. It
does not look a name up again. The run is refused when the stored name of
an id is no longer the one on the card, or when the mapper's type,
protocol or config is no longer the one the card was made from. A card
without both ids, both names and that content is refused too.

The op is a change of rights: a mapper decides what goes into a login
token. So the requester never approves it, and the approver is another
admin (:func:`~meho_backplane.operations.grant_approval.register_rights_change_op`).
In MEHO's own login realm the approver is a platform admin when the mapper
puts the platform admin claim or role into tokens now or after the change,
or when MEHO cannot read the mapper (:mod:`.ops_write_platform_reach`).
A person approves each call: no standing grant and no run approval covers
it (:func:`~meho_backplane.op_classes.declare_per_call_approval`).

MEHO refuses the change, before approval and at the run, when a client MEHO
itself signs in with has the scope as a default or optional scope
(:func:`~.ops_meho_own_login_guard.refuse_scope_mapper_write`).

Safe to run twice: the same fields written again is a success.

The op-metadata row is in
:mod:`~meho_backplane.connectors.keycloak.ops_write_scope_mapper_schemas`.
"""

from __future__ import annotations

import hashlib
import json
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
from meho_backplane.connectors.keycloak.ops_meho_own_login_guard import (
    refuse_scope_mapper_write,
    scope_mapper_park_reason,
)
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import get_or_none
from meho_backplane.connectors.keycloak.ops_write_merge import read_merge_write
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_MAPPER,
    hide_grant_from_agent,
    mapper_sets_platform_admin,
    with_platform_facts,
)
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.op_classes import declare_per_call_approval
from meho_backplane.operations._park_check import register_park_check
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)
from meho_backplane.operations.grant_approval import register_rights_change_op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["keycloak_client_scope_protocol_mapper_update"]

_OP_ID = "keycloak.client_scope.protocol_mapper.update"

#: The fields ``representation`` may change.
_CHANGEABLE = frozenset({"name", "config"})


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
    path = fill_path(_CLIENT_SCOPE_PATH, {"realm": realm, "client-scope-id": scope_id})
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
                {"realm": realm, "client-scope-id": scope_id},
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
            "client-scope-id": scope_id,
            "id": mapper_id,
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


def _content_hash(mapper: dict[str, Any]) -> str:
    """A SHA-256 of the mapper's type, protocol and config, to tell a later change."""
    content = {key: mapper.get(key) for key in ("protocolMapper", "protocol", "config")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def _approved_card(params: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The request with the ids and names on the approval card, and the card's content hash.

    ``None`` when the call is not the approved run of a request. A card
    without both ids, both names and the content hash is refused: MEHO
    cannot tell what the approver saw.
    """
    card = approved_preview(_OP_ID)
    if card is None:
        return None
    pinned = {key: _opt_str(card.get(key)) for keys in _ID_NAME_KEYS for key in keys[:2]}
    content = _opt_str(card.get("mapper_content_hash"))
    if content is None or any(value is None for value in pinned.values()):
        raise ValueError(
            f"{_OP_ID}: the approval card does not show the client scope and the mapper "
            "as they were, so MEHO cannot tell what was approved. Nothing was "
            "changed. Send a new request."
        )
    return {**params, **pinned}, content


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
    if not set(representation) <= _CHANGEABLE:
        raise ValueError(
            f"{_OP_ID}: 'representation' may change only 'name' and 'config'; remove "
            f"{sorted(set(representation) - _CHANGEABLE)}"
        )
    approved = _approved_card(params)
    found = await _resolve(
        self, operator, target, approved[0] if approved else params, approved=approved is not None
    )
    if approved is not None and _content_hash(found.mapper) != approved[1]:
        raise ValueError(
            f"{_OP_ID}: the mapper changed after the approval: its type, protocol or "
            "config is not the one on the approval card. Nothing was changed. A new "
            "request is needed."
        )
    await refuse_scope_mapper_write(
        self, operator, target, resolve_realm_config(target).managed_realm, found.scope_name
    )
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


def _current(mapper: dict[str, Any], representation: dict[str, Any]) -> dict[str, Any]:
    """The mapper as it is now: type, protocol, claim name, and the old value of each field sent."""
    raw_config = mapper.get("config")
    config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    before: dict[str, Any] = {}
    for key, value in representation.items():
        if key == "config" and isinstance(value, dict):
            before["config"] = {name: config.get(name) for name in value}
        else:
            before[key] = mapper.get(key)
    return {
        "protocolMapper": mapper.get("protocolMapper"),
        "protocol": mapper.get("protocol"),
        "claim_name": config.get("claim.name"),
        "before": redact_secret_fields(before),
    }


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if isinstance(connector, KeycloakConnector) and ctx.target is not None:
        return connector
    return None


async def _scope_mapper_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card: the scope and the mapper (ids and stored names), the mapper now, and the change.

    ``current`` holds the mapper's type, protocol and claim name, and the old
    value of each field sent; ``representation`` holds the new values (both
    with secrets hidden). ``mapper_content_hash`` lets the approved run tell
    whether the mapper changed in between. When the objects cannot be read
    (no live connector, an unknown name, a fault), the card still shows the
    request and says ``current_available: false``. The approved run then
    refuses, because the card has no ids and no content hash.
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
        current=_current(found.mapper, representation),
        mapper_content_hash=_content_hash(found.mapper),
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


async def _park_check(ctx: PreviewContext) -> str | None:
    """The check before approval: the names first, then MEHO's own sign-in clients.

    A scope or mapper MEHO cannot find is left to the run, which checks again.
    """
    reason = await _refuse_names_of_other_objects(ctx)
    if reason is not None:
        return reason
    connector = _live_connector(ctx)
    if connector is None:
        return None
    try:
        found = await _resolve(connector, ctx.operator, ctx.target, ctx.params)
    except Exception:
        return None
    return await scope_mapper_park_reason(ctx, found.scope_name)


async def _sets_platform_admin(ctx: PreviewContext, realm: str) -> bool | None:
    """Whether the mapper puts the platform admin claim or role into tokens, now or after it.

    Both count: a change of a mapper that sets the claim now changes who gets
    the claim. ``None`` when MEHO cannot read the mapper.
    """
    connector = _live_connector(ctx)
    if connector is None:
        return None
    found = await _resolve(connector, ctx.operator, ctx.target, ctx.params)
    representation = ctx.params.get("representation")
    sent = representation.get("config") if isinstance(representation, dict) else None
    config = found.mapper.get("config")
    merged = {
        **found.mapper,
        "config": {
            **(config if isinstance(config, dict) else {}),
            **(sent if isinstance(sent, dict) else {}),
        },
    }
    facts = [mapper_sets_platform_admin(found.mapper), mapper_sets_platform_admin(merged)]
    if True in facts:
        return True
    return None if None in facts else False


register_rights_change_op(_OP_ID)
declare_per_call_approval([_OP_ID])
register_park_check(_OP_ID, _park_check)
register_preview_builder(
    _OP_ID,
    with_platform_facts(_OP_ID, _scope_mapper_preview, (_sets_platform_admin, REASON_MAPPER)),
)
hide_grant_from_agent(_OP_ID)
