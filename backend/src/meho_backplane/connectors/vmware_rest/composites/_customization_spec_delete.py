# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Delete one saved guest customization spec (``guest.customization_spec.delete``).

``vmware.composite.guest.customization_spec.delete`` is ``destructive`` and
waits for approval: a person asks for a preview, then a second person
approves. An agent is always refused (the destructive risk level). It follows
the plan-then-act seam of :mod:`._teardown`:

1. Plan (read-only): ``GET /vcenter/guest/customization-specs/{name}``. The
   approval preview and the run use the same plan.
2. The preview refuses a spec that does not exist: the builder raises, so the
   call does not wait for approval and the person reads why.
3. The run: a spec that is gone by now gives ``unchanged`` (nothing sent).
   Otherwise one governed ``DELETE /vcenter/guest/customization-specs/{name}``
   through the shared write seam (:func:`._write._write_sub_op`), then the
   spec is read back: gone gives ``deleted``, still there gives
   ``still_present``.

No VM keeps a link to a spec: vCenter copies the settings into a VM when it
clones or deploys it. So the blast radius has no children; it says so. Only
names and dates of the spec reach the preview and the answer, never its
settings, so no secret of the spec leaves MEHO.

Calls vSphere REST on the connector session, so it needs no catalog ingest
and works on vCenter 8 and 9.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_DELETED,
    STATUS_STILL_PRESENT,
    TeardownPlan,
    blast_radius,
    envelope,
    pre_write_outcome,
)
from meho_backplane.connectors.vmware_rest.composites._write import _write_sub_op
from meho_backplane.connectors.vmware_rest.customization_specs import read_spec
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "CUSTOMIZATION_SPEC_DELETE_COMPOSITES",
    "CUSTOMIZATION_SPEC_DELETE_GOVERNED_SUBOPS",
    "OP_ID_CUSTOMIZATION_SPEC_DELETE",
    "SUB_OPS_CUSTOMIZATION_SPEC_DELETE",
    "customization_spec_delete_composite",
    "customization_spec_delete_preview",
]

OP_ID_CUSTOMIZATION_SPEC_DELETE: Final = "vmware.composite.guest.customization_spec.delete"

_OP_GET_SPEC: Final = "GET:/vcenter/guest/customization-specs/{name}"
_OP_DELETE_SPEC: Final = "DELETE:/vcenter/guest/customization-specs/{name}"

#: Every REST call of the delete. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files; ``_governed_subops`` lists them for
#: the grant set (the DELETE is delete-shaped, so it is never grantable).
SUB_OPS_CUSTOMIZATION_SPEC_DELETE: Final[tuple[str, ...]] = (_OP_GET_SPEC, _OP_DELETE_SPEC)

_NO_CHILDREN_NOTE: Final = (
    "No VM keeps a link to a spec: vCenter copied its settings into each VM made with it. "
    "Those VMs do not change."
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def plan_customization_spec_delete(
    connector: VmwareRestConnector, target: Any, operator: Operator, name: str
) -> TeardownPlan:
    """Read the spec and plan its delete. Read-only; secrets never reach the plan."""
    obj: dict[str, Any] = {"kind": "guest_customization_spec", "name": name}
    info = await read_spec(connector, operator, target, name)
    if info is None:
        return TeardownPlan(obj, False)
    raw_spec = info.get("spec")
    spec: Mapping[str, Any] = raw_spec if isinstance(raw_spec, Mapping) else {}
    obj.update(
        description=_text(spec.get("description")),
        last_modified=_text(info.get("last_modified")),
        note=_NO_CHILDREN_NOTE,
    )
    return TeardownPlan(obj, True)


async def customization_spec_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one guest customization spec by name. Op-id: ``guest.customization_spec.delete``."""
    name = str(params["name"])
    plan = await plan_customization_spec_delete(connector, target, operator, name)
    early = pre_write_outcome(
        plan, absent_guidance=f"vCenter has no spec named {name!r}; nothing was deleted"
    )
    if early is not None:
        return early
    gate, _ = await _write_sub_op(connector, target, operator, _OP_DELETE_SPEC, {"name": name})
    if gate is not None:
        return gate
    if await read_spec(connector, operator, target, name) is not None:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            guidance="vCenter accepted the delete but the spec still reads back; check again",
        )
    return envelope(plan, STATUS_DELETED)


async def customization_spec_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of the delete: the spec, no children, cannot be undone.

    Raises :exc:`LookupError` when the spec does not exist, so the call is
    refused before it waits for approval.
    """
    name = ctx.params.get("name")
    if not isinstance(name, str) or ctx.connector_instance is None:
        return None
    plan = await plan_customization_spec_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        name,
    )
    if not plan.present:
        raise LookupError(f"vCenter has no guest customization spec named {name!r}")
    return blast_radius(plan)


