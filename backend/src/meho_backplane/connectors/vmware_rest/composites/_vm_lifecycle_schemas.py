# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the VM template, register and instant clone changes.

``vm.template.mark`` and ``vm.template.unmark`` (:mod:`._vm_template`),
``vm.register`` and ``vm.unregister`` (:mod:`._vm_register`) and
``vm.instant_clone`` (:mod:`._vm_instant_clone`). Plain data, no I/O.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)

__all__ = [
    "RESOURCE_POOL_MOID_PATTERN",
    "VMX_PATH_PATTERN",
    "VM_INSTANT_CLONE_LLM_INSTRUCTIONS",
    "VM_INSTANT_CLONE_PARAMETER_SCHEMA",
    "VM_INSTANT_CLONE_RESPONSE_SCHEMA",
    "VM_REGISTER_LLM_INSTRUCTIONS",
    "VM_REGISTER_PARAMETER_SCHEMA",
    "VM_REGISTER_RESPONSE_SCHEMA",
    "VM_TEMPLATE_MARK_LLM_INSTRUCTIONS",
    "VM_TEMPLATE_MARK_PARAMETER_SCHEMA",
    "VM_TEMPLATE_MARK_RESPONSE_SCHEMA",
    "VM_TEMPLATE_UNMARK_LLM_INSTRUCTIONS",
    "VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA",
    "VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA",
    "VM_UNREGISTER_LLM_INSTRUCTIONS",
    "VM_UNREGISTER_PARAMETER_SCHEMA",
    "VM_UNREGISTER_RESPONSE_SCHEMA",
]

#: Resource pool moids; a vApp is a resource pool too (``resgroup-v<n>``).
RESOURCE_POOL_MOID_PATTERN: Final = r"^resgroup-v?[0-9]+$"
#: A datastore path to a ``.vmx`` (VM) or ``.vmtx`` (template) file:
#: ``[<datastore name>] <folder>/<file>.vmx``.
VMX_PATH_PATTERN: Final = r"^\[[^\[\]\r\n]+\] [^\r\n]+\.(vmx|vmtx)$"

_NULLABLE_STR: Final = {"type": ["string", "null"]}

_VM_ID_HELP: Final = (
    "Get it from vmware.vm.info (by VM name). A change takes no VM name, so after "
    "approval it runs on the VM the approver saw."
)

_VM_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": f"VM id, such as 'vm-42'. {_VM_ID_HELP}",
}
_POOL_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": RESOURCE_POOL_MOID_PATTERN,
    "description": (
        "Resource pool id, such as 'resgroup-8' (a cluster's own pool, a child pool or a "
        "vApp). List them with vmware.resource_pool.list."
    ),
}
_HOST_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": HOST_MOID_PATTERN,
    "description": "ESXi host id, such as 'host-12'. List them with GET:/api/vcenter/host.",
}

_VM_VIEW: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The VM as vCenter shows it.",
    "properties": {
        "vm": {"type": "string", "description": "VM id."},
        "name": {**_NULLABLE_STR, "description": "VM name."},
        "power_state": {**_NULLABLE_STR, "description": "poweredOn, poweredOff or suspended."},
        "is_template": {"type": ["boolean", "null"], "description": "The VM is a template."},
        "vmx_path": {
            **_NULLABLE_STR,
            "description": "Datastore path of its .vmx file, such as '[<datastore-name>] a/a.vmx'.",
        },
        "folder": {**_NULLABLE_STR, "description": "Id of the VM folder it is in."},
        "resource_pool": {
            **_NULLABLE_STR,
            "description": "Id of its resource pool (none for a template).",
        },
        "host": {**_NULLABLE_STR, "description": "Id of its ESXi host."},
    },
}

_COMMON_ANSWER: Final[dict[str, Any]] = {
    "task": {**_NULLABLE_STR, "description": "Id of the vCenter task, when one ran."},
    "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    "issues": {
        "type": "array",
        "items": {"type": "object"},
        "description": "Problems, such as a task that did not finish in time.",
    },
}


def _status(values: list[str], text: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": text}


# ---------------------------------------------------------------------------
# vm.template.mark / vm.template.unmark
# ---------------------------------------------------------------------------

VM_TEMPLATE_MARK_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"vm": _VM_PROPERTY},
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

