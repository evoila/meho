# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Salt key changes: accept, reject and delete minion keys."""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.salt._op_common import (
    GROUP_MINIONS,
    KEY_STATES,
    WRITE_TAGS,
    SaltTypedOp,
    _text,
)

__all__ = ["KEY_ACCEPT_OP", "KEY_DELETE_OP", "KEY_REJECT_OP", "KEY_WRITE_OPS"]

#: Most keys one call may change.
MAX_KEYS = 50

#: An exact master or minion id: no space, comma or glob character.
_ID_PATTERN = r"^[^\s,*?\[\]]+$"

_MINIONS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_KEYS,
    "description": (
        f"The keys to change, 1 to {MAX_KEYS}, each named by its master and minion id "
        "exactly as salt.key.list shows them. Glob characters are refused."
    ),
    "items": {
        "type": "object",
        "description": "One minion key.",
        "properties": {
            "master_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": _ID_PATTERN,
                "description": "Master the key belongs to (master_id from salt.key.list).",
            },
            "minion_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": _ID_PATTERN,
                "description": "Minion id of the key (minion_id from salt.key.list).",
            },
        },
        "required": ["master_id", "minion_id"],
        "additionalProperties": False,
    },
}

_EXAMPLE_MINIONS = [{"master_id": "<master-id>", "minion_id": "<minion-id>"}]

_KEY_STATE_OR_NULL: dict[str, Any] = {
    "type": ["string", "null"],
    "enum": [*KEY_STATES, None],
    "description": "Key state; null when the master has no key for the minion.",
}

_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["requested", "precondition_failed"],
            "description": (
                "requested: RaaS took the change. precondition_failed: the keys differ "
                "from what the approver saw, and nothing was changed."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["accept", "reject", "delete"],
            "description": "The change that was asked for.",
        },
        "reason": _text("Why nothing was changed; null otherwise."),
        "changed_count": {
            "type": "integer",
            "description": "Keys whose state differs after the change.",
        },
        "keys": {
            "type": "array",
            "description": "One row per key, in the order of the call.",
            "items": {
                "type": "object",
                "description": "One key before and after.",
                "properties": {
                    "master_id": _text("Master id."),
                    "minion_id": _text("Minion id."),
                    "key_state_before": _KEY_STATE_OR_NULL,
                    "key_state_after": _KEY_STATE_OR_NULL,
                },
            },
        },
    },
}

_OUTPUT_SHAPE = (
    "{status, action, reason, changed_count, keys: [{master_id, minion_id, "
    "key_state_before, key_state_after}]}. key_state_after is read right after the "
    "change; the master applies it, so a key can show its old state for a few seconds."
)

_RESULT_SCALARS = {"keys": ["status", "action", "reason", "changed_count"]}


def _flag(description: str) -> dict[str, Any]:
    return {"type": "boolean", "default": False, "description": description}


KEY_ACCEPT_OP = SaltTypedOp(
    op_id="salt.key.accept",
    handler_attr="key_accept",
    summary="Accept pending Salt minion keys, so the minions can get jobs.",
    description=(
        "Calls minions.set_minion_key_state with state accept for the named keys. "
        "Without the include flags only pending keys are accepted. Reads the key states "
        "before and after. Waits for a person's approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "minions": _MINIONS_SCHEMA,
            "include_rejected": _flag("Also accept keys that are rejected now."),
            "include_denied": _flag("Also accept keys that are denied now."),
        },
        "required": ["minions"],
        "additionalProperties": False,
        "examples": [{"minions": _EXAMPLE_MINIONS}],
    },
    response_schema=_RESPONSE,
    group_key=GROUP_MINIONS,
    tags=(*WRITE_TAGS, "minion"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a new machine's minion key is pending (salt.key.list with "
            "key_state pending) and the operator wants Salt to manage that machine."
        ),
        "preconditions": (
            "Each key exists on its master (salt.key.list); MEHO refuses the whole call "
            "when one does not. Check that the minion id is the machine you expect: an "
            "accepted minion gets every job its targets match. The call waits until a "
            "second person approves it."
        ),
        "output_shape": _OUTPUT_SHAPE,
        "next_step": (
            "Check that key_state_after is accepted. Then salt.minion.list shows the "
            "minion's presence once it connects."
        ),
        "result_scalars": _RESULT_SCALARS,
    },
)

KEY_REJECT_OP = SaltTypedOp(
    op_id="salt.key.reject",
    handler_attr="key_reject",
    summary="Reject Salt minion keys, so the minions get no jobs.",
    description=(
        "Calls minions.set_minion_key_state with state reject for the named keys. "
        "Without the include flags only pending keys are rejected; include_accepted also "
        "cuts off minions that run now. Reads the key states before and after."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "minions": _MINIONS_SCHEMA,
            "include_accepted": _flag(
                "Also reject keys that are accepted now. Those minions stop getting jobs."
            ),
            "include_denied": _flag("Also reject keys that are denied now."),
        },
        "required": ["minions"],
        "additionalProperties": False,
        "examples": [{"minions": _EXAMPLE_MINIONS}],
    },
    response_schema=_RESPONSE,
    group_key=GROUP_MINIONS,
    tags=(*WRITE_TAGS, "minion"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a pending key comes from a machine Salt must not manage, or, with "
            "include_accepted, to cut a managed machine off from Salt."
        ),
        "preconditions": (
            "Each key exists on its master (salt.key.list); MEHO refuses the whole call "
            "when one does not. The call waits until a second person approves it; an "
            "agent needs a grant for this risk level."
        ),
        "output_shape": _OUTPUT_SHAPE,
        "next_step": (
            "Check that key_state_after is rejected. To let the minion in later, call "
            "salt.key.accept with include_rejected true."
        ),
        "result_scalars": _RESULT_SCALARS,
    },
)

KEY_DELETE_OP = SaltTypedOp(
    op_id="salt.key.delete",
    handler_attr="key_delete",
    summary="Delete Salt minion keys from their masters.",
    description=(
        "Calls minions.set_minion_key_state with state delete for the named keys. The "
        "minions get no jobs until they send their keys again and a person accepts them. "
        "Needs a preview; the approval card lists each key and its state."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"minions": _MINIONS_SCHEMA},
        "required": ["minions"],
        "additionalProperties": False,
        "examples": [{"minions": _EXAMPLE_MINIONS}],
    },
    response_schema=_RESPONSE,
    group_key=GROUP_MINIONS,
    tags=(*WRITE_TAGS, "minion", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a machine is gone for good, or when its key must be replaced "
            "(the machine was rebuilt with the same minion id)."
        ),
        "preconditions": (
            "Each key exists on its master (salt.key.list). Call preview_operation with "
            "the same inputs and pass its preview_hash to call_operation. The card lists "
            "each key with its state. Only a person can run this; an agent is refused. "
            "When a key or its state changed after the approval, the run stops with "
            "status precondition_failed and deletes nothing."
        ),
        "output_shape": _OUTPUT_SHAPE,
        "next_step": (
            "Check that key_state_after is null for each key. If the machine still runs "
            "a minion, it sends its key again; it then shows as pending in salt.key.list."
        ),
        "result_scalars": _RESULT_SCALARS,
    },
)

KEY_WRITE_OPS: tuple[SaltTypedOp, ...] = (KEY_ACCEPT_OP, KEY_REJECT_OP, KEY_DELETE_OP)
