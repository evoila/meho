# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the vApp operations.

``vapp.create``, ``vapp.power`` and ``vapp.start_order.set``
(:mod:`._vapp_change`) and ``vapp.delete`` (:mod:`._vapp_delete`). Plain data,
no I/O.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)

__all__ = [
    "RESOURCE_POOL_MOID_PATTERN",
    "START_ACTIONS",
    "STOP_ACTIONS",
    "VAPP_CREATE_LLM_INSTRUCTIONS",
    "VAPP_CREATE_PARAMETER_SCHEMA",
    "VAPP_CREATE_RESPONSE_SCHEMA",
    "VAPP_DELETE_LLM_INSTRUCTIONS",
    "VAPP_DELETE_PARAMETER_SCHEMA",
    "VAPP_DELETE_RESPONSE_SCHEMA",
    "VAPP_MOID_PATTERN",
    "VAPP_POWER_LLM_INSTRUCTIONS",
    "VAPP_POWER_PARAMETER_SCHEMA",
    "VAPP_POWER_RESPONSE_SCHEMA",
    "VAPP_START_ORDER_SET_LLM_INSTRUCTIONS",
    "VAPP_START_ORDER_SET_PARAMETER_SCHEMA",
    "VAPP_START_ORDER_SET_RESPONSE_SCHEMA",
]

#: vApp ids (a vApp is a kind of resource pool), e.g. ``resgroup-v42``.
VAPP_MOID_PATTERN: Final = r"^resgroup-v[0-9]+$"
#: Plain resource pool ids, e.g. ``resgroup-8``; a vApp id is not one.
RESOURCE_POOL_MOID_PATTERN: Final = r"^resgroup-[0-9]+$"

#: The values vCenter accepts for a VM in a vApp (``VAppEntityConfigInfo``).
START_ACTIONS: Final = ("none", "powerOn")
STOP_ACTIONS: Final = ("none", "powerOff", "guestShutdown", "suspend")

_NULLABLE_STR: Final = {"type": ["string", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}

_VAPP_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VAPP_MOID_PATTERN,
    "description": (
        "vApp id, such as 'resgroup-v42'. A change takes no vApp name, so after "
        "approval it runs on the vApp the approver saw. vapp.create returns it; "
        "vmware.object.collect reads it from a VM's 'parentVApp' property."
    ),
}

_VM_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One VM in the vApp, with its start and stop settings.",
    "properties": {
        "vm": {"type": "string", "description": "VM id."},
        "name": {**_NULLABLE_STR, "description": "VM name."},
        "power_state": {**_NULLABLE_STR, "description": "poweredOn, poweredOff or suspended."},
        "start_order": {**_NULLABLE_INT, "description": "Start group; lower starts first."},
        "start_delay": {**_NULLABLE_INT, "description": "Seconds to wait before the next."},
        "wait_for_guest": {
            "type": ["boolean", "null"],
            "description": "Wait for VMware Tools in the guest before the next VM starts.",
        },
        "start_action": {**_NULLABLE_STR, "description": "none or powerOn."},
        "stop_action": {
            **_NULLABLE_STR,
            "description": "none, powerOff, guestShutdown or suspend.",
        },
        "stop_delay": {**_NULLABLE_INT, "description": "Seconds to wait after a stop."},
    },
}

_VAPP_VIEW_FIELDS: Final[dict[str, Any]] = {
    "vapp": {**_NULLABLE_STR, "description": "vApp id."},
    "name": {**_NULLABLE_STR, "description": "vApp name as vCenter shows it."},
    "state": {
        **_NULLABLE_STR,
        "description": "vApp state now: started, stopped, starting or stopping.",
    },
    "vms": {
        "type": "array",
        "items": _VM_ROW,
        "description": "The VMs in the vApp, one row each, in start order.",
    },
}

_TASK_FIELDS: Final[dict[str, Any]] = {
    "task": {**_NULLABLE_STR, "description": "Id of the vCenter task, when one ran."},
    "task_state": {**_NULLABLE_STR, "description": "Last state of the vCenter task."},
    "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
}