VM_TEMPLATE_MARK_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": _status(
            [
                "marked",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "not_confirmed",
            ],
            "marked: the VM reads back as a template; unchanged: it was one already, nothing "
            "sent; not_found / invalid_request: no such VM or a bad id; precondition_failed: "
            "the VM is not powered off, nothing sent; not_confirmed: the call ended but the VM "
            "does not read back as a template.",
        ),
        "vm": {"type": "string", "description": "VM id."},
        "before": {**_VM_VIEW, "description": "The VM before the change."},
        "after": {**_VM_VIEW, "description": "The VM read after the change."},
        **_COMMON_ANSWER,
    },
    "required": ["status", "vm"],
}

VM_TEMPLATE_MARK_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to turn a VM into a template, so it can be cloned (with "
        "vmware.composite.vm.clone_from_template) but no longer powered on."
    ),
    "preconditions": (
        "The VM must be powered off; MEHO checks this and changes nothing otherwise. Get the "
        "VM id from vmware.vm.info. The change waits for approval."
    ),
    "output_shape": (
        "{status: marked|unchanged|not_found|invalid_request|precondition_failed|"
        "not_confirmed, vm, before, after, task, guidance, issues}; before and after are "
        "{vm, name, power_state, is_template, vmx_path, folder, resource_pool, host}."
    ),
    "next_step": (
        "Safe to run twice: a template is unchanged. To turn it back into a VM use "
        "vmware.composite.vm.template.unmark."
    ),
}

VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {
            **_VM_PROPERTY,
            "description": f"Id of the template, such as 'vm-42'. {_VM_ID_HELP}",
        },
        "resource_pool": {
            **_POOL_PROPERTY,
            "description": f"{_POOL_PROPERTY['description']} The VM is placed in this pool.",
        },
        "host": {
            **_HOST_PROPERTY,
            "description": (
                f"{_HOST_PROPERTY['description']} Optional: the host the VM runs on. It must "
                "be in the cluster of the resource pool. Leave it out for a DRS cluster or a "
                "single host; vCenter then picks one."
            ),
        },
    },
    "required": ["vm", "resource_pool"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "resource_pool": "resgroup-8"}],
}

VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": _status(
            ["unmarked", "unchanged", "not_found", "invalid_request", "not_confirmed"],
            "unmarked: the template reads back as a VM; unchanged: it was not a template, "
            "nothing sent; not_found / invalid_request: no such VM or a bad id; "
            "not_confirmed: the call ended but it still reads back as a template.",
        ),
        "vm": {"type": "string", "description": "VM id."},
        "before": {**_VM_VIEW, "description": "The template before the change."},
        "after": {**_VM_VIEW, "description": "The VM read after the change."},
        **_COMMON_ANSWER,
    },
    "required": ["status", "vm"],
}

VM_TEMPLATE_UNMARK_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to turn a template back into a normal VM, for example to update its guest OS "
        "before making it a template again."
    ),
    "preconditions": (
        "The VM must be a template. Pick a resource pool (and optionally a host of the same "
        "cluster) for it. The change waits for approval."
    ),
    "parameter_hints": {
        "resource_pool": "The 'id' of a vmware.resource_pool.list row.",
    },
    "output_shape": (
        "{status: unmarked|unchanged|not_found|invalid_request|not_confirmed, vm, before, "
        "after, task, guidance, issues}; after is {vm, name, power_state, is_template, "
        "vmx_path, folder, resource_pool, host}."
    ),
    "next_step": (
        "Safe to run twice: a VM that is no template is unchanged. The VM stays powered off; "
        "power it on with vmware.composite.vm.power."
    ),
}

# ---------------------------------------------------------------------------
# vm.register / vm.unregister
# ---------------------------------------------------------------------------

