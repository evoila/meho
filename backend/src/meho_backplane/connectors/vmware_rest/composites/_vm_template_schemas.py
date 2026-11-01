# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the three VM-template library changes.

The handlers are in :mod:`._vm_template`. Kept apart so each file stays small.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_ID_PATTERN,
    VM_MOID_PATTERN,
)

__all__ = [
    "CHECK_IN_LLM_INSTRUCTIONS",
    "CHECK_IN_PARAMETER_SCHEMA",
    "CHECK_IN_RESPONSE_SCHEMA",
    "CHECK_OUT_LLM_INSTRUCTIONS",
    "CHECK_OUT_PARAMETER_SCHEMA",
    "CHECK_OUT_RESPONSE_SCHEMA",
    "DEPLOY_LLM_INSTRUCTIONS",
    "DEPLOY_PARAMETER_SCHEMA",
    "DEPLOY_RESPONSE_SCHEMA",
]

_EXAMPLE_ITEM_ID: Final = "00000000-0000-4000-8000-000000000001"

_MOID: Final[dict[str, Any]] = {"type": "string", "minLength": 1, "maxLength": 128}
_NULLABLE_TEXT: Final[dict[str, Any]] = {"type": ["string", "null"]}
_NULLABLE_INT: Final[dict[str, Any]] = {"type": ["integer", "null"]}

_TEMPLATE_ITEM: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": CONTENT_LIBRARY_ID_PATTERN,
    "description": (
        "Id (a UUID) of the content-library item that holds the VM template, for example "
        f"{_EXAMPLE_ITEM_ID}. Find it with vmware.content_library.item.find."
    ),
}

_VM_NAME: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 80,
    "description": "Name of the new VM. It must not be in use in the target folder.",
}

#: Where the new VM goes. vCenter needs a resource pool, a host or a cluster.
_PLACEMENT: Final[dict[str, Any]] = {
    "folder": {
        **_MOID,
        "description": "Optional. VM folder moid, such as group-v4, for the new VM.",
    },
    "resource_pool": {
        **_MOID,
        "description": "Resource pool moid, such as resgroup-8, for the new VM.",
    },
    "host": {**_MOID, "description": "ESXi host moid, such as host-21, for the new VM."},
    "cluster": {**_MOID, "description": "Cluster moid, such as domain-c7, for the new VM."},
}

_POWERED_ON: Final[dict[str, Any]] = {
    "type": "boolean",
    "default": False,
    "description": "Power the new VM on at the end. Default false.",
}

DEPLOY_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "template_library_item": _TEMPLATE_ITEM,
        "name": _VM_NAME,
        "description": {
            "type": "string",
            "maxLength": 1024,
            "description": "Optional. Description (notes) of the new VM.",
        },
        **_PLACEMENT,
        "datastore": {
            **_MOID,
            "description": (
                "Optional. Datastore moid, such as datastore-17, for the VM files and disks. "
                "Omit it to use the storage of the template."
            ),
        },
        "powered_on": _POWERED_ON,
        "guest_customization_spec": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Optional. Name of a saved guest customization spec "
                "(vmware.guest.customization_spec.list) to apply to the new VM."
            ),
        },
    },
    "required": ["template_library_item", "name"],
    "anyOf": [
        {"required": ["resource_pool"]},
        {"required": ["host"]},
        {"required": ["cluster"]},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "template_library_item": _EXAMPLE_ITEM_ID,
            "name": "<vm-name>",
            "cluster": "<cluster-moid>",
            "folder": "<folder-moid>",
            "powered_on": True,
            "guest_customization_spec": "<spec-name>",
        }
    ],
}

CHECK_OUT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "template_library_item": _TEMPLATE_ITEM,
        "name": {
            **_VM_NAME,
            "description": "Name of the VM that vCenter makes from the template to edit.",
        },
        **_PLACEMENT,
        "powered_on": _POWERED_ON,
    },
    "required": ["template_library_item", "name"],
    "additionalProperties": False,
    "examples": [
        {
            "template_library_item": _EXAMPLE_ITEM_ID,
            "name": "<vm-name>",
            "resource_pool": "<resource-pool-moid>",
        }
    ],
}

CHECK_IN_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "template_library_item": _TEMPLATE_ITEM,
        "vm": {
            "type": "string",
            "pattern": VM_MOID_PATTERN,
            "description": (
                "Id of the checked-out VM, such as vm-42, as the check-out returned it (vm)."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2048,
            "description": "What changed in this template version. vCenter keeps it.",
        },
    },
    "required": ["template_library_item", "vm", "message"],
    "additionalProperties": False,
    "examples": [
        {
            "template_library_item": _EXAMPLE_ITEM_ID,
            "vm": "vm-42",
            "message": "<what changed>",
        }
    ],
}

_STATUS_TEXT: Final = (
    "refused: vCenter said no (for example the name is in use or the VM is still on), "
    "nothing changed; not_found: vCenter did not find the item, VM or a placement "
    "object, nothing changed."
)

_VM_INFO: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The VM as read back after the change, or null when the read failed.",
    "properties": {
        "name": {**_NULLABLE_TEXT, "description": "VM name."},
        "power_state": {
            **_NULLABLE_TEXT,
            "description": "POWERED_ON, POWERED_OFF or SUSPENDED.",
        },
        "guest_os": {**_NULLABLE_TEXT, "description": "Guest OS type vCenter has set."},
        "cpu_count": {**_NULLABLE_INT, "description": "Number of virtual CPUs."},
        "memory_mib": {**_NULLABLE_INT, "description": "Memory in MiB."},
    },
}