# ---------------------------------------------------------------------------
# vapp.create
# ---------------------------------------------------------------------------

VAPP_CREATE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "Name of the new vApp. Must be free in the parent resource pool.",
        },
        "cluster": {
            "type": "string",
            "pattern": CLUSTER_MOID_PATTERN,
            "description": (
                "Cluster id, such as 'domain-c7'. The vApp goes into the cluster's root "
                "resource pool. Give this or 'resource_pool', not both."
            ),
        },
        "resource_pool": {
            "type": "string",
            "pattern": RESOURCE_POOL_MOID_PATTERN,
            "description": (
                "Resource pool id, such as 'resgroup-8'. The vApp goes into this pool. "
                "Give this or 'cluster', not both."
            ),
        },
        "folder": {
            "type": "string",
            "pattern": VM_FOLDER_MOID_PATTERN,
            "description": (
                "Id of the VM folder that shows the vApp, such as 'group-v3' (the "
                "datacenter's VM folder or one below it). vmware.folder.list lists them."
            ),
        },
        "annotation": {
            "type": "string",
            "maxLength": 1024,
            "description": "Optional note about the vApp, shown in vCenter.",
        },
    },
    "required": ["name", "folder"],
    "oneOf": [{"required": ["cluster"]}, {"required": ["resource_pool"]}],
    "additionalProperties": False,
    "examples": [
        {"name": "<vapp-name>", "cluster": "domain-c7", "folder": "group-v3"},
        {"name": "<vapp-name>", "resource_pool": "resgroup-8", "folder": "group-v3"},
    ],
}

VAPP_CREATE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "created",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "not_confirmed",
            ],
            "description": (
                "created: the new vApp reads back; unchanged: a vApp with this name is in the "
                "pool already, nothing sent; not_found: no such cluster, pool or folder; "
                "invalid_request: a bad id; precondition_failed: the name is used by a "
                "resource pool, or the folder is not a VM folder, nothing sent; "
                "not_confirmed: vCenter answered but the vApp does not read back."
            ),
        },
        "resource_pool": {**_NULLABLE_STR, "description": "Id of the parent resource pool."},
        "folder": {**_NULLABLE_STR, "description": "Id of the VM folder."},
        **_VAPP_VIEW_FIELDS,
        "guidance": _TASK_FIELDS["guidance"],
    },
    "required": ["status"],
}

VAPP_CREATE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to make an empty vApp: a container that groups VMs and starts and stops them "
        "in a set order. Move or create VMs into it afterwards."
    ),
    "preconditions": (
        "Give a cluster id (vmware.cluster.list) or a resource pool id "
        "(vmware.resource_pool.list), and a VM folder id (vmware.folder.list). The vApp "
        "gets the default resources: no reservation, no limit, normal shares. The change "
        "waits for approval."
    ),
    "output_shape": (
        "{status: created|unchanged|not_found|invalid_request|precondition_failed|"
        "not_confirmed, vapp, name, state, vms, resource_pool, folder, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a vApp with the same name in the pool is unchanged. Put VMs in "
        "it, then set their order with vmware.composite.vapp.start_order.set and start it "
        "with vmware.composite.vapp.power."
    ),
}

# ---------------------------------------------------------------------------
# vapp.power
# ---------------------------------------------------------------------------

VAPP_POWER_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vapp": _VAPP_PROPERTY,
        "action": {
            "type": "string",
            "enum": ["power_on", "power_off"],
            "description": (
                "power_on starts the VMs in their start order; power_off stops them in the "
                "reverse order."
            ),
        },
        "force": {
            "type": "boolean",
            "default": False,
            "description": (
                "Only for power_off. true powers off every VM at once and skips the stop "
                "order and the guest shutdown. Default false."
            ),
        },
    },
    "required": ["vapp", "action"],
    "additionalProperties": False,
    "examples": [
        {"vapp": "resgroup-v42", "action": "power_on"},
        {"vapp": "resgroup-v42", "action": "power_off", "force": False},
    ],
}

