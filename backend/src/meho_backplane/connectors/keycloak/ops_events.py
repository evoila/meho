# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the realm's saved events: ``keycloak.events.list`` and ``keycloak.admin_events.list``.

After an access change, or in a security review, the first question is "who
logged in, and who failed" and "who changed what". These two safe reads
answer it:

* ``keycloak.events.list``: login events (time, type, client, user id, IP
  address, error).
* ``keycloak.admin_events.list``: admin events (time, operation, resource
  type and path, and who did it: realm, client, user, IP address).

Each op copies only a fixed list of fields (an allow-list). The admin event
``representation`` (the JSON text of what was changed; it can hold a
password), the event ``details`` map and the session id are never returned.

When the realm does not save the events (``eventsEnabled`` or
``adminEventsEnabled`` is false), the answer says so, with ``enabled: false``
and a message. An empty list would look like "nothing happened".

Paging: ``first`` and ``max`` (default 100). The op asks Keycloak for one row
more than ``max``, so ``has_more`` is true when the page is not the whole
list. A read that fails, or a body of the wrong shape, is an error, never an
empty list. Times are returned as ISO 8601 in UTC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_EVENTS_PATH,
    _ADMIN_REALM_PATH,
    _EVENTS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_user_state import _iso
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "EVENT_OPS",
    "WHEN_TO_USE_EVENTS_BY_GROUP",
    "keycloak_admin_events_list",
    "keycloak_events_list",
]

_DEFAULT_MAX = 100
_LOGIN_FLAG = "eventsEnabled"
_ADMIN_FLAG = "adminEventsEnabled"

#: Param name -> Keycloak query name, for the string filters of each op.
_LOGIN_STRING_FILTERS = ("client", "user", "ipAddress", "dateFrom", "dateTo")
_ADMIN_STRING_FILTERS = ("resourcePath", "authUser", "authIpAddress", "dateFrom", "dateTo")
_LOGIN_LIST_FILTERS = ("type",)
_ADMIN_LIST_FILTERS = ("operationTypes", "resourceTypes")


def _query(
    params: dict[str, Any], strings: tuple[str, ...], lists: tuple[str, ...]
) -> tuple[dict[str, Any], int, int]:
    """The Keycloak query for one page, plus ``first`` and ``max``.

    Asks for ``max + 1`` rows, so the caller can tell a full list from a page.
    """
    query: dict[str, Any] = {}
    for name in strings:
        value = params.get(name)
        if isinstance(value, str) and value:
            query[name] = value
    for name in lists:
        values = params.get(name)
        if isinstance(values, list) and values:
            query[name] = [str(item) for item in values]
    first = params.get("first")
    first = first if isinstance(first, int) and not isinstance(first, bool) else 0
    limit = params.get("max")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else _DEFAULT_MAX
    query["first"] = first
    query["max"] = limit + 1
    return query, first, limit


async def _recording_is_on(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    flag: str,
) -> bool:
    """Whether the realm saves this kind of event. Not a bool is an error."""
    body = await connector._get_admin_json(
        target, fill_path(_ADMIN_REALM_PATH, {"realm": realm}), operator=operator
    )
    value = body.get(flag)
    if not isinstance(value, bool):
        raise KeycloakUnexpectedResponseError(f"the realm answer has no {flag} flag")
    return value


def _switched_off(realm: str, flag: str, first: int, limit: int) -> dict[str, Any]:
    kind = "login events" if flag == _LOGIN_FLAG else "admin events"
    return {
        "realm": realm,
        "enabled": False,
        "message": (
            f"The realm {realm!r} does not save {kind} ({flag} is false), so there is "
            "nothing to read. This is not an empty history. Turn the setting on first "
            "(keycloak.realm.update); events from before that were never saved."
        ),
        "first": first,
        "max": limit,
        "has_more": False,
        "rows": [],
        "total": 0,
    }


