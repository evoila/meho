# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The object as it is now, on the approval card of more Keycloak changes of rights.

These Keycloak writes are changes of rights (:mod:`.rights_changes`). Their
approval card shows what the call asks for. The wrappers here add the
object as it is now, read from Keycloak when the request waits, so the
approver sees the difference:

=========================================  ===================  ==========================
op_id                                      card key             what it holds
=========================================  ===================  ==========================
``keycloak.role_mapping.assign``           ``current_roles``    the realm roles the user
                                                                has directly now
``keycloak.role_mapping.client.assign``    ``current_roles``    the roles of the client
                                                                the user has directly now
``keycloak.group.member.add``              ``current_group``    the group's members now and
                                                                the roles it gives directly
``keycloak.user.reset_password``           ``current_user``     the user as stored: user
                                                                name, enabled, required
                                                                actions
``keycloak.user.enable``                   ``current_user``     the same
``keycloak.authflow.ensure_passkey_``      ``current_binding``  the realm's browser flow
``code_skip``
``keycloak.authflow.ensure_approval_``     ``current_binding``  the client's browser flow
``flow`` and ``..._approval_touch``                             and the realm's one
=========================================  ===================  ==========================

The card says ``<key>_available: true`` with the state under ``<key>``, or
``<key>_available: false`` when MEHO could not read it (no live connection,
an unknown user, group or client, an error, an odd answer). Then the card
also says it in plain words under ``<key>_note`` (:data:`STATE_NOT_READ`),
and MEHO logs ``approval_card_state_not_read``. It never shows an empty
state as if the object had none.

No secret goes on the card. ``current_user`` takes three fields of the user
by name and nothing else: no credential, no password, no attribute.

Each of these operations is registered with
:func:`~meho_backplane.operations.approval_state_pin.register_state_pin`.
Right before the approved run, MEHO builds the card again and compares the
state. When it changed while the request waited, or MEHO cannot read it,
the run is refused and the requester sends the request again. A re-run
after a first run that failed part way is refused the same way when the
first run already changed the object.

For ``keycloak.group.member.add`` the run compares only the group id and
the roles the group gives (:func:`_group_rights`), not the member list.
The new member gets the group's roles; who else is in the group does not
change that. So a big group, or a member who joined or left while the
request waited, does not refuse the run.

The group members are read in pages of :data:`_MEMBER_PAGE`, up to
:data:`~.ops_write_grant.MEMBER_CAP`, and one row more to know whether
there are more. The card lists the first :data:`MEMBERS_SHOWN` by id, the
number read (``member_count``), ``members_complete``, and one line
(``members_not_shown``) such as "950 more members not shown".

The approval request a person reads always shows the state. What the
caller gets back from ``preview_operation`` and from the MCP approval tools
is behind the switch ``KEYCLOAK_RIGHTS_CHANGE_CARDS``
(``Settings.keycloak_rights_change_cards``, off by default): off, the
caller gets the card without these keys, as before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _AUTH_FLOWS_PATH,
    _CLIENT_PATH,
    _GROUP_MEMBERS_PATH,
    _GROUP_ROLE_MAPPINGS_PATH,
    _USER_ROLE_MAPPINGS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow_rights import _shared_read
from meho_backplane.connectors.keycloak.ops_write_grant import MEMBER_CAP, _group
from meho_backplane.connectors.keycloak.ops_write_user_identity import _read_for_card
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._preview import (
    PreviewBuilder,
    PreviewContext,
    register_person_only_preview_keys,
)
from meho_backplane.operations.approval_state_pin import register_state_pin
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

__all__ = [
    "CURRENT_BINDING",
    "CURRENT_GROUP",
    "CURRENT_ROLES",
    "CURRENT_USER",
    "MEMBERS_SHOWN",
    "PINNED_STATE_OPS",
    "STATE_NOT_READ",
    "with_current_binding",
    "with_current_group",
    "with_current_roles",
    "with_current_user",
]