VAPP_POWER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "powered_on",
                "powered_off",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "timeout",
                "not_confirmed",
            ],
            "description": (
                "powered_on / powered_off: the vApp reads back in that state; unchanged: it "
                "was in that state already, nothing sent; not_found / invalid_request: no "
                "such vApp or a bad id; precondition_failed: the vApp is starting or "
                "stopping, nothing sent; timeout: the task did not finish in time; "
                "not_confirmed: the task ended but the state does not match."
            ),
        },
        "action": {"type": "string", "description": "The action asked for."},
        "force": {"type": "boolean", "description": "The force flag MEHO sent."},
        "state_before": {**_NULLABLE_STR, "description": "vApp state before the change."},
        **_VAPP_VIEW_FIELDS,
        **_TASK_FIELDS,
    },
    "required": ["status", "vapp", "action"],
}

VAPP_POWER_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to start or stop all VMs of a vApp in their set order, for example an "
        "application with a database VM that must run before its web VMs. For one VM use "
        "vmware.composite.vm.power instead."
    ),
    "preconditions": (
        "Get the vApp id from vapp.create, or from a VM's 'parentVApp' property with "
        "vmware.object.collect. A vApp that is starting or "
        "stopping is refused; wait and call again. A VM that fails to start stops the "
        "sequence; the VMs that started keep running. The change waits for approval; an "
        "agent needs a grant for it."
    ),
    "output_shape": (
        "{status: powered_on|powered_off|unchanged|not_found|invalid_request|"
        "precondition_failed|timeout|not_confirmed, vapp, name, action, force, "
        "state_before, state, vms: [{vm, name, power_state, start_order, ...}], task, "
        "task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a vApp in the asked state is unchanged. On not_confirmed or "
        "timeout call again later; it answers unchanged once the vApp is there. Check "
        "vms[].power_state for single VMs that did not follow."
    ),
}

# ---------------------------------------------------------------------------
# vapp.start_order.set
# ---------------------------------------------------------------------------

_ENTITY_SETTING_FIELDS: Final = (
    "start_order",
    "start_delay",
    "wait_for_guest",
    "start_action",
    "stop_action",
    "stop_delay",
)

VAPP_START_ORDER_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vapp": _VAPP_PROPERTY,
        "vms": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "description": (
                "One entry per VM to change. Fields left out keep their value. VMs not "
                "listed keep all their settings."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "vm": {
                        "type": "string",
                        "pattern": VM_MOID_PATTERN,
                        "description": "Id of a VM in this vApp, such as 'vm-42'.",
                    },
                    "start_order": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Start group. Lower groups start first and stop last; VMs in "
                            "one group start together."
                        ),
                    },
                    "start_delay": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Seconds to wait after this VM starts.",
                    },
                    "wait_for_guest": {
                        "type": "boolean",
                        "description": (
                            "true: start the next VM once VMware Tools in this guest answers."
                        ),
                    },
                    "start_action": {
                        "type": "string",
                        "enum": list(START_ACTIONS),
                        "description": "powerOn, or none to leave the VM out of the start.",
                    },
                    "stop_action": {
                        "type": "string",
                        "enum": list(STOP_ACTIONS),
                        "description": (
                            "How the vApp stops this VM: powerOff, guestShutdown, suspend, "
                            "or none to leave it out of the stop."
                        ),
                    },
                    "stop_delay": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Seconds to wait after this VM stops.",
                    },
                },
                "required": ["vm"],
                "anyOf": [{"required": [name]} for name in _ENTITY_SETTING_FIELDS],
                "additionalProperties": False,
            },
        },
    },
    "required": ["vapp", "vms"],
    "additionalProperties": False,
    "examples": [
        {
            "vapp": "resgroup-v42",
            "vms": [
                {"vm": "vm-41", "start_order": 1, "start_delay": 60},
                {"vm": "vm-42", "start_order": 2, "stop_action": "guestShutdown"},
            ],
        }
    ],
}