def _object(row: Any, what: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise KeycloakUnexpectedResponseError(f"{what} is not a JSON object")
    return row


def _login_row(row: Any) -> dict[str, Any]:
    data = _object(row, "a login event")
    return {
        "time": _iso(data.get("time")),
        "type": data.get("type"),
        "clientId": data.get("clientId"),
        "userId": data.get("userId"),
        "ipAddress": data.get("ipAddress"),
        "error": data.get("error"),
    }


def _admin_row(row: Any) -> dict[str, Any]:
    data = _object(row, "an admin event")
    auth = data.get("authDetails")
    who = auth if isinstance(auth, dict) else {}
    return {
        "time": _iso(data.get("time")),
        "operationType": data.get("operationType"),
        "resourceType": data.get("resourceType"),
        "resourcePath": data.get("resourcePath"),
        "authRealmId": who.get("realmId"),
        "authClientId": who.get("clientId"),
        "authUserId": who.get("userId"),
        "authIpAddress": who.get("ipAddress"),
        "error": data.get("error"),
    }


async def _list(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    path: str,
    flag: str,
    strings: tuple[str, ...],
    lists: tuple[str, ...],
    make_row: Any,
) -> dict[str, Any]:
    realm = resolve_realm_config(target).managed_realm
    query, first, limit = _query(params, strings, lists)
    if not await _recording_is_on(connector, operator, target, realm, flag):
        return _switched_off(realm, flag, first, limit)
    rows = await get_admin_list_strict(
        connector, target, fill_path(path, {"realm": realm}), operator=operator, params=query
    )
    page = [make_row(row) for row in rows[:limit]]
    return {
        "realm": realm,
        "enabled": True,
        "first": first,
        "max": limit,
        "has_more": len(rows) > limit,
        "rows": page,
        "total": len(page),
    }


async def keycloak_events_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the realm's login events, with no details map and no session id."""
    return await _list(
        self,
        operator,
        target,
        params,
        path=_EVENTS_PATH,
        flag=_LOGIN_FLAG,
        strings=_LOGIN_STRING_FILTERS,
        lists=_LOGIN_LIST_FILTERS,
        make_row=_login_row,
    )


async def keycloak_admin_events_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the realm's admin events, never the ``representation`` of a change."""
    return await _list(
        self,
        operator,
        target,
        params,
        path=_ADMIN_EVENTS_PATH,
        flag=_ADMIN_FLAG,
        strings=_ADMIN_STRING_FILTERS,
        lists=_ADMIN_LIST_FILTERS,
        make_row=_admin_row,
    )


_WHEN_TO_USE_EVENTS = (
    "Use to see who logged in, who failed, and who changed what in the realm, "
    "with no change. ``keycloak.events.list`` lists login events (time, type, "
    "client, user id, IP address, error). ``keycloak.admin_events.list`` lists "
    "admin events (time, operation, resource type and path, and who did it). "
    "If the realm does not save these events, the answer says so: it is not an "
    "empty history. Neither op returns the content of a change or a secret."
)

#: Group text for the two ops, merged into the connector's map.
WHEN_TO_USE_EVENTS_BY_GROUP: dict[str, str] = {"events": _WHEN_TO_USE_EVENTS}

_NULLABLE_STR = {"type": ["string", "null"]}
_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}

_PAGING_PROPERTIES: dict[str, Any] = {
    "dateFrom": {**_DATE, "description": "Oldest day to include, as YYYY-MM-DD."},
    "dateTo": {**_DATE, "description": "Newest day to include, as YYYY-MM-DD."},
    "first": {
        "type": "integer",
        "minimum": 0,
        "description": "How many rows to skip (for the next page). Default 0.",
    },
    "max": {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "description": f"Rows in one page. Default {_DEFAULT_MAX}.",
    },
}

_RESPONSE_BASE: dict[str, Any] = {
    "realm": {"type": "string"},
    "enabled": {
        "type": "boolean",
        "description": "false: the realm does not save these events (see message).",
    },
    "message": {"type": "string", "description": "Only when enabled is false."},
    "first": {"type": "integer"},
    "max": {"type": "integer"},
    "has_more": {
        "type": "boolean",
        "description": "true: more rows exist after this page. Ask again with a higher first.",
    },
    "total": {"type": "integer", "description": "Rows in this page."},
}

_LOGIN_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_RESPONSE_BASE,
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": _NULLABLE_STR,
                    "type": _NULLABLE_STR,
                    "clientId": _NULLABLE_STR,
                    "userId": _NULLABLE_STR,
                    "ipAddress": _NULLABLE_STR,
                    "error": _NULLABLE_STR,
                },
            },
        },
    },
    "required": ["realm", "enabled", "first", "max", "has_more", "rows", "total"],
}

