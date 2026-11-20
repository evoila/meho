# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep Keycloak writes from breaking MEHO's own sign-in clients and login realm.

In MEHO's own login realm
(:func:`~.ops_user_meho_access_guard.is_meho_login_realm`), MEHO refuses:

* ``keycloak.client.delete`` of a client MEHO itself uses;
* ``keycloak.client.update`` of such a client, unless every change is on
  :data:`OWN_CLIENT_CHANGES`;
* ``keycloak.realm.update`` of the login realm, unless every change is on
  :data:`LOGIN_REALM_CHANGES`.

**MEHO's own clients** (:func:`meho_own_client_ids`) come from MEHO's
settings: ``KEYCLOAK_ADMIN_CLIENT_ID``, ``CHECK_RUNNER_CLIENT_ID``,
``KEYCLOAK_AUDIENCE``, the clients of the CLI login and the CLI approval,
the clients of the web console login and the web approval, and the fixed
sign-in clients of the AI tools (``meho-mcp`` and one per tool kind). A
param of the call never adds or removes one. MEHO judges the client as
Keycloak stores it: a client named by its internal id is read first, and
its ``clientId`` is compared without letter case and spaces at the ends.

**A change** is a key whose value after the write differs from the value
Keycloak holds now. A key sent with the value it already has is no change.
A map such as ``attributes`` counts as one key: it changes when one of its
keys changes. The allowed changes are short lists; every other key counts
as a change that can lock MEHO out.

The check runs before the request waits for approval
(:func:`register_meho_own_login_checks`) and again when the call runs, in
the handlers of the three writes. So no approval and no standing grant lets
such a write through. On doubt MEHO refuses at the run: when it cannot read
its settings, or the client or realm comes back in an odd shape. Before
approval, a client or realm MEHO cannot read is left to the run.

Each refusal is a
:exc:`~.ops_user_meho_access_guard.MehoAccessRefusedError` with the code
``meho_own_client``, ``meho_login_realm`` or ``meho_access_not_checked``.
Its text says what was refused and why. It names the keys the caller sent,
never a client the caller did not name, and gives no advice to change
rights.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import (
    NO_APPROVAL,
    MehoAccessRefusedError,
    is_meho_login_realm,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    CLIENT_DELETE_OP,
    find_client,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import ParkCheck, register_park_check
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "CLIENT_UPDATE_OP",
    "LOGIN_REALM_CHANGES",
    "OWN_CLIENT_CHANGES",
    "REALM_UPDATE_OP",
    "blocked_changes",
    "login_realm_park_reason",
    "meho_own_client_ids",
    "refuse_login_realm_write",
    "refuse_own_client_write",
    "register_meho_own_login_checks",
]

CLIENT_UPDATE_OP: Final = "keycloak.client.update"
REALM_UPDATE_OP: Final = "keycloak.realm.update"

_OWN_CLIENT: Final = "meho_own_client"
_LOGIN_REALM: Final = "meho_login_realm"
_NOT_CHECKED: Final = "meho_access_not_checked"
_REFUSED: Final = "this write was refused"

#: Keycloak's own browser login flow; setting it back is the way back from a
#: login flow that a MEHO write set up.
_BUILTIN_BROWSER_FLOW: Final = "browser"

#: Answers whether a change of one key from the old to the new value is allowed.
ChangeCheck = Callable[[Any, Any], bool]


def _any_value(_old: Any, _new: Any) -> bool:
    return True


def _turns_on(_old: Any, new: Any) -> bool:
    return new is True


def _items(value: Any) -> set[str] | None:
    """The items of a list as JSON text; an empty set for no list; ``None`` for another shape."""
    if value is None:
        return set()
    if not isinstance(value, list):
        return None
    return {json.dumps(item, sort_keys=True) for item in value}


def _adds_only(old: Any, new: Any) -> bool:
    """Whether the new list keeps every item of the old list."""
    old_items, new_items = _items(old), _items(new)
    return old_items is not None and new_items is not None and old_items <= new_items


def _unbinds_only(old: Any, new: Any) -> bool:
    """Whether every flow binding that changes is set back to ``""`` (the realm's flow)."""
    old_map = old if isinstance(old, dict) else {} if old is None else None
    if old_map is None or not isinstance(new, dict):
        return False
    return all(value == "" for key, value in new.items() if old_map.get(key) != value)


