# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration metadata of ``keycloak.authflow.ensure_passkey_code_skip``.

The handler is in :mod:`.ops_authflow_passkey`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import KeycloakOp

__all__ = ["ENSURE_PASSKEY_CODE_SKIP_OP_ID", "PASSKEY_CODE_SKIP_OPS"]

ENSURE_PASSKEY_CODE_SKIP_OP_ID = "keycloak.authflow.ensure_passkey_code_skip"

_WHEN_TO_USE = (
    "Use when a realm made before Keycloak 26.4 asks for the one-time code "
    "after a passkey login. Keycloak 26.4 puts the step 'Condition - "
    "credential' into the browser flow of new realms only. The op copies "
    "the built-in 'browser' flow as 'browser with passkeys', adds the step "
    "to the copy, and binds the copy as the realm's browser flow. It "
    "changes how every person of the realm logs in, so it needs approval. "
    "Safe to run twice. A custom browser flow is not changed: the op stops."
)

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "level": {"type": ["integer", "null"]},
        "step": {"type": "string"},
        "requirement": {"type": ["string", "null"]},
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The target's managed realm."},
        "previous_browser_flow": {
            "type": "string",
            "description": "The realm's browser flow before the run.",
        },
        "browser_flow": {
            "type": "string",
            "description": "The realm's browser flow after the run, as read back.",
        },
        "changed": {
            "type": "boolean",
            "description": "false: the step was already there and nothing was written.",
        },
        "flow_copied": {
            "type": "boolean",
            "description": "true: this run copied 'browser' as 'browser with passkeys'.",
        },
        "step_added": {
            "type": "boolean",
            "description": "true: this run added the step to the copy.",
        },
        "binding_changed": {
            "type": "boolean",
            "description": "true: this run bound the copy as the realm's browser flow.",
        },
        "unrequested_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Realm fields other than browserFlow that read back changed after "
                "the bind. Names only, never values. Normally empty."
            ),
        },
        "flow_steps": {
            "type": "array",
            "description": "The steps of the browser flow, as read back from Keycloak.",
            "items": _STEP_SCHEMA,
        },
    },
    "required": [
        "realm",
        "previous_browser_flow",
        "browser_flow",
        "changed",
        "flow_copied",
        "step_added",
        "binding_changed",
        "unrequested_changes",
        "flow_steps",
    ],
}

PASSKEY_CODE_SKIP_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=ENSURE_PASSKEY_CODE_SKIP_OP_ID,
        handler_attr="authflow_ensure_passkey_code_skip",
        summary="Skip the one-time code after a passkey login on a realm made before 26.4.",
        description=(
            "Copies the built-in 'browser' flow as 'browser with passkeys', adds "
            "'Condition - credential' to its one-time code sub-flow, and binds "
            "the copy as the realm's browser flow. Changes nothing when the step "
            "is there. The way back: keycloak.realm.update with "
            '{"browserFlow": "browser"}.'
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="authflow_write",
        tags=("login-flow", "passkey", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE,
            "when_to_call": (
                "When people of a realm that was made before Keycloak 26.4 log "
                "in with a passkey and are still asked for the one-time code. "
                "Also to check a realm: a run on a realm that has the step "
                "changes nothing."
            ),
            "preconditions": (
                "The Keycloak server runs 26.4 or newer, so the step "
                "'conditional-credential' exists. The realm's browser flow is "
                "the built-in 'browser' flow."
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{realm, previous_browser_flow, browser_flow, changed, "
                "flow_copied, step_added, binding_changed, unrequested_changes, "
                "flow_steps: [{level, step, requirement}]}``."
            ),
            "next_step": (
                "changed false: the realm already had the step. An error with "
                "'custom_flow' or 'no_code_step' changed nothing; a person adds "
                "the step by hand in the Keycloak admin console. An error with "
                "'other_steps' names how an existing 'browser with passkeys' "
                "flow differs; delete or rename it and run again. To undo, run "
                'keycloak.realm.update with {"browserFlow": "browser"}.'
            ),
        },
    ),
)
