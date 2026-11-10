# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give a running realm the approval sign-in flow: ``keycloak.authflow.ensure_approval_flow``.

The chart's realm file defines the login flow ``meho approval sign-in`` and
binds it to the client ``meho-cli-approve``. Keycloak reads that file only
when the realm does not exist yet, so a realm made earlier has no such flow.
This op adds it with one governed request.

What the op does, on the target's managed realm:

1. Find the client (default ``meho-cli-approve``). No client: stop before
   anything is written.
2. Find the flow ``meho approval sign-in``.

   * It does not exist: make it with the same steps as the realm file. The
     password form (required), then the sub-flow
     ``meho approval sign-in - one-time code`` (conditional) with
     ``conditional-user-configured`` and ``auth-otp-form`` (both required).
     Then read the steps again and check them.
   * It exists with the same steps: change nothing.
   * It exists with other steps: stop and name the first difference. Nothing
     is written.

3. Bind the flow as the client's browser flow
   (``authenticationFlowBindingOverrides.browser``), unless it already is.
   The client is read, the one key is put on top, and the full client is
   written back, so no other client setting changes. Then the client is read
   again; the result reports the browser flow id that was read back.

Safe to run twice: a second run finds the flow and the binding and writes
nothing. Risk level ``caution`` with approval: the op changes how people sign
in to approve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _AUTH_FLOW_ADD_STEP_PATH,
    _AUTH_FLOW_ADD_SUBFLOW_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    _AUTH_FLOWS_PATH,
    _CLIENT_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.ops_write_merge import merge_representation
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "APPROVAL_FLOW_ALIAS",
    "APPROVAL_FLOW_OPS",
    "ENSURE_APPROVAL_FLOW_OP_ID",
    "WHEN_TO_USE_AUTHFLOW_BY_GROUP",
    "KeycloakApprovalFlowError",
    "get_admin_list_strict",
    "keycloak_authflow_ensure_approval_flow",
]

ENSURE_APPROVAL_FLOW_OP_ID = "keycloak.authflow.ensure_approval_flow"
APPROVAL_FLOW_ALIAS = "meho approval sign-in"
_CODE_FLOW_ALIAS = "meho approval sign-in - one-time code"
_DEFAULT_CLIENT_ID = "meho-cli-approve"
_FLOW_DESCRIPTION = (
    "Sign-in of `meho approvals approve`: always asks for the password, and for "
    "the one-time code when the person has set one up."
)
_CODE_FLOW_DESCRIPTION = "Asks for the one-time code when the person has set one up."


@dataclass(frozen=True)
class _Step:
    """One step of the approval flow, as Keycloak lists it."""

    level: int
    name: str
    requirement: str
    parent: str
    subflow: bool = False


#: The steps in the order Keycloak lists them (a sub-flow's steps follow it).
_STEPS: tuple[_Step, ...] = (
    _Step(0, "auth-username-password-form", "REQUIRED", APPROVAL_FLOW_ALIAS),
    _Step(0, _CODE_FLOW_ALIAS, "CONDITIONAL", APPROVAL_FLOW_ALIAS, subflow=True),
    _Step(1, "conditional-user-configured", "REQUIRED", _CODE_FLOW_ALIAS),
    _Step(1, "auth-otp-form", "REQUIRED", _CODE_FLOW_ALIAS),
)