def _builtin_browser_flow(_old: Any, new: Any) -> bool:
    return bool(new == _BUILTIN_BROWSER_FLOW)


#: The changes of a client MEHO itself uses that stay allowed: its name and
#: description, a new redirect address or web origin (every address it has
#: stays), turning it on, and removing its own login flow so that it uses
#: the realm's flow again.
OWN_CLIENT_CHANGES: Final[dict[str, ChangeCheck]] = {
    "name": _any_value,
    "description": _any_value,
    "redirectUris": _adds_only,
    "webOrigins": _adds_only,
    "enabled": _turns_on,
    "authenticationFlowBindingOverrides": _unbinds_only,
}

#: The changes of MEHO's login realm that stay allowed: names shown on the
#: login page, the mail server, the event settings, the languages, turning
#: the realm on, and setting the browser login flow back to Keycloak's own.
LOGIN_REALM_CHANGES: Final[dict[str, ChangeCheck]] = {
    "displayName": _any_value,
    "displayNameHtml": _any_value,
    "enabled": _turns_on,
    "browserFlow": _builtin_browser_flow,
    "smtpServer": _any_value,
    "eventsEnabled": _any_value,
    "eventsExpiration": _any_value,
    "eventsListeners": _any_value,
    "enabledEventTypes": _any_value,
    "adminEventsEnabled": _any_value,
    "adminEventsDetailsEnabled": _any_value,
    "internationalizationEnabled": _any_value,
    "supportedLocales": _any_value,
    "defaultLocale": _any_value,
}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def meho_own_client_ids() -> frozenset[str]:
    """The client ids (lower case) of the clients MEHO itself signs in with."""
    # Imported late: the auth modules are not needed to load the connector.
    from meho_backplane.auth.tool_clients import MCP_TOOL_CLIENTS
    from meho_backplane.auth.tool_sign_out import OTHER_MCP_CLIENT_ID

    settings = get_settings()
    names = (
        settings.keycloak_admin_client_id,
        settings.check_runner_client_id,
        settings.keycloak_audience,
        settings.keycloak_cli_client_id,
        settings.keycloak_cli_approve_client_id,
        settings.ui_keycloak_client_id,
        settings.ui_keycloak_approve_client_id,
        OTHER_MCP_CLIENT_ID,
        *MCP_TOOL_CLIENTS.values(),
    )
    return frozenset(name.casefold() for name in map(_text, names) if name is not None)


def blocked_changes(
    current: dict[str, Any], requested: dict[str, Any], allowed: dict[str, ChangeCheck]
) -> list[str]:
    """The keys of *requested* that change *current* in a way *allowed* does not list.

    The new value of a key is the one the write sends: a map is merged key
    by key on top of the current map, as the update ops do.
    """
    # Imported late: the merge module imports the connector module.
    from meho_backplane.connectors.keycloak.ops_write_merge import _same, merge_representation

    merged = merge_representation(current, requested)
    blocked = []
    for key in sorted(requested):
        old, new = current.get(key), merged.get(key)
        if key in current and _same(old, new):
            continue
        check = allowed.get(key)
        if check is None or not check(old, new):
            blocked.append(key)
    return blocked


def _not_checked(op_id: str, what: str) -> MehoAccessRefusedError:
    return MehoAccessRefusedError(
        op_id,
        _NOT_CHECKED,
        f"{_REFUSED}: MEHO could not check that this write keeps its own sign-in working "
        f"({what}). Nothing was changed.",
    )


def _is_own_client(op_id: str, client: Any, named: str | None) -> bool:
    try:
        own = meho_own_client_ids()
    except Exception as exc:
        raise _not_checked(op_id, "MEHO could not read its settings") from exc
    stored = _text(client.get("clientId")) if isinstance(client, dict) else None
    if stored is None:
        raise _not_checked(op_id, "the client has no client id")
    names = {stored.casefold()}
    if named is not None:
        names.add(named.casefold())
    return not names.isdisjoint(own)