register_preview_builder(OP_ID_CUSTOMIZATION_SPEC_DELETE, customization_spec_delete_preview)


# ---------------------------------------------------------------------------
# Schemas, AI guidance and the registration row
# ---------------------------------------------------------------------------

_NULLABLE_TEXT: Final[dict[str, Any]] = {"type": ["string", "null"]}

CUSTOMIZATION_SPEC_DELETE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Name of the spec to delete, exactly as vmware.guest.customization_spec.list "
                "shows it."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
    "examples": [{"name": "<spec-name>"}],
}

CUSTOMIZATION_SPEC_DELETE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["deleted", "unchanged", "still_present"],
            "description": (
                "deleted: the spec is gone (read back); unchanged: there was no such spec, "
                "nothing was sent; still_present: vCenter accepted the delete but the spec "
                "still reads back."
            ),
        },
        "object": {
            "type": "object",
            "description": "The spec: kind, name, description, last_modified and a note.",
            "properties": {
                "kind": {"type": "string", "description": "Always guest_customization_spec."},
                "name": {"type": "string", "description": "Name of the spec."},
                "description": {**_NULLABLE_TEXT, "description": "Description of the spec."},
                "last_modified": {**_NULLABLE_TEXT, "description": "When it was last changed."},
                "note": {**_NULLABLE_TEXT, "description": "What else is affected: nothing."},
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Always empty: nothing blocks a spec delete.",
        },
        "task": {**_NULLABLE_TEXT, "description": "Always null: the delete has no vCenter task."},
        "task_state": {**_NULLABLE_TEXT, "description": "Always null."},
        "guidance": {**_NULLABLE_TEXT, "description": "What happened and what to do next."},
    },
    "required": ["status", "object"],
}

CUSTOMIZATION_SPEC_DELETE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to remove a saved guest customization spec that is no longer used. VMs made "
        "with it keep their settings."
    ),
    "preconditions": (
        "Check the name with vmware.guest.customization_spec.list. A person must run it: "
        "first preview_operation, then call_operation with the preview_hash, then a second "
        "person approves. An agent is always refused. A name that does not exist is refused "
        "at the preview."
    ),
    "parameter_hints": {"name": "The exact spec name; letter case counts."},
    "output_shape": (
        "{status: deleted|unchanged|still_present, object: {kind, name, description, "
        "last_modified, note}, blockers, task, task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a second run gives unchanged. Confirm with "
        "vmware.guest.customization_spec.list. It cannot be undone; save a new spec with "
        "vmware.composite.guest.customization_spec.create if needed."
    ),
}

#: The registration row, as ``_CompositeSpec`` keyword arguments.
CUSTOMIZATION_SPEC_DELETE_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_CUSTOMIZATION_SPEC_DELETE,
        "handler": customization_spec_delete_composite,
        "summary": "Delete one saved guest customization spec by name; it cannot be undone.",
        "description": (
            "Deletes a saved guest customization spec from vCenter, by name, and reads back "
            "that it is gone. VMs made with it do not change. A second person always "
            "approves, after a preview."
        ),
        "parameter_schema": CUSTOMIZATION_SPEC_DELETE_PARAMETER_SCHEMA,
        "response_schema": CUSTOMIZATION_SPEC_DELETE_RESPONSE_SCHEMA,
        "group_key": "guest",
        "tags": ["composite", "write", "guest", "customization", "delete"],
        "safety_level": "destructive",
        "requires_approval": True,
        "llm_instructions": CUSTOMIZATION_SPEC_DELETE_LLM_INSTRUCTIONS,
    },
)

#: The governed REST calls of the delete, for ``_governed_subops``.
CUSTOMIZATION_SPEC_DELETE_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_CUSTOMIZATION_SPEC_DELETE: SUB_OPS_CUSTOMIZATION_SPEC_DELETE,
}