class KeycloakApprovalFlowError(RuntimeError):
    """The approval flow op stopped. ``code`` is a short, stable word.

    ``client_not_found``: the client does not exist. ``other_steps``: a flow
    with the name exists with other steps. ``check_failed``: the flow or the
    binding did not read back as written. ``flow_not_found``: the new flow
    could not be found after it was made.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{ENSURE_APPROVAL_FLOW_OP_ID} stopped ({code}): {message}")
        self.code = code


async def get_admin_list_strict(
    connector: KeycloakConnector,
    target: KeycloakTargetLike,
    path: str,
    *,
    operator: Operator,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    """GET a JSON array. Any other body raises; it is never read as an empty list."""
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    resp = await client.request("GET", path, params=params, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise KeycloakUnexpectedResponseError(
            f"GET {path} answered with {type(payload).__name__}, not a JSON array"
        )
    return payload


def _step_name(row: dict[str, Any]) -> str:
    """A sub-flow is named by its alias (``displayName``), a step by its provider."""
    key = "displayName" if row.get("authenticationFlow") is True else "providerId"
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _listed_steps(rows: list[Any]) -> list[dict[str, Any]]:
    """The steps of a flow as ``{level, step, requirement}``."""
    steps = []
    for row in rows:
        if not isinstance(row, dict):
            raise KeycloakUnexpectedResponseError("a flow step is not a JSON object")
        steps.append(
            {
                "level": row.get("level"),
                "step": _step_name(row),
                "requirement": row.get("requirement"),
            }
        )
    return steps


def _difference(steps: list[dict[str, Any]]) -> str | None:
    """Name the first difference to the wanted steps, or ``None`` when they match."""
    for index, want in enumerate(_STEPS):
        if index >= len(steps):
            return f"step {index + 1} ({want.name!r}) is missing"
        have = steps[index]
        if (have["level"], have["step"], have["requirement"]) != (
            want.level,
            want.name,
            want.requirement,
        ):
            return (
                f"step {index + 1} is {have['step']!r} ({have['requirement']}, level "
                f"{have['level']}); wanted {want.name!r} ({want.requirement}, level {want.level})"
            )
    if len(steps) > len(_STEPS):
        return f"the flow has {len(steps)} steps; wanted {len(_STEPS)}"
    return None


async def _read_steps(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    alias: str,
) -> list[Any]:
    path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(alias)}
    )
    return await get_admin_list_strict(connector, target, path, operator=operator)


async def _find_flow_id(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str | None:
    flows = await get_admin_list_strict(
        connector, target, fill_path(_AUTH_FLOWS_PATH, {"realm": realm}), operator=operator
    )
    for flow in flows:
        if isinstance(flow, dict) and flow.get("alias") == APPROVAL_FLOW_ALIAS:
            flow_id = flow.get("id")
            if isinstance(flow_id, str) and flow_id:
                return flow_id
    return None


async def _make_flow(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str:
    """Make the flow and its steps, set each requirement, and return the flow id."""
    await connector._write_admin(
        target,
        "POST",
        fill_path(_AUTH_FLOWS_PATH, {"realm": realm}),
        operator=operator,
        json={
            "alias": APPROVAL_FLOW_ALIAS,
            "description": _FLOW_DESCRIPTION,
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        },
        idempotent_conflict=False,
    )
    for step in _STEPS:
        values = {"realm": realm, "flowAlias": quote_segment(step.parent)}
        if step.subflow:
            path = fill_path(_AUTH_FLOW_ADD_SUBFLOW_PATH, values)
            body = {
                "alias": step.name,
                "type": "basic-flow",
                "description": _CODE_FLOW_DESCRIPTION,
            }
        else:
            path = fill_path(_AUTH_FLOW_ADD_STEP_PATH, values)
            body = {"provider": step.name}
        await connector._write_admin(
            target, "POST", path, operator=operator, json=body, idempotent_conflict=False
        )
    # Keycloak makes every new step DISABLED; set the wanted requirement.
    wanted: dict[tuple[Any, str], str] = {
        (step.level, step.name): step.requirement for step in _STEPS
    }
    executions_path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH,
        {"realm": realm, "flowAlias": quote_segment(APPROVAL_FLOW_ALIAS)},
    )
    for row in await _read_steps(connector, operator, target, realm, APPROVAL_FLOW_ALIAS):
        if not isinstance(row, dict):
            continue
        requirement = wanted.get((row.get("level"), _step_name(row)))
        if requirement is None or row.get("requirement") == requirement:
            continue
        await connector._write_admin(
            target,
            "PUT",
            executions_path,
            operator=operator,
            json={**row, "requirement": requirement},
            idempotent_conflict=False,
        )
    flow_id = await _find_flow_id(connector, operator, target, realm)
    if flow_id is None:
        raise KeycloakApprovalFlowError(
            "flow_not_found", f"flow {APPROVAL_FLOW_ALIAS!r} was made but cannot be found"
        )
    return flow_id


async def _bind(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    client_path: str,
    flow_id: str,
) -> dict[str, Any]:
    """Make *flow_id* the client's browser flow, then read the client again."""
    client = await connector._get_admin_json(target, client_path, operator=operator)
    overrides = client.get("authenticationFlowBindingOverrides")
    previous = overrides.get("browser") if isinstance(overrides, dict) else None
    changed = previous != flow_id
    if changed:
        merged = merge_representation(
            client, {"authenticationFlowBindingOverrides": {"browser": flow_id}}
        )
        await connector._write_admin(
            target, "PUT", client_path, operator=operator, json=merged, idempotent_conflict=False
        )
        client = await connector._get_admin_json(target, client_path, operator=operator)
        overrides = client.get("authenticationFlowBindingOverrides")
    read_back = overrides.get("browser") if isinstance(overrides, dict) else None
    if read_back != flow_id:
        raise KeycloakApprovalFlowError(
            "check_failed",
            f"the client's browser flow reads back as {read_back!r}, not the flow {flow_id!r}",
        )
    return {
        "browser_flow_id": read_back,
        "previous_browser_flow_id": previous,
        "binding_changed": changed,
    }


