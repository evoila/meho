# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read how a realm logs people in, and what it records: three safe reads.

* ``keycloak.auth_flow.step.list``: the steps of one login flow (default:
  the realm's browser flow).
* ``keycloak.required_action.list``: the realm's required actions, in the
  order Keycloak runs them at login.
* ``keycloak.events.config.get``: the realm's event settings, with the full
  list of event types it saves.

An operator uses them before turning on one-time codes (does the flow ask
for a code, is "Configure OTP" on, which action comes first) and before and
after turning on event saving.

No secret and no step setting is returned. Each op copies only a fixed list
of fields (an allow-list); it never removes fields from the vendor answer.
A step's ``authenticationConfig`` id, a required action's ``config`` map and
any other field are left out. A read that fails, or an answer of the wrong
shape, is an error, never an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    _EVENTS_CONFIG_PATH,
    _REQUIRED_ACTIONS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "FLOW_READ_OPS",
    "WHEN_TO_USE_FLOW_READ_BY_GROUP",
    "KeycloakFlowNotFoundError",
    "keycloak_auth_flow_step_list",
    "keycloak_events_config_get",
    "keycloak_required_action_list",
]

_STEP_FIELDS = (
    "level",
    "index",
    "displayName",
    "requirement",
    "providerId",
    "authenticationFlow",
)
_REQUIRED_ACTION_FIELDS = ("alias", "name", "enabled", "defaultAction", "priority")
_EVENT_FIELDS = (
    "eventsEnabled",
    "eventsExpiration",
    "eventsListeners",
    "enabledEventTypes",
    "adminEventsEnabled",
    "adminEventsDetailsEnabled",
)


class KeycloakFlowNotFoundError(RuntimeError):
    """The named login flow does not exist in the realm."""


def _pick(row: Any, fields: tuple[str, ...], what: str) -> dict[str, Any]:
    """Copy only *fields* from a JSON object. Anything else is an error."""
    if not isinstance(row, dict):
        raise KeycloakUnexpectedResponseError(f"{what} is not a JSON object")
    return {field: row.get(field) for field in fields}


async def _browser_flow_alias(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str:
    realm_row = await connector._get_admin_json(
        target, fill_path(_ADMIN_REALM_PATH, {"realm": realm}), operator=operator
    )
    alias = realm_row.get("browserFlow")
    if not isinstance(alias, str) or not alias:
        raise KeycloakUnexpectedResponseError("the realm names no browser flow")
    return alias


async def keycloak_auth_flow_step_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the steps of one login flow, with no step setting."""
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    flow = params.get("flow")
    if not isinstance(flow, str) or not flow:
        flow = await _browser_flow_alias(self, operator, target, realm_segment)
    try:
        rows = await get_admin_list_strict(
            self,
            target,
            fill_path(
                _AUTH_FLOW_EXECUTIONS_PATH,
                {"realm": realm_segment, "flowAlias": quote_segment(flow)},
            ),
            operator=operator,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise KeycloakFlowNotFoundError(
                f"flow not found: the realm {realm!r} has no login flow named {flow!r}"
            ) from exc
        raise
    steps = [_pick(row, _STEP_FIELDS, "a flow step") for row in rows]
    return {"realm": realm, "flow": flow, "rows": steps, "total": len(steps)}


def _priority_key(row: dict[str, Any]) -> tuple[int, int]:
    priority = row.get("priority")
    if isinstance(priority, int) and not isinstance(priority, bool):
        return (0, priority)
    return (1, 0)


async def keycloak_required_action_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the realm's required actions by priority, with no config map."""
    del params  # declared empty
    realm = resolve_realm_config(target).managed_realm
    rows = await get_admin_list_strict(
        self,
        target,
        fill_path(_REQUIRED_ACTIONS_PATH, {"realm": quote_segment(realm)}),
        operator=operator,
    )
    actions = [_pick(row, _REQUIRED_ACTION_FIELDS, "a required action") for row in rows]
    actions.sort(key=_priority_key)
    return {"realm": realm, "rows": actions, "total": len(actions)}


async def keycloak_events_config_get(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the realm's event settings, with the full list of saved types."""
    del params  # declared empty
    realm = resolve_realm_config(target).managed_realm
    body = await self._get_admin_json(
        target,
        fill_path(_EVENTS_CONFIG_PATH, {"realm": quote_segment(realm)}),
        operator=operator,
    )
    picked = _pick(body, _EVENT_FIELDS, "the events config")
    if not isinstance(picked["enabledEventTypes"], list):
        raise KeycloakUnexpectedResponseError("enabledEventTypes is not a JSON array")
    return {"realm": realm, **picked}


_WHEN_TO_USE_FLOW_READ = (
    "Use to check how the realm logs people in and what it records, with no "
    "change. ``keycloak.auth_flow.step.list`` lists the steps of a login flow "
    "(default: the browser flow): level, order, name, requirement and type. "
    "``keycloak.required_action.list`` lists the required actions in the order "
    "Keycloak runs them at login, with enabled and default. "
    "``keycloak.events.config.get`` shows the event settings and the full list "
    "of saved event types. None of them returns a step setting or a secret."
)

#: Group text for the three ops, merged into the connector's map.
WHEN_TO_USE_FLOW_READ_BY_GROUP: dict[str, str] = {"login_setup": _WHEN_TO_USE_FLOW_READ}

_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_EXECUTIONS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string"},
        "flow": {"type": "string", "description": "The flow alias that was read."},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "level": _NULLABLE_INT,
                    "index": _NULLABLE_INT,
                    "displayName": _NULLABLE_STR,
                    "requirement": _NULLABLE_STR,
                    "providerId": _NULLABLE_STR,
                    "authenticationFlow": {
                        "type": ["boolean", "null"],
                        "description": "true: this row is a sub-flow, not a step.",
                    },
                },
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["realm", "flow", "rows", "total"],
}