CURRENT_ROLES: Final[str] = "current_roles"
CURRENT_USER: Final[str] = "current_user"
CURRENT_GROUP: Final[str] = "current_group"
CURRENT_BINDING: Final[str] = "current_binding"

#: op_id -> the card key that shows the object as it is now. Each one is
#: compared again right before the approved run.
PINNED_STATE_OPS: Final[dict[str, str]] = {
    "keycloak.role_mapping.assign": CURRENT_ROLES,
    "keycloak.role_mapping.client.assign": CURRENT_ROLES,
    "keycloak.group.member.add": CURRENT_GROUP,
    "keycloak.user.reset_password": CURRENT_USER,
    "keycloak.user.enable": CURRENT_USER,
    "keycloak.authflow.ensure_passkey_code_skip": CURRENT_BINDING,
    "keycloak.authflow.ensure_approval_flow": CURRENT_BINDING,
    "keycloak.authflow.ensure_approval_touch": CURRENT_BINDING,
}

_MEMBER_PAGE: Final[int] = 100

#: How many members the ``current_group`` card lists.
MEMBERS_SHOWN: Final[int] = 50

#: What the card says when MEHO could not read the object as it is now.
STATE_NOT_READ: Final[str] = (
    "MEHO could not read the object as it is now; an approved run will be refused."
)

_log = structlog.get_logger(__name__)

#: Reads the state now for a card; ``None`` when it cannot be read.
_Reader = Callable[[PreviewContext, dict[str, Any]], Awaitable[Any]]


def _cards_on() -> bool:
    return get_settings().keycloak_rights_change_cards


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def _with_state(key: str, read: _Reader) -> Callable[[PreviewBuilder], PreviewBuilder]:
    """Wrap a card builder so the card shows the state *read* returns under *key*."""

    def wrap(builder: PreviewBuilder) -> PreviewBuilder:
        async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
            card = await builder(ctx)
            if card is None or (ctx.agent_preview and not _cards_on()):
                return card
            error: str | None = None
            try:
                state = await read(ctx, card)
            except Exception as exc:
                # The card is still useful without the state; it says so.
                state, error = None, type(exc).__name__
            card[f"{key}_available"] = state is not None
            if state is not None:
                card[key] = state
                return card
            card[f"{key}_note"] = STATE_NOT_READ
            _log.warning(
                "approval_card_state_not_read",
                op_id=ctx.descriptor.op_id,
                connector_id=ctx.connector_id,
                card_key=key,
                error=error,
            )
            return card

        return _wrapped

    return wrap


def _role_names(rows: Any) -> list[str] | None:
    """The sorted names of a list of roles; ``None`` when it is not such a list."""
    if not isinstance(rows, list):
        return None
    names = [_text(row.get("name")) if isinstance(row, dict) else None for row in rows]
    if any(name is None for name in names):
        return None
    return sorted(name for name in names if name is not None)


def _client_mappings(mappings: dict[str, Any]) -> dict[str, list[str]] | None:
    """``clientMappings`` as client id -> sorted role names; ``None`` when odd."""
    raw = mappings.get("clientMappings", {})
    if not isinstance(raw, dict):
        return None
    clients: dict[str, list[str]] = {}
    for client_id, entry in raw.items():
        names = _role_names(entry.get("mappings", []) if isinstance(entry, dict) else None)
        if names is None:
            return None
        clients[str(client_id)] = names
    return clients


async def _read_user_roles(ctx: PreviewContext, *, client: bool) -> list[str] | None:
    connector = _live_connector(ctx)
    found = await _read_for_card(ctx, "id")
    if connector is None or found is None:
        return None
    realm = resolve_realm_config(ctx.target).managed_realm
    mappings = await connector._get_admin_json(
        ctx.target,
        fill_path(_USER_ROLE_MAPPINGS_PATH, {"realm": realm, "user-id": found[0]}),
        operator=ctx.operator,
    )
    if not isinstance(mappings, dict):
        return None
    if not client:
        return _role_names(mappings.get("realmMappings", []))
    client_id = _text(ctx.params.get("client_id"))
    clients = _client_mappings(mappings)
    if client_id is None or clients is None:
        return None
    return clients.get(client_id, [])


