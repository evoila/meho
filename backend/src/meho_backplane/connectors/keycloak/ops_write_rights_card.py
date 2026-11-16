# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval cards that show the object as it is now and the new values.

The Keycloak writes that change an object which already exists, and that
are changes of rights (:mod:`.rights_changes`), show the approver both
sides on the approval card:

* ``keycloak.client.update``: ``current_values`` holds the client's values
  of each field the request sends, read from Keycloak when the request
  waits; ``new_values`` holds the values the request sends. For a map
  field (such as ``attributes``), the write changes only the keys it
  sends, so ``current_values`` shows only those keys too.
* ``keycloak.realm.update``: the same, for the realm. The card of
  :mod:`.ops_write_realm_smtp` (realm, requested fields, where the SMTP
  password comes from) gets ``current_values`` added.
* ``keycloak.protocol_mapper.create``: ``current_mappers`` lists the
  mappers the client has now (name, mapper type and settings);
  ``new_mapper`` is the mapper the request adds.

The writes that make a new object (realm, client, client scope, user)
have nothing to read yet: their card shows the new values only.

Secret values are hidden on both sides: first by the Keycloak list of
secret fields (a client ``secret``, an SMTP ``password``), then by the same
two passes as the generic params echo (secret-named keys and
secret-shaped text).

When MEHO cannot read the object (no live connection, an unknown client,
an error), the card says ``current_values_available: false`` (or
``current_mappers_available: false``) and shows the new values only. It
never shows an empty "now" side as if the object had no values.

