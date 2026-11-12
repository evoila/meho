# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and answer schemas, and the guidance text, of the ``spaces.*`` operations."""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "ASSIGN_PARAMETERS",
    "ASSIGN_RESPONSE",
    "DISABLE_PARAMETERS",
    "DISABLE_RESPONSE",
    "OVERVIEW_PARAMETERS",
    "OVERVIEW_RESPONSE",
    "REASSIGN_PARAMETERS",
    "REASSIGN_RESPONSE",
    "UNASSIGN_PARAMETERS",
    "UNASSIGN_RESPONSE",
]

_NULLABLE_STRING: Final[list[str]] = ["string", "null"]

_REASON: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1000,
    "description": "Why you make this change. Required. It is saved with the change.",
}

_ASSIGNMENT: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "assignment_id": {"type": "string", "description": "Id of the assignment row."},
        "space_id": {"type": "string", "description": "Id of the space it belongs to."},
        "kind": {"type": "string", "description": "target, object, service or fact."},
        "target_id": {"type": _NULLABLE_STRING, "description": "MEHO target id, if any."},
        "object_id": {"type": _NULLABLE_STRING, "description": "The vendor's own object id."},
        "name": {"type": "string", "description": "Name when the assignment was made."},
        "access": {"type": "string", "description": "own or shared_summary."},
        "valid_from": {"type": "string", "description": "When the assignment started."},
        "valid_to": {"type": _NULLABLE_STRING, "description": "When it ended; null if current."},
    },
    "required": ["assignment_id", "space_id", "kind", "name", "access", "valid_from"],
}

ASSIGN_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "space_id": {"type": "string", "description": "Id of the space that gets the system."},
        "kind": {
            "type": "string",
            "enum": ["target", "object", "service", "fact"],
            "description": (
                "What you assign: a MEHO target, a machine or other vendor object, "
                "a service, or a fact."
            ),
        },
        "target_id": {
            "type": "string",
            "description": (
                "Id of a MEHO target of your tenant. Needed for kind target. "
                "For other kinds, the target the thing runs on (optional)."
            ),
        },
        "object_id": {
            "type": "string",
            "description": "The vendor's own object id. Needed for kind object.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": (
                "Name to save with the assignment. Needed for every kind except "
                "target, where it defaults to the target's name."
            ),
        },
        "access": {
            "type": "string",
            "enum": ["own", "shared_summary"],
            "default": "own",
            "description": "own: the space owns it. shared_summary: others see a summary only.",
        },
        "reason": _REASON,
    },
    "required": ["space_id", "kind", "reason"],
    "additionalProperties": False,
    "examples": [
        {
            "space_id": "<space-id>",
            "kind": "target",
            "target_id": "<target-id>",
            "reason": "<why the space gets this target>",
        }
    ],
}

ASSIGN_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "assigned, or unchanged when the space already has it.",
        },
        "assignment": _ASSIGNMENT,
    },
    "required": ["status", "assignment"],
}

UNASSIGN_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "assignment_id": {"type": "string", "description": "Id of the current assignment."},
        "reason": _REASON,
    },
    "required": ["assignment_id", "reason"],
    "additionalProperties": False,
    "examples": [{"assignment_id": "<assignment-id>", "reason": "<why it leaves the space>"}],
}

UNASSIGN_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"status": {"type": "string", "description": "ended"}, "assignment": _ASSIGNMENT},
    "required": ["status", "assignment"],
}

REASSIGN_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "assignment_id": {"type": "string", "description": "Id of the current assignment."},
        "to_space_id": {"type": "string", "description": "Id of the space that gets it now."},
        "reason": _REASON,
    },
    "required": ["assignment_id", "to_space_id", "reason"],
    "additionalProperties": False,
    "examples": [
        {
            "assignment_id": "<assignment-id>",
            "to_space_id": "<space-id>",
            "reason": "<why it moves>",
        }
    ],
}

REASSIGN_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "moved, or unchanged when already there."},
        "ended_assignment_id": {"type": "string", "description": "The assignment that ended."},
        "assignment": _ASSIGNMENT,
    },
    "required": ["status", "ended_assignment_id", "assignment"],
}

DISABLE_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "space_id": {"type": "string", "description": "Id of the space the person leaves."},
        "member_issuer": {
            "type": "string",
            "minLength": 1,
            "description": "Keycloak issuer of the person.",
        },
        "member_sub": {
            "type": "string",
            "minLength": 1,
            "description": "Keycloak subject of the person.",
        },
        "reason": _REASON,
    },
    "required": ["space_id", "member_issuer", "member_sub", "reason"],
    "additionalProperties": False,
    "examples": [
        {
            "space_id": "<space-id>",
            "member_issuer": "<issuer-url>",
            "member_sub": "<subject>",
            "reason": "<why the person leaves>",
        }
    ],
}