VAPP_START_ORDER_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "updated",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "not_confirmed",
            ],
            "description": (
                "updated: every asked setting reads back; unchanged: they were so already, "
                "nothing sent; not_found / invalid_request: no such vApp, a bad id or a VM "
                "listed twice; precondition_failed: a VM is not in this vApp, nothing sent; "
                "not_confirmed: vCenter answered but a setting does not read back."
            ),
        },
        **_VAPP_VIEW_FIELDS,
        "guidance": _TASK_FIELDS["guidance"],
    },
    "required": ["status", "vapp"],
}

VAPP_START_ORDER_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to change in which order and how a vApp starts and stops its VMs: the start "
        "group, the delays, whether to wait for the guest, and the start and stop actions."
    ),
    "preconditions": (
        "Each VM must be in the vApp; MEHO checks this and changes nothing otherwise. The "
        "answer lists every VM of the vApp with its settings. The change waits for "
        "approval."
    ),
    "output_shape": (
        "{status: updated|unchanged|not_found|invalid_request|precondition_failed|"
        "not_confirmed, vapp, name, state, vms: [{vm, name, power_state, start_order, "
        "start_delay, wait_for_guest, start_action, stop_action, stop_delay}], guidance}."
    ),
    "next_step": (
        "Safe to run twice: settings that are so already are unchanged. Then start the vApp "
        "with vmware.composite.vapp.power."
    ),
}

# ---------------------------------------------------------------------------
# vapp.delete
# ---------------------------------------------------------------------------

VAPP_DELETE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"vapp": _VAPP_PROPERTY},
    "required": ["vapp"],
    "additionalProperties": False,
    "examples": [{"vapp": "resgroup-v42"}],
}

VAPP_DELETE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "deleted",
                "unchanged",
                "invalid_request",
                "precondition_failed",
                "timeout",
                "still_present",
            ],
            "description": (
                "deleted: the vApp no longer reads back; unchanged: there was no such vApp, "
                "nothing sent; invalid_request: a bad id; precondition_failed: the vApp or a "
                "VM in it is not powered off, or it holds other vApps or pools, nothing "
                "sent; timeout: the task did not finish in time; still_present: the task "
                "ended but the vApp still reads back."
            ),
        },
        "object": {
            "type": "object",
            "description": "The vApp the call is about, as read before the change.",
            "properties": {
                "kind": {"type": "string", "description": "Always 'vapp'."},
                "vapp": {"type": "string", "description": "vApp id."},
                "name": {**_NULLABLE_STR, "description": "vApp name."},
                "state": {**_NULLABLE_STR, "description": "vApp state."},
                "vm_count": {"type": "integer", "description": "Number of VMs in the vApp."},
                "lost_with_it": {**_NULLABLE_STR, "description": "What the delete removes."},
            },
        },
        "children": {
            "type": "array",
            "description": "The VMs deleted with the vApp (first 50).",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Always 'vm'."},
                    "moid": {"type": "string", "description": "VM id."},
                    "name": {**_NULLABLE_STR, "description": "VM name."},
                    "power_state": {**_NULLABLE_STR, "description": "VM power state."},
                },
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "object"},
            "description": "What stops the delete: running VMs, or vApps and pools inside.",
        },
        **_TASK_FIELDS,
    },
    "required": ["status", "object"],
}

VAPP_DELETE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call only when a person wants a vApp gone for good, with every VM in it and their "
        "disks. To keep the VMs, move them out of the vApp first."
    ),
    "preconditions": (
        "The vApp and every VM in it must be powered off (vmware.composite.vapp.power with "
        "action power_off), and it must hold no other vApps or resource pools. This is "
        "destructive: an agent cannot run it. A person calls preview_operation with the "
        "same params, then call_operation with the preview_hash; a second person approves."
    ),
    "output_shape": (
        "{status: deleted|unchanged|invalid_request|precondition_failed|timeout|"
        "still_present, object: {kind, vapp, name, state, vm_count, lost_with_it}, "
        "children: [{kind, moid, name, power_state}], blockers, task, task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a vApp that is gone is unchanged. On precondition_failed read "
        "blockers, fix them and call again."
    ),
}