async def keycloak_authflow_ensure_approval_flow(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Make the approval sign-in flow when it is missing and bind it to the client."""
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    client_id = str(params.get("client_id") or _DEFAULT_CLIENT_ID).strip()
    client_uuid = await self._find_client_uuid(target, realm_segment, client_id, operator=operator)
    if client_uuid is None:
        raise KeycloakApprovalFlowError(
            "client_not_found", f"no client {client_id!r} in realm {realm!r}; nothing was changed"
        )

    flow_id = await _find_flow_id(self, operator, target, realm_segment)
    created = flow_id is None
    if flow_id is not None:
        steps = _listed_steps(
            await _read_steps(self, operator, target, realm_segment, APPROVAL_FLOW_ALIAS)
        )
        difference = _difference(steps)
        if difference is not None:
            raise KeycloakApprovalFlowError(
                "other_steps",
                f"flow {APPROVAL_FLOW_ALIAS!r} exists with other steps: {difference}. "
                "Nothing was changed. Fix the flow by hand, or delete it and run again.",
            )
    else:
        flow_id = await _make_flow(self, operator, target, realm_segment)
        steps = _listed_steps(
            await _read_steps(self, operator, target, realm_segment, APPROVAL_FLOW_ALIAS)
        )
        difference = _difference(steps)
        if difference is not None:
            raise KeycloakApprovalFlowError(
                "check_failed",
                f"flow {APPROVAL_FLOW_ALIAS!r} was made, but its steps read back wrong: "
                f"{difference}. The client was not bound to it.",
            )

    client_path = fill_path(
        _CLIENT_PATH, {"realm": realm_segment, "client-uuid": quote_segment(client_uuid)}
    )
    binding = await _bind(self, operator, target, client_path, flow_id)
    return {
        "realm": realm,
        "flow_alias": APPROVAL_FLOW_ALIAS,
        "flow_id": flow_id,
        "flow_created": created,
        "flow_steps": steps,
        "client_id": client_id,
        "client_uuid": client_uuid,
        **binding,
    }


_WHEN_TO_USE_AUTHFLOW = (
    "Use to give a realm that already runs the approval sign-in flow "
    "(``keycloak.authflow.ensure_approval_flow``). Keycloak reads the chart's "
    "realm file only when the realm is new, so an older realm has no flow "
    "'meho approval sign-in'. The op makes the flow (password, then the "
    "one-time code when the person has one) and binds it as the browser flow "
    "of the client 'meho-cli-approve'. Safe to run twice. A flow with that "
    "name but other steps is not changed: the op stops and names the "
    "difference. It needs approval."
)

_WHEN_TO_USE_AUTHFLOW_GROUP = (
    "Use to change a realm's login flows with approval. "
    "``keycloak.authflow.ensure_approval_flow`` gives a realm the approval "
    "sign-in flow of 'meho-cli-approve'. "
    "``keycloak.authflow.ensure_passkey_code_skip`` gives a realm made before "
    "Keycloak 26.4 the step that skips the one-time code after a passkey "
    "login. Both are safe to run twice and stop without a change on a flow "
    "with another shape."
)

#: Group text for the login flow ops' group, merged into the connector's map.
WHEN_TO_USE_AUTHFLOW_BY_GROUP: dict[str, str] = {"authflow_write": _WHEN_TO_USE_AUTHFLOW_GROUP}

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Client to bind the flow to. Default 'meho-cli-approve'.",
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"client_id": "meho-cli-approve"}],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The target's managed realm."},
        "flow_alias": {"type": "string", "description": "Always 'meho approval sign-in'."},
        "flow_id": {"type": "string", "description": "Internal id of the flow."},
        "flow_created": {
            "type": "boolean",
            "description": "true: this run made the flow. false: it was already there.",
        },
        "flow_steps": {
            "type": "array",
            "description": "The flow's steps as read back from Keycloak.",
            "items": {
                "type": "object",
                "properties": {
                    "level": {"type": ["integer", "null"]},
                    "step": {"type": "string"},
                    "requirement": {"type": ["string", "null"]},
                },
            },
        },
        "client_id": {"type": "string", "description": "The client the flow is bound to."},
        "client_uuid": {"type": "string", "description": "Internal id of the client."},
        "browser_flow_id": {
            "type": "string",
            "description": "The client's browser flow, read back after the run.",
        },
        "previous_browser_flow_id": {
            "type": ["string", "null"],
            "description": "The client's browser flow before the run; null when it had none.",
        },
        "binding_changed": {
            "type": "boolean",
            "description": "true: this run bound the flow. false: it was already bound.",
        },
    },
    "required": [
        "realm",
        "flow_alias",
        "flow_id",
        "flow_created",
        "flow_steps",
        "client_id",
        "client_uuid",
        "browser_flow_id",
        "previous_browser_flow_id",
        "binding_changed",
    ],
}

APPROVAL_FLOW_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=ENSURE_APPROVAL_FLOW_OP_ID,
        handler_attr="authflow_ensure_approval_flow",
        summary="Make the approval sign-in flow on the realm and bind it to meho-cli-approve.",
        description=(
            "Makes the login flow 'meho approval sign-in' (password, then the "
            "one-time code) on the managed realm when it is missing, and sets "
            "it as the browser flow of 'meho-cli-approve'. Changes nothing when "
            "both are there. Stops without a change when the flow has other steps."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="authflow_write",
        tags=("login-flow", "client", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_AUTHFLOW,
            "when_to_call": (
                "When a realm made before the approval sign-in flow existed must "
                "get it, so that `meho approvals approve` asks for the password "
                "and the one-time code. Also to check it: a second run changes "
                "nothing."
            ),
            "preconditions": (
                "The client (default 'meho-cli-approve') must exist in the "
                "target's managed realm. Create it first with "
                "keycloak.client.create when it is missing."
            ),
            "parameter_hints": {
                "client_id": "Leave it out to use 'meho-cli-approve'.",
            },
            "output_shape": (
                "``{realm, flow_alias, flow_id, flow_created, flow_steps: "
                "[{level, step, requirement}], client_id, client_uuid, "
                "browser_flow_id, previous_browser_flow_id, binding_changed}``."
            ),
            "next_step": (
                "flow_created and binding_changed both false: the realm already "
                "had the flow, nothing changed. An error with 'other_steps' names "
                "the difference; a person fixes the flow in the Keycloak admin "
                "console. Then `meho approvals approve` asks for the password "
                "and the one-time code through this flow."
            ),
        },
    ),
)
