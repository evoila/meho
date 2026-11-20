# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep Keycloak client and realm writes from taking away MEHO's own login.

In MEHO's own login realm (:func:`~.ops_user_meho_access_guard.is_meho_login_realm`)
MEHO refuses these writes:

* ``keycloak.client.delete`` of a client MEHO itself uses
  (:func:`meho_own_client_ids`);
* ``keycloak.client.update`` of such a client, unless every changed field
  is on :data:`CLIENT_ALLOWED`;
* ``keycloak.realm.update`` of the login realm, unless every changed field
  is on :data:`REALM_ALLOWED`.

MEHO finds its own clients in its settings, never in a param of the call.
It compares the ``clientId`` that Keycloak stores for the client, without
letter case, so a client named by its internal id or by another spelling of
its client id is the same client.

A field counts as changed when its value after the write differs from the
stored value. So a field sent with the value it already has is let through.

The check runs before the request waits for approval
(:func:`register_meho_client_realm_checks`) and again when the call runs. No
approval and no standing grant lets such a write through. On doubt MEHO
refuses: when it cannot read its settings, or the stored client has no
client id.

The refusal is :exc:`~.ops_user_meho_access_guard.MehoAccessRefusedError`
with the code ``meho_own_client`` or ``meho_login_realm``. Its text says
what was refused and why, names only fields the caller sent, and names no
client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, _CLIENT_PATH, fill_path
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
from meho_backplane.connectors.keycloak.ops_write_merge import _same, merge_representation
from meho_backplane.connectors.keycloak.ops_write_realm_smtp import (
    REALM_UPDATE_OP_ID,
    _refuse_inline_smtp_password,
)
from meho_backplane.connectors.keycloak.realm_params import realm_param
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import register_park_check
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "CLIENT_ALLOWED",
    "CLIENT_UPDATE_OP",
    "REALM_ALLOWED",
    "meho_own_client_ids",
    "read_login_realm_for_update",
    "read_own_client_for_update",
    "refuse_own_client_delete",
    "register_meho_client_realm_checks",
]

CLIENT_UPDATE_OP: Final = "keycloak.client.update"

#: Fields of a client MEHO itself uses that a write may change. Lists may
#: only grow: every address already there must stay. ``enabled`` may only
#: be set to ``true``.
CLIENT_ALLOWED: Final = frozenset({"name", "description", "redirectUris", "webOrigins", "enabled"})

#: Fields of the login realm that a write may change. ``enabled`` may only
#: be set to ``true``.
REALM_ALLOWED: Final = frozenset(
    {
        "displayName",
        "displayNameHtml",
        "smtpServer",
        "internationalizationEnabled",
        "supportedLocales",
        "defaultLocale",
        "eventsEnabled",
        "eventsExpiration",
        "eventsListeners",
        "enabledEventTypes",
        "adminEventsEnabled",
        "adminEventsDetailsEnabled",
        "enabled",
    }
)

#: Client fields whose list may only grow.
_GROW_ONLY: Final = frozenset({"redirectUris", "webOrigins"})

_OWN_CLIENT: Final = "meho_own_client"
_LOGIN_REALM: Final = "meho_login_realm"
_REFUSED: Final = "this write was refused"
_MISSING: Final = object()


def meho_own_client_ids() -> frozenset[str] | None:
    """The client ids (lower case) MEHO itself uses; ``None`` when the settings cannot be read.

    These are the clients of MEHO's admin login, of the check runner, of the
    command line login and its approval sign-in, of the web console and its
    approval sign-in, and the backplane's own client (its token audience).
    """
    try:
        settings = get_settings()
        values = (
            settings.keycloak_admin_client_id,
            settings.check_runner_client_id,
            settings.keycloak_cli_client_id,
            settings.keycloak_cli_approve_client_id,
            settings.ui_keycloak_client_id,
            settings.ui_keycloak_approve_client_id,
            settings.keycloak_audience,
        )
    except Exception:
        return None
    return frozenset(
        value.strip().casefold() for value in values if isinstance(value, str) and value.strip()
    )


def _is_own_client(client: dict[str, Any]) -> bool:
    """Whether the stored *client* is one MEHO itself uses; ``True`` on doubt."""
    own = meho_own_client_ids()
    client_id = client.get("clientId")
    if own is None or not isinstance(client_id, str) or not client_id.strip():
        return True
    return client_id.strip().casefold() in own


def _list_kept(old: Any, new: Any) -> bool:
    """Whether *new* is a list of text that keeps every item of *old*."""
    before = old if isinstance(old, list) else []
    if not isinstance(new, list) or not all(isinstance(item, str) for item in new):
        return False
    return set(before) <= set(new)


def _not_allowed(
    current: dict[str, Any],
    requested: dict[str, Any],
    allowed: frozenset[str],
    grow_only: frozenset[str] = frozenset(),
) -> list[str]:
    """The sent fields whose change is not allowed, in sorted order."""
    merged = merge_representation(current, requested)
    refused = []
    for field in sorted(requested):
        old, new = current.get(field, _MISSING), merged[field]
        if not _same(old, new) and not _change_allowed(field, old, new, allowed, grow_only):
            refused.append(field)
    return refused


def _change_allowed(
    field: str, old: Any, new: Any, allowed: frozenset[str], grow_only: frozenset[str]
) -> bool:
    if field not in allowed:
        return False
    if field == "enabled":
        return new is True
    return field not in grow_only or _list_kept(old, new)


def _fields(names: list[str]) -> str:
    return ", ".join(repr(name) for name in names)


def _requested(params: dict[str, Any]) -> dict[str, Any]:
    representation = params.get("representation")
    return dict(representation) if isinstance(representation, dict) else {}


