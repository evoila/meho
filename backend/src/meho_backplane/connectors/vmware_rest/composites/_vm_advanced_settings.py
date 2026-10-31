# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vm.advanced_settings.set`` -- set or remove VM advanced settings.

``dangerous``, waits for approval. The advanced settings are the VM's
``extraConfig`` keys. vCenter changes them only through the VIM API, so the
flow is:

1. Read ``config.extraConfig`` (:func:`read_extra_config`).
2. Send nothing when every key already has the asked value, or a key to
   remove is not there (``unchanged``).
3. One governed ``ReconfigVM_Task`` whose ``VirtualMachineConfigSpec`` carries
   only the changed keys as ``OptionValue`` rows. vSphere removes a key whose
   value is empty, so a removal sends ``""``.
4. Read ``config.extraConfig`` again and compare.

A key whose value may hold a secret (:func:`is_secret_key`) cannot be set:
MEHO does not take secrets as plain values. Such a key can be removed. Its
values are masked in every answer. vSphere ignores keys that clash with
settings that have their own field; then the answer is ``partial``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._vm_change_common import (
    RECONFIG_VM_TASK_CALL,
    VM_ID_PROPERTY,
    reconfigure_vm,
    timeout_answer,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_advanced_settings import (
    ADVANCED_SETTINGS_VIM_CALLS,
    is_secret_key,
    read_extra_config,
    setting_row,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, VIM_VALUE_KEY
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VIM_SUB_OPS_VM_ADVANCED_SETTINGS_SET",
    "VM_ADVANCED_SETTINGS_COMPOSITES",
    "vm_advanced_settings_set_composite",
]

OP_ID: Final = "vmware.composite.vm.advanced_settings.set"

#: The VIM calls of the change: the ``extraConfig`` read and the ``ReconfigVM_Task``.
VIM_SUB_OPS_VM_ADVANCED_SETTINGS_SET: Final[tuple[str, ...]] = (
    *ADVANCED_SETTINGS_VIM_CALLS,
    RECONFIG_VM_TASK_CALL,
)

#: An ``extraConfig`` key: letters, digits and ``. _ - :``.
KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$(?!\n)"


def _requested(params: dict[str, Any]) -> dict[str, str]:
    """``{key: value}``; a removal is the empty value vSphere reads as "remove"."""
    wanted = {row["key"]: row["value"] for row in params.get("settings") or []}
    wanted.update(dict.fromkeys(params.get("remove") or [], ""))
    return wanted


def _problem(params: dict[str, Any]) -> str | None:
    """Why the request cannot be sent, or ``None``."""
    set_keys = [row["key"] for row in params.get("settings") or []]
    remove_keys = list(params.get("remove") or [])
    if not set_keys and not remove_keys:
        return "pass at least one entry in settings or remove"
    every = set_keys + remove_keys
    if len(every) != len(set(every)):
        return "each key may appear only once across settings and remove"
    secret = sorted(key for key in set_keys if is_secret_key(key))
    if secret:
        return (
            f"MEHO does not set keys that may hold a secret ({', '.join(secret)}); it takes "
            "no secret as a plain value. Such keys can only be removed here"
        )
    return None


def _changes(wanted: dict[str, str], current: dict[str, Any]) -> dict[str, str]:
    """The keys that differ from the VM now; a removal of a missing key is no change."""
    changes: dict[str, str] = {}
    for key, value in wanted.items():
        if value == "":
            if key in current:
                changes[key] = value
        elif current.get(key) != value:
            changes[key] = value
    return changes


def _change_rows(
    changes: dict[str, str], before: dict[str, Any], after: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """One row per changed key, with masked values where they may hold a secret."""
    rows = []
    for key in sorted(changes):
        row: dict[str, Any] = {
            "key": key,
            "action": "remove" if changes[key] == "" else "set",
            "before": setting_row(key, before.get(key))["value"],
            "after": None if after is None else setting_row(key, after.get(key))["value"],
            "masked": is_secret_key(key),
        }
        rows.append(row)
    return rows


def _option_values(changes: dict[str, str]) -> list[dict[str, Any]]:
    """``OptionValue`` rows; ``value`` is an ``Any`` field, so the string is boxed."""
    return [
        {
            VIM_TYPE_NAME_KEY: "OptionValue",
            "key": key,
            "value": {VIM_TYPE_NAME_KEY: "string", VIM_VALUE_KEY: value},
        }
        for key, value in sorted(changes.items())
    ]


def _applied(changes: dict[str, str], after: dict[str, Any]) -> bool:
    return all(
        (key not in after) if value == "" else after.get(key) == value
        for key, value in changes.items()
    )


async def vm_advanced_settings_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set or remove VM advanced settings. Op-id: ``vmware.composite.vm.advanced_settings.set``."""
    vm = check_vm_moid(params["vm"])
    envelope: dict[str, Any] = {
        "vm": vm,
        "vm_name": None,
        "changes": [],
        "task": None,
        "guidance": None,
    }
    problem = _problem(params)
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": problem}
    vm_name, before = await read_extra_config(connector, operator, target, vm, op_id=OP_ID)
    envelope["vm_name"] = vm_name
    changes = _changes(_requested(params), before)
    if not changes:
        return {
            **envelope,
            "status": "unchanged",
            "guidance": "Every key already has the asked value; nothing was sent.",
        }
    envelope["changes"] = _change_rows(changes, before, None)
    gate, task, timed_out = await reconfigure_vm(
        connector,
        target,
        operator,
        vm=vm,
        spec_fields={"extraConfig": _option_values(changes)},
        params={"keys": sorted(changes), "remove": sorted(k for k, v in changes.items() if not v)},
        label="vm.advanced_settings.set",
    )
    if gate is not None:
        return gate
    envelope["task"] = task
    if timed_out:
        return timeout_answer(
            envelope, task or "", "Read the settings with vmware.vm.advanced_settings.show."
        )
    _, after = await read_extra_config(connector, operator, target, vm, op_id=OP_ID)
    envelope["changes"] = _change_rows(changes, before, after)
    if not _applied(changes, after):
        return {
            **envelope,
            "status": "partial",
            "guidance": "vCenter took the change, but some keys read back different. vSphere "
            "ignores keys that clash with settings that have their own field.",
        }
    return {**envelope, "status": "set"}


async def _vm_advanced_settings_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the keys that would change, with their values now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    problem = _problem(ctx.params)
    if problem is not None:
        return {"summary": f"MEHO will send nothing: {problem}."}
    try:
        vm = check_vm_moid(ctx.params["vm"])
        _, current = await read_extra_config(connector, ctx.operator, ctx.target, vm, op_id=OP_ID)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its advanced settings.",
            "read_error": preview_failure_reason(exc),
        }
    changes = _changes(_requested(ctx.params), current)
    rows = _change_rows(changes, current, None)
    for row in rows:
        row["after"] = None if row["masked"] or row["action"] == "remove" else changes[row["key"]]
    summary = (
        f"Change {len(rows)} advanced setting(s) of {vm}."
        if rows
        else f"The advanced settings of {vm} already match; the call will change nothing."
    )
    return {"summary": summary, "vm": vm, "changes": rows}


register_preview_builder(OP_ID, _vm_advanced_settings_set_preview)


# ---------------------------------------------------------------------------
# Schemas, AI guidance and the registration row
# ---------------------------------------------------------------------------

_KEY_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": KEY_PATTERN,
    "description": "Setting key, such as 'disk.EnableUUID'.",
}

VM_ADVANCED_SETTINGS_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **VM_ID_PROPERTY,
        "settings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "key": _KEY_FIELD,
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                        "description": "New value as text, such as 'TRUE'. Never a secret.",
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
                "description": "One key to set.",
            },
            "description": "Keys to add or change.",
        },
        "remove": {
            "type": "array",
            "minItems": 1,
            "items": {**_KEY_FIELD, "description": "Setting key to remove."},
            "description": "Keys to remove.",
        },
    },
    "required": ["vm"],
    "anyOf": [{"required": ["settings"]}, {"required": ["remove"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "settings": [{"key": "disk.EnableUUID", "value": "TRUE"}]},
        {"vm": "vm-42", "remove": ["guestinfo.example"]},
    ],
}

_NULLABLE_STR: Final = {"type": ["string", "null"]}

VM_ADVANCED_SETTINGS_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["set", "unchanged", "partial", "invalid_request", "timeout"],
            "description": (
                "set: done and read back; unchanged: nothing to do, nothing sent; partial: "
                "sent, but some keys read back different; invalid_request: MEHO sent "
                "nothing; timeout: the task did not finish in time."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "vm_name": {**_NULLABLE_STR, "description": "VM name."},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Setting key."},
                    "action": {
                        "type": "string",
                        "enum": ["set", "remove"],
                        "description": "What MEHO asked for this key.",
                    },
                    "before": {**_NULLABLE_STR, "description": "Value before; null if none."},
                    "after": {**_NULLABLE_STR, "description": "Value read back; null if none."},
                    "masked": {
                        "type": "boolean",
                        "description": "True when the values may hold a secret and are hidden.",
                    },
                },
                "description": "One changed key.",
            },
            "description": "The keys MEHO changed.",
        },
        "task": {**_NULLABLE_STR, "description": "The ReconfigVM_Task id."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_ADVANCED_SETTINGS_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to add, change or remove VM advanced settings (extraConfig keys), for example "
        "disk.EnableUUID=TRUE for Kubernetes storage, isolation.tools.* hardening keys, or "
        "guestinfo.* values for the guest."
    ),
    "preconditions": (
        "Read the settings first with vmware.vm.advanced_settings.show. The change takes "
        "the VM id, not a name. Many keys take effect only after the next power cycle. Keys "
        "that may hold a secret (password, token, user data) cannot be set here. The change "
        "waits for approval."
    ),
    "parameter_hints": {
        "settings": "[{key: 'disk.EnableUUID', value: 'TRUE'}]; values are text.",
        "remove": "['guestinfo.example']; a key that is not there is skipped.",
    },
    "output_shape": (
        "{status: set|unchanged|partial|invalid_request|timeout, vm, vm_name, changes: "
        "[{key, action: set|remove, before, after, masked}], task, guidance}."
    ),
    "next_step": (
        "Safe to run twice: keys that already match are not sent. Power cycle the VM when "
        "the key needs it. On partial, the key may clash with a setting that has its own "
        "operation (for example CPU or memory)."
    ),
}

VM_ADVANCED_SETTINGS_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID,
        "handler": vm_advanced_settings_set_composite,
        "summary": "Set or remove VM advanced settings (extraConfig keys).",
        "description": (
            "Adds, changes or removes advanced settings (extraConfig keys) of a VM, named by "
            "its VM id. Sends only keys that differ and reads them back. Keys that may hold "
            "a secret cannot be set."
        ),
        "parameter_schema": VM_ADVANCED_SETTINGS_SET_PARAMETER_SCHEMA,
        "response_schema": VM_ADVANCED_SETTINGS_SET_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "advanced-settings"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_ADVANCED_SETTINGS_SET_LLM_INSTRUCTIONS,
    },
)