def refuse_own_client_write(
    op_id: str,
    realm: str,
    client: Any,
    *,
    named: Any,
    requested: dict[str, Any] | None = None,
) -> None:
    """Raise :exc:`MehoAccessRefusedError` when the write breaks a client MEHO itself uses.

    *client* is the client as Keycloak stores it, *named* the ``client_id``
    the caller sent, and *requested* the representation of an update.
    """
    if not is_meho_login_realm(realm) or not _is_own_client(op_id, client, _text(named)):
        return
    if op_id == CLIENT_DELETE_OP:
        action = "deleting it"
    else:
        blocked = blocked_changes(client, requested or {}, OWN_CLIENT_CHANGES)
        if not blocked:
            return
        action = f"changing {', '.join(blocked)}"
    raise MehoAccessRefusedError(
        op_id,
        _OWN_CLIENT,
        f"{_REFUSED}: this client is one that MEHO itself signs in with, and {action} "
        f"can stop MEHO's sign-in from working. Nothing was changed. {NO_APPROVAL}",
    )


def refuse_login_realm_write(realm: str, stored: Any, requested: dict[str, Any]) -> None:
    """Raise :exc:`MehoAccessRefusedError` when the realm update can lock MEHO out.

    *realm* is the realm name the call names, *stored* the realm as Keycloak
    stores it, and *requested* the representation of the update.
    """
    stored_name = _text(stored.get("realm")) if isinstance(stored, dict) else None
    names = [name for name in (realm, stored_name) if name is not None]
    if not any(is_meho_login_realm(name) for name in names):
        return
    if not isinstance(stored, dict):
        raise _not_checked(REALM_UPDATE_OP, "the realm came back in an odd shape")
    blocked = blocked_changes(stored, requested, LOGIN_REALM_CHANGES)
    if not blocked:
        return
    raise MehoAccessRefusedError(
        REALM_UPDATE_OP,
        _LOGIN_REALM,
        f"{_REFUSED}: this realm is the realm MEHO itself signs in with, and changing "
        f"{', '.join(blocked)} can lock everybody out of MEHO. Nothing was changed. "
        f"{NO_APPROVAL}",
    )


def _live_connector(ctx: PreviewContext) -> Any:
    # Imported late: the preview module imports the delete lookup and this module's peers.
    from meho_backplane.connectors.keycloak.ops_write_preview import _live_keycloak_connector

    return _live_keycloak_connector(ctx)


def _client_park_check(op_id: str) -> ParkCheck:
    async def _check(ctx: PreviewContext) -> str | None:
        connector = _live_connector(ctx)
        if connector is None:
            return None
        realm = resolve_realm_config(ctx.target).managed_realm
        if not is_meho_login_realm(realm):
            return None
        try:
            found = await find_client(connector, ctx.operator, ctx.target, ctx.params)
        except Exception:
            # A client MEHO cannot find is left to the run, which checks again.
            return None
        if not found.present:
            return None
        representation = ctx.params.get("representation")
        try:
            refuse_own_client_write(
                op_id,
                realm,
                found.rep,
                named=ctx.params.get("client_id"),
                requested=representation if isinstance(representation, dict) else None,
            )
        except MehoAccessRefusedError as exc:
            return exc.reason
        return None

    return _check


async def login_realm_park_reason(ctx: PreviewContext) -> str | None:
    """The check of ``keycloak.realm.update`` before approval: the refusal reason, or ``None``."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    realm = _text(ctx.params.get("realm")) or resolve_realm_config(ctx.target).managed_realm
    if not is_meho_login_realm(realm):
        return None
    representation = ctx.params.get("representation")
    if not isinstance(representation, dict):
        return None
    try:
        stored = await get_or_none(
            connector, ctx.target, fill_path(_ADMIN_REALM_PATH, {"realm": realm}), ctx.operator
        )
    except Exception:
        # A realm MEHO cannot read is left to the run, which checks again.
        return None
    if stored is None:
        return None
    try:
        refuse_login_realm_write(realm, stored, representation)
    except MehoAccessRefusedError as exc:
        return exc.reason
    return None


def register_meho_own_login_checks() -> None:
    """Run the check before a client update or delete waits for approval.

    ``keycloak.realm.update`` runs :func:`login_realm_park_reason` from its
    own check (:mod:`.ops_write_realm_smtp`), after the check of the SMTP
    password.
    """
    for op_id in (CLIENT_UPDATE_OP, CLIENT_DELETE_OP):
        register_park_check(op_id, _client_park_check(op_id))