_ACTIONS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "alias": _NULLABLE_STR,
                    "name": _NULLABLE_STR,
                    "enabled": _NULLABLE_BOOL,
                    "defaultAction": _NULLABLE_BOOL,
                    "priority": _NULLABLE_INT,
                },
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["realm", "rows", "total"],
}

_EVENTS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string"},
        "eventsEnabled": _NULLABLE_BOOL,
        "eventsExpiration": _NULLABLE_INT,
        "eventsListeners": {"type": ["array", "null"], "items": {"type": "string"}},
        "enabledEventTypes": {"type": "array", "items": {"type": "string"}},
        "adminEventsEnabled": _NULLABLE_BOOL,
        "adminEventsDetailsEnabled": _NULLABLE_BOOL,
    },
    "required": ["realm", "enabledEventTypes"],
}

_TAGS = ("read-only", "login-flow", "keycloak")

FLOW_READ_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.auth_flow.step.list",
        handler_attr="auth_flow_step_list",
        summary="List the steps of one login flow (default: the realm's browser flow).",
        description=(
            "Reads the steps of a login flow of the managed realm (default: the "
            "browser flow). Returns level, index, displayName, requirement, "
            "providerId and whether a row is a sub-flow. Never a step's settings. "
            "An unknown flow alias is a 'flow not found' error."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "flow": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Alias of the flow. Default: the realm's browser flow.",
                }
            },
            "additionalProperties": False,
            "examples": [{}, {"flow": "<flow-alias>"}],
        },
        response_schema=_EXECUTIONS_RESPONSE,
        group_key="login_setup",
        tags=_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_FLOW_READ,
            "when_to_call": (
                "To check that a login flow asks for the one-time code, or to see "
                "which steps a flow runs and whether each is required."
            ),
            "next_step": (
                "Check that the OTP Form step is REQUIRED inside a CONDITIONAL "
                "sub-flow. Then read keycloak.required_action.list."
            ),
            "parameter_hints": {"flow": "Flow alias. Leave out to read the browser flow."},
            "output_shape": (
                "``{realm, flow, rows: [{level, index, displayName, requirement, "
                "providerId, authenticationFlow}], total}``. A sub-flow row comes "
                "right before its own steps, which have a higher level."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.required_action.list",
        handler_attr="required_action_list",
        summary="List the realm's required actions in login order.",
        description=(
            "Reads the required actions of the target's managed realm. Returns "
            "alias, name, enabled, defaultAction and priority for each, sorted "
            "by priority: the order Keycloak asks for them at login (a lower "
            "number comes first). Never returns an action's config map."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_ACTIONS_RESPONSE,
        group_key="login_setup",
        tags=_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_FLOW_READ,
            "when_to_call": (
                "To check that 'Configure OTP' is on, and whether Keycloak asks "
                "for it before or after 'Update password'."
            ),
            "next_step": (
                "If Configure OTP is on, a lower priority number asks for it before "
                "the other actions. Then read keycloak.auth_flow.step.list."
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{realm, rows: [{alias, name, enabled, defaultAction, priority}], "
                "total}`` sorted by priority."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.events.config.get",
        handler_attr="events_config_get",
        summary="Read the realm's event settings and the full list of saved event types.",
        description=(
            "Reads the events config of the managed realm: eventsEnabled, "
            "eventsExpiration, eventsListeners, enabledEventTypes, "
            "adminEventsEnabled, adminEventsDetailsEnabled. When the realm sets "
            "no list, Keycloak fills enabledEventTypes with its default list."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_EVENTS_RESPONSE,
        group_key="login_setup",
        tags=_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_FLOW_READ,
            "when_to_call": (
                "Before and after turning on event saving, to see which event types Keycloak saves."
            ),
            "next_step": (
                "To change the event settings, use keycloak.realm.update (it waits for approval)."
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{realm, eventsEnabled, eventsExpiration, eventsListeners, "
                "enabledEventTypes, adminEventsEnabled, adminEventsDetailsEnabled}``."
            ),
        },
    ),
)