_ADMIN_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_RESPONSE_BASE,
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": _NULLABLE_STR,
                    "operationType": _NULLABLE_STR,
                    "resourceType": _NULLABLE_STR,
                    "resourcePath": _NULLABLE_STR,
                    "authRealmId": _NULLABLE_STR,
                    "authClientId": _NULLABLE_STR,
                    "authUserId": _NULLABLE_STR,
                    "authIpAddress": _NULLABLE_STR,
                    "error": _NULLABLE_STR,
                },
            },
        },
    },
    "required": ["realm", "enabled", "first", "max", "has_more", "rows", "total"],
}

_TAGS = ("read-only", "events", "keycloak")

EVENT_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.events.list",
        handler_attr="events_list",
        summary="List the realm's login events: who logged in, who failed, from where.",
        description=(
            "Reads the saved login events of the managed realm, newest first: time, "
            "type, clientId, userId, ipAddress, error. No details map, no session id. "
            "If the realm does not save login events, the answer has enabled=false "
            "and a message."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Event types to keep, for example LOGIN_ERROR.",
                },
                "client": {"type": "string", "minLength": 1, "description": "Client id."},
                "user": {"type": "string", "minLength": 1, "description": "User id (UUID)."},
                "ipAddress": {
                    "type": "string",
                    "minLength": 1,
                    "description": "IP address the login came from.",
                },
                **_PAGING_PROPERTIES,
            },
            "additionalProperties": False,
            "examples": [{}, {"type": ["LOGIN_ERROR"], "dateFrom": "2026-01-01", "max": 50}],
        },
        response_schema=_LOGIN_RESPONSE,
        group_key="events",
        tags=_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_EVENTS,
            "when_to_call": (
                "After an access change or in a security review, to see who logged "
                "in and who failed, with which client and from which address."
            ),
            "next_step": (
                "If enabled is false, check keycloak.events.config.get. To see who "
                "changed settings, call keycloak.admin_events.list."
            ),
            "parameter_hints": {
                "type": 'A list such as ["LOGIN_ERROR"]. Leave out for all types.',
                "user": "The user's UUID from keycloak.user.list, not the username.",
                "dateFrom": "YYYY-MM-DD.",
                "first": "Use first = first + max for the next page when has_more is true.",
            },
            "output_shape": (
                "``{realm, enabled, first, max, has_more, rows: [{time, type, "
                "clientId, userId, ipAddress, error}], total}``. When enabled is "
                "false: ``message`` says the realm does not save login events."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.admin_events.list",
        handler_attr="admin_events_list",
        summary="List the realm's admin events: who changed what, and when.",
        description=(
            "Reads the saved admin events of the managed realm, newest first: time, "
            "operation, resource type and path, and who did it. Never the "
            "representation of the change (it can hold a secret). If the realm does "
            "not save admin events, the answer has enabled=false and a message."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "operationTypes": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Operations to keep: CREATE, UPDATE, DELETE, ACTION.",
                },
                "resourceTypes": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Resource types to keep, for example USER, CLIENT.",
                },
                "resourcePath": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Resource path to match. A * matches any part.",
                },
                "authUser": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the user who made the change.",
                },
                "authIpAddress": {
                    "type": "string",
                    "minLength": 1,
                    "description": "IP address the change came from.",
                },
                **_PAGING_PROPERTIES,
            },
            "additionalProperties": False,
            "examples": [{}, {"operationTypes": ["DELETE"], "resourceTypes": ["USER"]}],
        },
        response_schema=_ADMIN_RESPONSE,
        group_key="events",
        tags=_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_EVENTS,
            "when_to_call": (
                "To see who changed users, clients, roles or settings in the realm, and when."
            ),
            "next_step": (
                "If enabled is false, check keycloak.events.config.get. To see who "
                "logged in, call keycloak.events.list."
            ),
            "parameter_hints": {
                "operationTypes": 'A list such as ["DELETE"]. Leave out for all.',
                "resourcePath": "For example users/* to match every user.",
                "authUser": "The UUID of the user who made the change.",
                "first": "Use first = first + max for the next page when has_more is true.",
            },
            "output_shape": (
                "``{realm, enabled, first, max, has_more, rows: [{time, "
                "operationType, resourceType, resourcePath, authRealmId, "
                "authClientId, authUserId, authIpAddress, error}], total}``. "
                "There is no representation field."
            ),
        },
    ),
)