The cards are behind the switch ``KEYCLOAK_RIGHTS_CHANGE_CARDS``
(``Settings.keycloak_rights_change_cards``, off by default). Off,
``keycloak.client.update`` and ``keycloak.protocol_mapper.create`` show
the generic params echo, and ``keycloak.realm.update`` shows the realm
card of :mod:`.ops_write_realm_smtp` without ``current_values``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _CLIENT_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_realm_smtp import (
    REALM_UPDATE_OP_ID,
    _realm_label,
    _realm_update_preview,
)
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import (
    PreviewContext,
    _redact_echoed_params,
    register_preview_builder,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

__all__ = ["CLIENT_UPDATE_OP_ID", "PROTOCOL_MAPPER_CREATE_OP_ID"]

CLIENT_UPDATE_OP_ID = "keycloak.client.update"
PROTOCOL_MAPPER_CREATE_OP_ID = "keycloak.protocol_mapper.create"


def _cards_on() -> bool:
    return get_settings().keycloak_rights_change_cards


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _representation(ctx: PreviewContext) -> dict[str, Any]:
    representation = ctx.params.get("representation")
    return representation if isinstance(representation, dict) else {}


def _hide(ctx: PreviewContext, value: Any) -> Any:
    """*value* with every secret hidden, the way the realm update card hides it."""
    tenant = str(ctx.operator.tenant_id) if ctx.operator.tenant_id is not None else None
    hidden = _redact_echoed_params(
        {"value": redact_secret_fields(value)},
        connector_id=ctx.connector_id,
        tenant=tenant,
        op_id=ctx.descriptor.op_id,
    )
    return hidden.get("value")


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def _managed_realm(ctx: PreviewContext) -> str | None:
    if ctx.target is None:
        return None
    try:
        return resolve_realm_config(ctx.target).managed_realm
    except Exception:
        return None


async def _client_uuid(ctx: PreviewContext, connector: KeycloakConnector, realm: str) -> str | None:
    """The client the handler will change: ``id``, else ``client_id`` looked up."""
    client_uuid = _text(ctx.params.get("id"))
    if client_uuid is not None:
        return client_uuid
    client_id = _text(ctx.params.get("client_id"))
    if client_id is None:
        return None
    return await connector._find_client_uuid(ctx.target, realm, client_id, operator=ctx.operator)


async def _read_client(ctx: PreviewContext) -> tuple[str | None, dict[str, Any] | None]:
    """The client's UUID and its representation now; ``None`` for what cannot be read."""
    connector = _live_connector(ctx)
    realm = _managed_realm(ctx)
    if connector is None or realm is None:
        return _text(ctx.params.get("id")), None
    try:
        client_uuid = await _client_uuid(ctx, connector, realm)
        if client_uuid is None:
            return None, None
        client = await connector._get_admin_json(
            ctx.target,
            fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": quote_segment(client_uuid)}),
            operator=ctx.operator,
        )
    except Exception:
        # The card is still useful with the new values only; it says so.
        return _text(ctx.params.get("id")), None
    return client_uuid, client if isinstance(client, dict) else None


async def _read_realm(ctx: PreviewContext) -> dict[str, Any] | None:
    connector = _live_connector(ctx)
    realm = _realm_label(ctx)
    if connector is None or realm is None:
        return None
    try:
        current = await connector._get_admin_json(
            ctx.target,
            fill_path(_ADMIN_REALM_PATH, {"realm": quote_segment(realm)}),
            operator=ctx.operator,
        )
    except Exception:
        return None
    return current if isinstance(current, dict) else None


def _add_current_values(
    card: dict[str, Any],
    ctx: PreviewContext,
    current: dict[str, Any] | None,
    requested: dict[str, Any],
) -> dict[str, Any]:
    """Add the object's values now of each field in *requested*; ``None`` means not set."""
    if current is None:
        card["current_values_available"] = False
        return card
    card["current_values_available"] = True
    card["current_values"] = _hide(
        ctx, {key: _value_now(current.get(key), sent) for key, sent in requested.items()}
    )
    return card


def _value_now(now: Any, sent: Any) -> Any:
    """The value now of a field; for a map field, only the keys the request sends."""
    if isinstance(now, dict) and isinstance(sent, dict):
        return {key: now.get(key) for key in sent}
    return now


async def _client_update_card(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for ``keycloak.client.update``: the client, its values now, the new values."""
    requested = _representation(ctx)
    client_uuid, current = await _read_client(ctx)
    card: dict[str, Any] = {
        "resource": "client",
        "id": client_uuid,
        "client_id": _text(ctx.params.get("client_id")),
        "realm": _managed_realm(ctx),
        "new_values": _hide(ctx, requested),
    }
    return _add_current_values(card, ctx, current, requested)


async def _realm_update_card(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for ``keycloak.realm.update``: the realm card plus the realm's values now."""
    card = await _realm_update_preview(ctx)
    if card is None or not _cards_on():
        return card
    return _add_current_values(card, ctx, await _read_realm(ctx), _representation(ctx))


def _mapper_summary(ctx: PreviewContext, mapper: Any) -> dict[str, Any]:
    row = mapper if isinstance(mapper, dict) else {}
    return {
        "name": row.get("name"),
        "protocol_mapper": row.get("protocolMapper"),
        "config": _hide(ctx, row.get("config") or {}),
    }


async def _protocol_mapper_create_card(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for ``keycloak.protocol_mapper.create``: the client's mappers now and the new one."""
    client_uuid, client = await _read_client(ctx)
    card: dict[str, Any] = {
        "resource": "protocol_mapper",
        "id": client_uuid,
        "client_id": _text(ctx.params.get("client_id")),
        "realm": _managed_realm(ctx),
        "new_mapper": _hide(ctx, _representation(ctx)),
    }
    mappers = client.get("protocolMappers", []) if client is not None else None
    if not isinstance(mappers, list):
        card["current_mappers_available"] = False
        return card
    card["current_mappers_available"] = True
    card["current_mappers"] = [_mapper_summary(ctx, mapper) for mapper in mappers]
    return card


register_preview_builder(CLIENT_UPDATE_OP_ID, _client_update_card, enabled=_cards_on)
register_preview_builder(REALM_UPDATE_OP_ID, _realm_update_card)
register_preview_builder(
    PROTOCOL_MAPPER_CREATE_OP_ID, _protocol_mapper_create_card, enabled=_cards_on
)