VM_REGISTER_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vmx_path": {
            "type": "string",
            "pattern": VMX_PATH_PATTERN,
            "maxLength": 1024,
            "description": (
                "Datastore path of the VM's .vmx file (or a template's .vmtx file): "
                "'[<datastore-name>] <folder>/<file>.vmx'. Find it with "
                "vmware.composite.datastore.file.list or vmware.composite.datastore.orphan_dirs."
            ),
        },
        "folder": {
            "type": "string",
            "pattern": VM_FOLDER_MOID_PATTERN,
            "description": "Id of the VM folder the VM goes into, such as 'group-v3'.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 79,
            "description": "Optional VM name. Leave it out to use the name in the .vmx file.",
        },
        "as_template": {
            "type": "boolean",
            "default": False,
            "description": "true registers the files as a template (needs host, no resource_pool).",
        },
        "resource_pool": {
            **_POOL_PROPERTY,
            "description": f"{_POOL_PROPERTY['description']} Needed for a VM, not for a template.",
        },
        "host": {
            **_HOST_PROPERTY,
            "description": (
                f"{_HOST_PROPERTY['description']} Needed for a template. For a VM optional: it "
                "must be in the cluster of the resource pool."
            ),
        },
    },
    "required": ["vmx_path", "folder"],
    "oneOf": [
        {"required": ["resource_pool"]},
        {
            "required": ["as_template", "host"],
            "properties": {
                "as_template": {"const": True, "description": "A template: send host, no pool."}
            },
        },
    ],
    "additionalProperties": False,
    "examples": [
        {
            "vmx_path": "[<datastore-name>] <vm-name>/<vm-name>.vmx",
            "folder": "group-v3",
            "resource_pool": "resgroup-8",
        },
        {
            "vmx_path": "[<datastore-name>] <template-name>/<template-name>.vmtx",
            "folder": "group-v3",
            "as_template": True,
            "host": "host-12",
        },
    ],
}

VM_REGISTER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": _status(
            ["registered", "invalid_request", "timeout", "not_confirmed"],
            "registered: the new VM reads back; invalid_request: a bad input, nothing sent; "
            "timeout: the task did not finish in time; not_confirmed: the task ended but the "
            "new VM does not read back.",
        ),
        "vmx_path": {"type": "string", "description": "The file that was registered."},
        "vm": {**_NULLABLE_STR, "description": "Id of the new VM, such as 'vm-43'."},
        "after": {**_VM_VIEW, "description": "The new VM as read after the task."},
        **_COMMON_ANSWER,
    },
    "required": ["status", "vmx_path"],
}

VM_REGISTER_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to add a VM (or a template) to the vCenter inventory from its files on a "
        "datastore, for example after vmware.composite.vm.unregister, a datastore move or a "
        "restore of the VM folder."
    ),
    "preconditions": (
        "The .vmx (or .vmtx) file must exist and no VM may use it yet. For a VM give a "
        "resource pool; for a template set as_template and give a host. The change waits for "
        "approval."
    ),
    "parameter_hints": {
        "vmx_path": "The 'vmx_path' of a vmware.composite.vm.unregister answer, or a .vmx file "
        "found by vmware.composite.datastore.file.list.",
        "folder": "The 'folder' of a vmware.composite.vm.unregister answer.",
    },
    "output_shape": (
        "{status: registered|invalid_request|timeout|not_confirmed, vmx_path, vm, after, task, "
        "guidance, issues}; after is {vm, name, power_state, is_template, vmx_path, folder, "
        "resource_pool, host}."
    ),
    "next_step": (
        "Not safe to run twice: a second call fails because the file is in use (AlreadyExists). "
        "The VM stays powered off; power it on with vmware.composite.vm.power. If vCenter asks "
        "whether the VM was moved or copied, answer with vmware.vm.question.answer."
    ),
}

VM_UNREGISTER_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"vm": _VM_PROPERTY},
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

VM_UNREGISTER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": _status(
            [
                "unregistered",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "still_present",
            ],
            "unregistered: the VM is gone from the inventory, its files stay; not_found / "
            "invalid_request: no such VM or a bad id; precondition_failed: the VM is powered "
            "on, nothing sent; still_present: the call ended but the VM still reads back.",
        ),
        "vm": {"type": "string", "description": "VM id."},
        "before": {
            **_VM_VIEW,
            "description": "The VM before the change. Its vmx_path and folder register it again.",
        },
        **_COMMON_ANSWER,
    },
    "required": ["status", "vm"],
}