DISABLE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "disabled, or unchanged when already so."},
        "space_id": {"type": "string", "description": "The space."},
        "owner_left": {
            "type": "boolean",
            "description": "True when the space has no active owner after this change.",
        },
        "assignments_marked_owner_left": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of the current assignments that now wait for reassignment.",
        },
    },
    "required": ["status", "space_id", "owner_left", "assignments_marked_owner_left"],
}

OVERVIEW_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "space_id": {
            "type": "string",
            "description": "Show only this space. Leave out to show every space of the tenant.",
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"space_id": "<space-id>"}],
}

OVERVIEW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "spaces": {
            "type": "array",
            "description": "One entry per space: owner, members and assigned systems.",
            "items": {
                "type": "object",
                "properties": {
                    "space_id": {"type": "string", "description": "Id of the space."},
                    "name": {"type": "string", "description": "Name of the space."},
                    "kind": {"type": "string", "description": "person or team."},
                    "state": {"type": "string", "description": "active or disabled."},
                    "owner": {
                        "type": "object",
                        "description": "Keycloak issuer and subject of the owner.",
                    },
                    "owner_left": {
                        "type": "boolean",
                        "description": "True when the owner has left and nobody owns it now.",
                    },
                    "members": {
                        "type": "array",
                        "description": "Current members: issuer, subject, role and state.",
                        "items": {"type": "object"},
                    },
                    "assignments": {
                        "type": "array",
                        "description": (
                            "Current assignments. A target shows its product, version, "
                            "when it was detected, and its health."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": [
                    "space_id",
                    "name",
                    "kind",
                    "state",
                    "owner",
                    "members",
                    "assignments",
                ],
            },
        },
        "count": {"type": "integer", "description": "Number of spaces in `spaces`."},
    },
    "required": ["spaces", "count"],
}

GROUP_KEY: Final[str] = "records"
GROUP_WHEN_TO_USE: Final[str] = (
    "Spaces say which person or team owns which lab, machine or service, "
    "inside one tenant. Use the writes to assign, move or end an assignment "
    "or to let a person leave a space. Use the overview to see owners, "
    "product versions and health in one call. Nothing here is deleted: "
    "changes end-date rows and the history stays."
)

_ADMIN_PRECONDITION: Final[str] = (
    "Needs a reason. A person needs the `tenant_admin` role. The call waits until "
    "a `tenant_admin` approves it. The setting SPACES_ENABLED must be on."
)

ASSIGN_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": "A lab, machine, service or fact belongs to a space and is not assigned yet.",
    "preconditions": _ADMIN_PRECONDITION
    + " Get the space id from `spaces.overview` and the target id from list_targets.",
    "parameter_hints": {
        "kind": "target needs target_id. object needs object_id and name.",
        "reason": "Say why, in one plain sentence.",
    },
    "output_shape": "{'status': 'assigned' | 'unchanged', 'assignment': {...}}",
    "next_step": "Run `spaces.overview` to see the space with the new assignment.",
}

UNASSIGN_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": "A system no longer belongs to any space. The row stays, with an end time.",
    "preconditions": _ADMIN_PRECONDITION
    + " Get the assignment id from `spaces.overview`. A second call for an ended "
    "assignment fails with not found.",
    "parameter_hints": {"assignment_id": "From `spaces.overview`. Must be current."},
    "output_shape": "{'status': 'ended', 'assignment': {...}}",
    "next_step": "Use `spaces.assign` to give the system to another space.",
}

REASSIGN_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": "A system moves from one space to another, for example after an owner left.",
    "preconditions": _ADMIN_PRECONDITION
    + " Get both ids from `spaces.overview`. The old row ends and a new one starts "
    "in one step.",
    "parameter_hints": {"to_space_id": "The space that gets the system."},
    "output_shape": "{'status': 'moved', 'ended_assignment_id', 'assignment': {...}}",
    "next_step": "Run `spaces.overview` to check both spaces.",
}

DISABLE_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": (
        "A person leaves a lab. Their membership in this one space ends. "
        "When they were the owner, the assignments of the space wait for reassignment."
    ),
    "preconditions": _ADMIN_PRECONDITION
    + " This never changes the person's company account. Disable that "
    "account in a separate step.",
    "parameter_hints": {"member_issuer": "Keycloak issuer", "member_sub": "Keycloak subject"},
    "output_shape": (
        "{'status', 'space_id', 'owner_left', 'assignments_marked_owner_left': [ids]}"
    ),
    "next_step": "Use `spaces.reassign` for each assignment id that is listed.",
}

OVERVIEW_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": ("See who owns which lab, its product version and its health, in one call."),
    "parameter_hints": {"space_id": "Optional. One space only."},
    "output_shape": "{'spaces': [{space_id, name, owner, members, assignments}], 'count'}",
    "next_step": (
        "Use `spaces.reassign` for an assignment of a space whose owner has left. "
        "Health `unknown` means no check ran in the last two intervals."
    ),
    "result_scalars": {"keys": ["count"]},
}