_COMMON_ANSWER: Final[dict[str, Any]] = {
    "template_library_item": {"type": "string", "description": "Id of the template item."},
    "read_note": {
        **_NULLABLE_TEXT,
        "description": "Why the read back after the change failed. The change itself is done.",
    },
    "error_type": {**_NULLABLE_TEXT, "description": "vCenter error type on refused/not_found."},
    "guidance": {**_NULLABLE_TEXT, "description": "What happened and what to do next."},
}

DEPLOY_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["deployed", "refused", "not_found"],
            "description": f"deployed: the new VM exists; {_STATUS_TEXT}",
        },
        "vm": {**_NULLABLE_TEXT, "description": "Id of the new VM, such as vm-42."},
        "vm_info": _VM_INFO,
        **_COMMON_ANSWER,
    },
    "required": ["status", "template_library_item", "vm"],
}

CHECK_OUT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["checked_out", "refused", "not_found"],
            "description": f"checked_out: the VM to edit exists; {_STATUS_TEXT}",
        },
        "vm": {
            **_NULLABLE_TEXT,
            "description": "Id of the checked-out VM. Keep it for the check-in.",
        },
        "check_out": {
            "type": ["object", "null"],
            "description": "The check-out as vCenter records it, or null when the read failed.",
            "properties": {
                "time": {**_NULLABLE_TEXT, "description": "When the VM was checked out."},
                "user": {**_NULLABLE_TEXT, "description": "Who checked it out."},
            },
        },
        "vm_info": _VM_INFO,
        **_COMMON_ANSWER,
    },
    "required": ["status", "template_library_item", "vm"],
}

CHECK_IN_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["checked_in", "refused", "not_found"],
            "description": f"checked_in: the item holds a new template version; {_STATUS_TEXT}",
        },
        "vm": {"type": "string", "description": "Id of the VM that was checked in."},
        "version": {**_NULLABLE_TEXT, "description": "The new version of the library item."},
        "template": {
            "type": ["object", "null"],
            "description": "The template as read back after the check-in, or null.",
            "properties": {
                "vm_template": {
                    **_NULLABLE_TEXT,
                    "description": "Moid of the template VM of the new version.",
                },
                "guest_os": {**_NULLABLE_TEXT, "description": "Guest OS type."},
                "cpu_count": {**_NULLABLE_INT, "description": "Number of virtual CPUs."},
                "cores_per_socket": {**_NULLABLE_INT, "description": "CPU cores per socket."},
                "memory_mib": {**_NULLABLE_INT, "description": "Memory in MiB."},
                "disk_count": {**_NULLABLE_INT, "description": "Number of virtual disks."},
                "nic_count": {**_NULLABLE_INT, "description": "Number of virtual NICs."},
            },
        },
        **_COMMON_ANSWER,
    },
    "required": ["status", "template_library_item", "vm"],
}

_REFUSED_HINT: Final = "On refused or not_found, read guidance: nothing changed."

DEPLOY_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to make a new VM from a VM template that is stored in a content library, with "
        "placement, power-on and an optional guest customization spec. For an OVF/OVA item "
        "use vmware.composite.vm.deploy_from_library; for a template in a VM folder use "
        "vmware.composite.vm.clone_from_template."
    ),
    "preconditions": (
        "Get the item id with vmware.content_library.item.find (type vm-template), and a "
        "cluster, host or resource pool moid. The name must be free. A dangerous change: it "
        "waits for approval, and an agent needs a grant."
    ),
    "parameter_hints": {
        "template_library_item": "The item id (UUID), not its name.",
        "cluster": "Give a cluster, a host or a resource pool: one is enough.",
        "guest_customization_spec": "A name from vmware.guest.customization_spec.list.",
    },
    "output_shape": (
        "{status: deployed|refused|not_found, template_library_item, vm, vm_info: {name, "
        "power_state, guest_os, cpu_count, memory_mib}, read_note, error_type, guidance}."
    ),
    "next_step": (
        "Use vm with the other vm operations. The call waits until vCenter has copied the "
        f"template, so a big one takes minutes. Not safe to run twice. {_REFUSED_HINT}"
    ),
}

CHECK_OUT_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to change a VM template in a content library: vCenter makes a VM from it that "
        "you can edit (for example to patch it). The template stays as it is until the "
        "check-in."
    ),
    "preconditions": (
        "Get the item id with vmware.content_library.item.find. The item must be in a local "
        "library, and only one VM can be checked out of it at a time. A dangerous change: it "
        "waits for approval, and an agent needs a grant."
    ),
    "parameter_hints": {
        "template_library_item": "The item id (UUID), not its name.",
        "folder": "Omit it to use the folder of the template.",
    },
    "output_shape": (
        "{status: checked_out|refused|not_found, template_library_item, vm, check_out: "
        "{time, user}, vm_info, read_note, error_type, guidance}."
    ),
    "next_step": (
        "Edit the VM, power it off, then call vmware.composite.library.vm_template.check_in "
        f"with vm and a message. {_REFUSED_HINT}"
    ),
}

CHECK_IN_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to save a checked-out VM back into its content-library item as the new "
        "template version. vCenter keeps the previous version as a backup."
    ),
    "preconditions": (
        "The VM must be the one checked out of this item (vm from check_out) and powered "
        "off. A dangerous change: it waits for approval, and an agent needs a grant."
    ),
    "parameter_hints": {
        "vm": "The vm the check-out returned.",
        "message": "A short note on what changed.",
    },
    "output_shape": (
        "{status: checked_in|refused|not_found, template_library_item, vm, version, template: "
        "{vm_template, guest_os, cpu_count, cores_per_socket, memory_mib, disk_count, "
        "nic_count}, read_note, error_type, guidance}."
    ),
    "next_step": (
        "The checked-out VM is now the template; deploy new VMs with "
        f"vmware.composite.library.vm_template.deploy. {_REFUSED_HINT}"
    ),
}