VM_UNREGISTER_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to remove a VM or template from the vCenter inventory but keep its files on the "
        "datastore, for example before moving its folder to another datastore. To delete the "
        "files too, use vmware.composite.vm.destroy instead."
    ),
    "preconditions": (
        "The VM must not be powered on; MEHO checks this and changes nothing otherwise. vCenter "
        "drops the VM's statistics, permissions, alarms and resource pool settings. Get the VM "
        "id from vmware.vm.info. The change waits for approval."
    ),
    "output_shape": (
        "{status: unregistered|not_found|invalid_request|precondition_failed|still_present, "
        "vm, before, task, guidance, issues}; before is {vm, name, power_state, is_template, "
        "vmx_path, folder, resource_pool, host}."
    ),
    "next_step": (
        "Keep before.vmx_path and before.folder: vmware.composite.vm.register with them adds "
        "the VM back. A second call answers not_found."
    ),
}

# ---------------------------------------------------------------------------
# vm.instant_clone
# ---------------------------------------------------------------------------

VM_INSTANT_CLONE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "source_vm": {
            **_VM_PROPERTY,
            "description": f"Id of the running VM to clone, such as 'vm-42'. {_VM_ID_HELP}",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 79,
            "description": "Name of the new VM. It must not exist in the folder yet.",
        },
        "folder": {
            "type": "string",
            "pattern": VM_FOLDER_MOID_PATTERN,
            "description": "Optional VM folder id, such as 'group-v3'. Default: as the source.",
        },
        "resource_pool": {
            **_POOL_PROPERTY,
            "description": (
                f"{_POOL_PROPERTY['description']} Optional; default: the source's resource pool."
            ),
        },
        "datastore": {
            "type": "string",
            "pattern": DATASTORE_MOID_PATTERN,
            "description": (
                "Optional datastore id, such as 'datastore-11', for the new VM's files. "
                "Default: the source's datastore."
            ),
        },
        "disconnect_all_nics": {
            "type": "boolean",
            "description": (
                "true starts the clone with every network adapter disconnected, so it does not "
                "clash with the source's IP address. Default: as on the source."
            ),
        },
    },
    "required": ["source_vm", "name"],
    "additionalProperties": False,
    "examples": [{"source_vm": "vm-42", "name": "<new-vm-name>", "disconnect_all_nics": True}],
}

VM_INSTANT_CLONE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": _status(
            ["cloned", "not_found", "invalid_request", "precondition_failed", "not_confirmed"],
            "cloned: the new VM reads back; not_found / invalid_request: no such source VM or a "
            "bad input, nothing sent; precondition_failed: the source is not powered on, "
            "nothing sent; not_confirmed: vCenter answered but the new VM does not read back.",
        ),
        "source_vm": {"type": "string", "description": "Id of the source VM."},
        "source": {**_VM_VIEW, "description": "The source VM before the clone."},
        "vm": {**_NULLABLE_STR, "description": "Id of the new VM."},
        "after": {**_VM_VIEW, "description": "The new VM as read after the clone."},
        **_COMMON_ANSWER,
    },
    "required": ["status", "source_vm"],
}

VM_INSTANT_CLONE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to make an instant clone: a new, running VM that shares the memory and disk "
        "state of a running source VM. It is much faster than a full clone, for example for "
        "test or desktop VMs."
    ),
    "preconditions": (
        "The source VM must be powered on; MEHO checks this and changes nothing otherwise. "
        "The new name must be free in the folder. The clone has the same guest identity "
        "(host name, IP) as the source, so set disconnect_all_nics or change it in the guest. "
        "The change waits for approval."
    ),
    "output_shape": (
        "{status: cloned|not_found|invalid_request|precondition_failed|not_confirmed, "
        "source_vm, source, vm, after, task, guidance, issues}; source and after are "
        "{vm, name, power_state, is_template, vmx_path, folder, resource_pool, host}."
    ),
    "next_step": (
        "Not safe to run twice: a second call with the same name fails (AlreadyExists). Use "
        "vmware.composite.vm.guest.net.show on the new VM to see its network settings."
    ),
}