def refuse_own_client_update(client: dict[str, Any], requested: dict[str, Any]) -> None:
    """Raise when the update changes a field of a client MEHO uses that may not change."""
    if not _is_own_client(client):
        return
    refused = _not_allowed(client, requested, CLIENT_ALLOWED, _GROW_ONLY)
    if refused:
        raise MehoAccessRefusedError(
            CLIENT_UPDATE_OP,
            _OWN_CLIENT,
            f"{_REFUSED}: this client is one that MEHO itself uses, and changing "
            f"{_fields(refused)} can stop MEHO's own login from working. Nothing was "
            f"changed. {NO_APPROVAL}",
        )


def refuse_own_client_delete(client: dict[str, Any]) -> None:
    """Raise when *client* is a client MEHO itself uses."""
    if _is_own_client(client):
        raise MehoAccessRefusedError(
            CLIENT_DELETE_OP,
            _OWN_CLIENT,
            f"{_REFUSED}: this client is one that MEHO itself uses, and deleting it stops "
            f"MEHO's own login from working. Nothing was changed. {NO_APPROVAL}",
        )


def refuse_login_realm_update(realm: dict[str, Any], requested: dict[str, Any]) -> None:
    """Raise when the update changes a field of the login realm that may not change."""
    refused = _not_allowed(realm, requested, REALM_ALLOWED)
    if refused:
        raise MehoAccessRefusedError(
            REALM_UPDATE_OP_ID,
            _LOGIN_REALM,
            f"{_REFUSED}: this realm is the one MEHO's own login uses, and changing "
            f"{_fields(refused)} can stop MEHO's own login from working. Nothing was "
            f"changed. {NO_APPROVAL}",
        )


async def read_own_client_for_update(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    uuid: str,
    requested: dict[str, Any],
) -> dict[str, Any] | None:
    """Check a client update in the login realm; return the client that was read.

    Outside MEHO's login realm nothing is read and ``None`` is returned. The
    caller writes the returned client back with the change, so the check and
    the write use the same read.
    """
    if not is_meho_login_realm(realm):
        return None
    path = fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": uuid})
    client = await connector._get_admin_json(target, path, operator=operator)
    if not isinstance(client, dict):
        raise ValueError(f"{CLIENT_UPDATE_OP}: Keycloak answered with no client object")
    refuse_own_client_update(client, requested)
    return client


async def read_login_realm_for_update(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    requested: dict[str, Any],
) -> dict[str, Any] | None:
    """Check a realm update of the login realm; return the realm that was read.

    For another realm nothing is read and ``None`` is returned.
    """
    if not is_meho_login_realm(realm):
        return None
    path = fill_path(_ADMIN_REALM_PATH, {"realm": realm})
    current = await connector._get_admin_json(target, path, operator=operator)
    if not isinstance(current, dict):
        raise ValueError(f"{REALM_UPDATE_OP_ID}: Keycloak answered with no realm object")
    refuse_login_realm_update(current, requested)
    return current


async def _park_client(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str
) -> dict[str, Any] | None:
    """The stored client the call names, or ``None`` when it is not found."""
    if ctx.descriptor.op_id == CLIENT_DELETE_OP:
        found = await find_client(connector, ctx.operator, ctx.target, ctx.params)
        return found.rep if found.present else None
    uuid = ctx.params.get("id")
    client_id = ctx.params.get("client_id")
    if not (isinstance(uuid, str) and uuid.strip()):
        if not (isinstance(client_id, str) and client_id.strip()):
            return None
        uuid = await connector._find_client_uuid(
            ctx.target, realm, client_id.strip(), operator=ctx.operator
        )
        if uuid is None:
            return None
    path = fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": uuid.strip()})
    return await get_or_none(connector, ctx.target, path, ctx.operator)


async def _client_park_check(ctx: PreviewContext) -> str | None:
    # Imported late: the preview module imports this module.
    from meho_backplane.connectors.keycloak.ops_write_preview import _live_keycloak_connector

    connector = _live_keycloak_connector(ctx)
    if connector is None or ctx.target is None:
        return None
    realm = resolve_realm_config(ctx.target).managed_realm
    if not is_meho_login_realm(realm):
        return None
    client = await _park_client(connector, ctx, realm)
    if client is None:
        # A client that is not found is left to the run, which fails with "not found".
        return None
    try:
        if ctx.descriptor.op_id == CLIENT_DELETE_OP:
            refuse_own_client_delete(client)
        else:
            refuse_own_client_update(client, _requested(ctx.params))
    except MehoAccessRefusedError as exc:
        return exc.reason
    return None


async def _realm_park_check(ctx: PreviewContext) -> str | None:
    reason = await _refuse_inline_smtp_password(ctx)
    if reason is not None:
        return reason
    # Imported late: the preview module imports this module.
    from meho_backplane.connectors.keycloak.ops_write_preview import _live_keycloak_connector

    connector = _live_keycloak_connector(ctx)
    if connector is None or ctx.target is None:
        return None
    realm = (
        realm_param(ctx.params, "realm", REALM_UPDATE_OP_ID)
        or resolve_realm_config(ctx.target).managed_realm
    )
    try:
        await read_login_realm_for_update(
            connector, ctx.operator, ctx.target, realm, _requested(ctx.params)
        )
    except MehoAccessRefusedError as exc:
        return exc.reason
    return None


def register_meho_client_realm_checks() -> None:
    """Run the checks before a client or realm write waits for approval."""
    register_park_check(CLIENT_UPDATE_OP, _client_park_check)
    register_park_check(CLIENT_DELETE_OP, _client_park_check)
    register_park_check(REALM_UPDATE_OP_ID, _realm_park_check)