def with_current_roles(*, client: bool = False) -> Callable[[PreviewBuilder], PreviewBuilder]:
    """Add ``current_roles``: the realm roles (or, with *client*, the client's roles) now.

    Only the roles given to the user directly, as the write gives them.
    """

    async def read(ctx: PreviewContext, card: dict[str, Any]) -> list[str] | None:
        return await _read_user_roles(ctx, client=client)

    return _with_state(CURRENT_ROLES, read)


async def _read_user(ctx: PreviewContext, card: dict[str, Any]) -> dict[str, Any] | None:
    found = await _read_for_card(ctx, "id")
    if found is None:
        return None
    user = found[1]
    username = _text(user.get("username"))
    enabled = user.get("enabled")
    actions = user.get("requiredActions")
    if username is None or not isinstance(enabled, bool):
        return None
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        return None
    return {"username": username, "enabled": enabled, "required_actions": list(actions)}


#: Add ``current_user``: the user name, enabled or not, and the required actions now.
with_current_user: Final = _with_state(CURRENT_USER, _read_user)


async def _read_members(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, group_id: str
) -> tuple[list[dict[str, str]], bool] | None:
    """Up to :data:`MEMBER_CAP` members (``id``, ``username``), and whether that is all.

    After a full last page at the cap, one more row tells whether there
    are more members.
    """
    path = fill_path(_GROUP_MEMBERS_PATH, {"realm": realm, "group-id": group_id})

    async def page(first: int, size: int) -> list[dict[str, str]] | None:
        rows = await connector._get_admin_list(
            ctx.target,
            path,
            operator=ctx.operator,
            params={"first": str(first), "max": str(size), "briefRepresentation": "true"},
            strict=True,
        )
        out: list[dict[str, str]] = []
        for row in rows:
            member_id = _text(row.get("id")) if isinstance(row, dict) else None
            username = _text(row.get("username")) if isinstance(row, dict) else None
            if member_id is None or username is None:
                return None
            out.append({"id": member_id, "username": username})
        return out

    members: list[dict[str, str]] = []
    while len(members) < MEMBER_CAP:
        size = min(_MEMBER_PAGE, MEMBER_CAP - len(members))
        rows = await page(len(members), size)
        if rows is None:
            return None
        members.extend(rows)
        if len(rows) < size:
            return sorted(members, key=lambda m: m["id"]), True
    more = await page(MEMBER_CAP, 1)
    if more is None:
        return None
    return sorted(members, key=lambda m: m["id"]), not more


def _not_shown(count: int, complete: bool) -> str | None:
    """The line under the listed members, or ``None`` when every member is listed."""
    hidden = count - MEMBERS_SHOWN
    if complete and hidden <= 0:
        return None
    if complete:
        return f"{hidden} more members not shown"
    return f"More than {hidden} more members not shown"


async def _read_group(ctx: PreviewContext, card: dict[str, Any]) -> dict[str, Any] | None:
    connector = _live_connector(ctx)
    if connector is None:
        return None
    group_id = await _group(ctx, "group_id", "group_name")
    if group_id is None:
        return None
    realm = resolve_realm_config(ctx.target).managed_realm
    members = await _read_members(connector, ctx, realm, group_id)
    mappings = await connector._get_admin_json(
        ctx.target,
        fill_path(_GROUP_ROLE_MAPPINGS_PATH, {"realm": realm, "group-id": group_id}),
        operator=ctx.operator,
    )
    if members is None or not isinstance(mappings, dict):
        return None
    realm_roles = _role_names(mappings.get("realmMappings", []))
    client_roles = _client_mappings(mappings)
    if realm_roles is None or client_roles is None:
        return None
    listed, complete = members
    return {
        "id": group_id,
        "members": listed[:MEMBERS_SHOWN],
        "member_count": len(listed),
        "members_complete": complete,
        "members_not_shown": _not_shown(len(listed), complete),
        "realm_roles": realm_roles,
        "client_roles": client_roles,
    }


