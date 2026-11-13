# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration metadata of ``keycloak.authflow.ensure_approval_flow``.

The handler is in :mod:`.ops_authflow`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import KeycloakOp

__all__ = ["APPROVAL_FLOW_OPS", "ENSURE_APPROVAL_FLOW_OP_ID", "WHEN_TO_USE_AUTHFLOW_BY_GROUP"]

ENSURE_APPROVAL_FLOW_OP_ID = "keycloak.authflow.ensure_approval_flow"

_WHEN_TO_USE_AUTHFLOW = (
    "Use to give a realm that already runs the approval sign-in flow "
    "(``keycloak.authflow.ensure_approval_flow``). Keycloak reads the chart's "
    "realm file only when the realm is new, so an older realm has no flow "
    "'meho approval sign-in level'. The op makes the flow (password, then the "
    "one-time code when the person has one, at approval level 2 named "
    "'meho-approve') and binds it as the browser flow of the client "
    "'meho-cli-approve'. Safe to run twice. A flow with that "
    "name but other steps is not changed: the op stops and names the "
    "difference. It needs approval."
)

_WHEN_TO_USE_AUTHFLOW_GROUP = (
    "Use to change a realm's login flows with approval. "
    "``keycloak.authflow.ensure_approval_flow`` gives a realm the approval "
    "sign-in flow of 'meho-cli-approve'. "
    "``keycloak.authflow.ensure_passkey_code_skip`` gives a realm made before "
    "Keycloak 26.4 the step that skips the one-time code after a passkey "
    "login. ``keycloak.authflow.ensure_approval_touch`` gives the console "
    "client the login flow with the passkey touch level. All are safe to run "
    "twice and stop without a change on a flow with another shape."
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
        "flow_alias": {"type": "string", "description": "Always 'meho approval sign-in level'."},
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
        "level_setting_added": {
            "type": "boolean",
            "description": "true: this run gave the level step its settings (level 2, "
            "asked for at every sign-in).",
        },
        "level_name": {
            "type": "string",
            "description": "Always 'meho-approve': the acr of a sign-in through the flow.",
        },
        "level_changed": {
            "type": "boolean",
            "description": "true: this run named level 2 'meho-approve' in the realm's "
            "acr.loa.map. false: the realm already had it.",
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
        "level_setting_added",
        "level_name",
        "level_changed",
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
            "Makes the login flow 'meho approval sign-in level' (password, then "
            "the one-time code, at approval level 2 'meho-approve') when it is "
            "missing, names the level in the realm, and binds the flow to "
            "'meho-cli-approve'. Changes nothing when all three are there."
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
                "[{level, step, requirement}], level_setting_added, level_name, "
                "level_changed, client_id, client_uuid, "
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