#: Add ``current_group``: the group's members now and the roles it gives directly.
with_current_group: Final = _with_state(CURRENT_GROUP, _read_group)


async def _flow_aliases(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str
) -> dict[str, str]:
    rows = await connector._get_admin_list(
        ctx.target,
        fill_path(_AUTH_FLOWS_PATH, {"realm": realm}),
        operator=ctx.operator,
        strict=True,
    )
    return {
        row["id"]: row["alias"]
        for row in rows
        if isinstance(row, dict) and _text(row.get("id")) and _text(row.get("alias"))
    }


async def _signed_in(connector: KeycloakConnector, ctx: PreviewContext) -> bool:
    """Whether MEHO holds an admin token for the target, after the one shared sign-in.

    The login flow ops read the account's rights from the admin token once
    per request (:mod:`.ops_authflow_rights`). When that sign-in failed (for
    example a wrong password), the card does not try again: a second failed
    sign-in can lock the account.
    """
    await _shared_read(ctx, connector)
    return target_cache_key(ctx.target) in connector._admin_tokens


async def _read_binding(
    ctx: PreviewContext, card: dict[str, Any], *, client: bool
) -> dict[str, Any] | None:
    connector = _live_connector(ctx)
    if connector is None or not await _signed_in(connector, ctx):
        return None
    realm = resolve_realm_config(ctx.target).managed_realm
    realm_row = await connector._get_admin_json(
        ctx.target, fill_path(_ADMIN_REALM_PATH, {"realm": realm}), operator=ctx.operator
    )
    realm_flow = _text(realm_row.get("browserFlow")) if isinstance(realm_row, dict) else None
    if realm_flow is None:
        return None
    if not client:
        return {"realm_browser_flow": realm_flow}
    client_id = _text(card.get("client_id"))
    if client_id is None:
        return None
    client_uuid = await connector._find_client_uuid(
        ctx.target, realm, client_id, operator=ctx.operator
    )
    if client_uuid is None:
        return None
    client_row = await connector._get_admin_json(
        ctx.target,
        fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": client_uuid}),
        operator=ctx.operator,
    )
    if not isinstance(client_row, dict):
        return None
    overrides = client_row.get("authenticationFlowBindingOverrides", {})
    if not isinstance(overrides, dict):
        return None
    flow_id = _text(overrides.get("browser"))
    alias = (await _flow_aliases(connector, ctx, realm)).get(flow_id) if flow_id else None
    return {
        "realm_browser_flow": realm_flow,
        "client_browser_flow_id": flow_id,
        "client_browser_flow": alias,
    }


def with_current_binding(*, client: bool = False) -> Callable[[PreviewBuilder], PreviewBuilder]:
    """Add ``current_binding``: the realm's browser flow, and with *client* the client's.

    A client without its own browser flow shows ``client_browser_flow_id:
    None``: its logins use the realm's flow.
    """

    async def read(ctx: PreviewContext, card: dict[str, Any]) -> dict[str, Any] | None:
        return await _read_binding(ctx, card, client=client)

    return _with_state(CURRENT_BINDING, read)


def _group_rights(state: Any) -> Any:
    """The part of a ``current_group`` state the run compares: the id and the roles."""
    if not isinstance(state, dict):
        return state
    return {k: state.get(k) for k in ("id", "realm_roles", "client_roles")}


#: op_id -> the part of its state the run compares, when not the whole state.
_COMPARED: Final[dict[str, Callable[[Any], Any]]] = {
    "keycloak.group.member.add": _group_rights,
}


def _register() -> None:
    for op_id, key in PINNED_STATE_OPS.items():
        register_state_pin(op_id, key, compared=_COMPARED.get(op_id))
        register_person_only_preview_keys(
            op_id, _cards_on, frozenset({key, f"{key}_available", f"{key}_note"})
        )


_register()
