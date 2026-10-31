# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the VM security changes.

``vm.vtpm.add`` and ``vm.vtpm.remove`` (:mod:`._vm_vtpm`) and
``vm.encryption.set`` (:mod:`._vm_encryption`). Plain data, no I/O.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN

__all__ = [
    "VM_ENCRYPTION_SET_LLM_INSTRUCTIONS",
    "VM_ENCRYPTION_SET_PARAMETER_SCHEMA",
    "VM_ENCRYPTION_SET_RESPONSE_SCHEMA",
    "VM_VTPM_ADD_LLM_INSTRUCTIONS",
    "VM_VTPM_ADD_PARAMETER_SCHEMA",
    "VM_VTPM_ADD_RESPONSE_SCHEMA",
    "VM_VTPM_REMOVE_LLM_INSTRUCTIONS",
    "VM_VTPM_REMOVE_PARAMETER_SCHEMA",
    "VM_VTPM_REMOVE_RESPONSE_SCHEMA",
]

_NULLABLE_STR: Final = {"type": ["string", "null"]}

_VM_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": (
        "VM id, such as 'vm-42'. Get it from vmware.vm.info (by VM name). A change "
        "takes no VM name, so after approval it runs on the VM the approver saw."
    ),
}

_VM_ONLY_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"vm": _VM_PROPERTY},
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

_VTPM_VIEW: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The VM's vTPM device, or null when it has none.",
    "properties": {
        "key": {"type": ["integer", "null"], "description": "vCenter device key."},
        "label": {**_NULLABLE_STR, "description": "Device label, such as 'Virtual TPM'."},
    },
}

_TASK_FIELDS: Final[dict[str, Any]] = {
    "task": {**_NULLABLE_STR, "description": "Id of the vCenter task, when one ran."},
    "task_state": {**_NULLABLE_STR, "description": "Last state of the vCenter task."},
    "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
}

# ---------------------------------------------------------------------------
# vm.vtpm.add
# ---------------------------------------------------------------------------

VM_VTPM_ADD_PARAMETER_SCHEMA: Final[dict[str, Any]] = _VM_ONLY_PARAMETERS

VM_VTPM_ADD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "added",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "timeout",
                "not_confirmed",
            ],
            "description": (
                "added: the vTPM reads back; unchanged: the VM had one, nothing sent; "
                "not_found / invalid_request: no such VM or a bad id; precondition_failed: "
                "the VM is on, not EFI, or too old, nothing sent; timeout: the task did not "
                "finish in time; not_confirmed: the task ended but no vTPM reads back."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "vm_name": {**_NULLABLE_STR, "description": "VM name as vCenter shows it."},
        "power_state": {**_NULLABLE_STR, "description": "VM power state before the change."},
        "vtpm": _VTPM_VIEW,
        **_TASK_FIELDS,
    },
    "required": ["status", "vm"],
}

VM_VTPM_ADD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to give a VM a virtual TPM 2.0, for example before installing Windows 11 or "
        "Windows Server 2025, or to use BitLocker or Secure Boot measurements in the guest."
    ),
    "preconditions": (
        "The VM must be powered off, use EFI firmware and virtual hardware version 14 or "
        "newer; MEHO checks this and changes nothing otherwise. vCenter needs a default key "
        "provider (a standard, trusted or native key provider); without one the task fails. "
        "Get the VM id from vmware.vm.info (by VM name). The change waits for approval."
    ),
    "output_shape": (
        "{status: added|unchanged|not_found|invalid_request|precondition_failed|timeout|"
        "not_confirmed, vm, vm_name, power_state, vtpm: {key, label}, task, task_state, "
        "guidance}."
    ),
    "next_step": (
        "Safe to run twice: a VM with a vTPM is unchanged. On precondition_failed read "
        "guidance, fix the cause (power the VM off, upgrade its hardware version) and call "
        "again. Then power the VM on with vmware.composite.vm.power."
    ),
}

# ---------------------------------------------------------------------------
# vm.vtpm.remove
# ---------------------------------------------------------------------------

VM_VTPM_REMOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = _VM_ONLY_PARAMETERS

VM_VTPM_REMOVE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "removed",
                "unchanged",
                "invalid_request",
                "precondition_failed",
                "timeout",
                "still_present",
            ],
            "description": (
                "removed: no vTPM reads back; unchanged: the VM had none, nothing sent; "
                "invalid_request: no such VM or a bad id; precondition_failed: the VM is on, "
                "nothing sent; timeout: the task did not finish in time; still_present: the "
                "task ended but the vTPM still reads back."
            ),
        },
        "object": {
            "type": "object",
            "description": "The vTPM the call is about, as read before the change.",
            "properties": {
                "kind": {"type": "string", "description": "Always 'vm_vtpm'."},
                "vm": {"type": "string", "description": "VM id."},
                "vm_name": {**_NULLABLE_STR, "description": "VM name."},
                "power_state": {**_NULLABLE_STR, "description": "VM power state."},
                "vtpm": _VTPM_VIEW,
                "lost_with_it": {**_NULLABLE_STR, "description": "What the removal loses."},
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Always empty for this change.",
        },
        **_TASK_FIELDS,
    },
    "required": ["status", "object"],
}

VM_VTPM_REMOVE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call only when a person wants the virtual TPM taken out of a VM, for example before "
        "moving the VM to a place without a key provider. The guest loses every key it kept "
        "in the vTPM, such as BitLocker keys."
    ),
    "preconditions": (
        "The VM must be powered off. In the guest, suspend BitLocker or save the recovery keys "
        "first. This is destructive: an agent cannot run it. A person calls preview_operation "
        "with the same params, then call_operation with the preview_hash; a second person "
        "approves."
    ),
    "output_shape": (
        "{status: removed|unchanged|invalid_request|precondition_failed|timeout|still_present, "
        "object: {kind, vm, vm_name, power_state, vtpm, lost_with_it}, blockers, task, "
        "task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a VM without a vTPM is unchanged. After removed, the VM files can "
        "stay encrypted; use vmware.composite.vm.encryption.set with action decrypt when that "
        "is wanted too."
    ),
}

# ---------------------------------------------------------------------------
# vm.encryption.set
# ---------------------------------------------------------------------------

VM_ENCRYPTION_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_PROPERTY,
        "action": {
            "type": "string",
            "enum": ["encrypt", "decrypt"],
            "description": "encrypt or decrypt the VM files and all its disks.",
        },
        "storage_policy_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Id of the VM storage policy the VM home and every disk get. To encrypt: a "
                "policy with encryption (such as the built-in VM Encryption Policy). To "
                "decrypt: a policy without encryption. List them with "
                "vmware.composite.storage_policy.list (field 'policy')."
            ),
        },
    },
    "required": ["vm", "action", "storage_policy_id"],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "action": "encrypt", "storage_policy_id": "<encryption-policy-id>"},
        {"vm": "vm-42", "action": "decrypt", "storage_policy_id": "<plain-policy-id>"},
    ],
}

_ENCRYPTION_STATE: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "Encryption state of the VM. Holds no key material.",
    "properties": {
        "vm_home_encrypted": {
            "type": "boolean",
            "description": "The VM files (configuration, NVRAM, swap) are encrypted.",
        },
        "key_provider": {
            **_NULLABLE_STR,
            "description": "Id of the key provider of the VM files' key, or null.",
        },
        "has_vtpm": {"type": "boolean", "description": "The VM has a virtual TPM."},
        "disks": {
            "type": "array",
            "description": "One row per virtual disk.",
            "items": {
                "type": "object",
                "properties": {
                    "disk": {"type": "string", "description": "Disk id (vCenter device key)."},
                    "label": {**_NULLABLE_STR, "description": "Disk label, such as 'Hard disk 1'."},
                    "encrypted": {"type": "boolean", "description": "The disk is encrypted."},
                },
            },
        },
    },
}

VM_ENCRYPTION_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "encrypted",
                "decrypted",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "timeout",
                "not_confirmed",
            ],
            "description": (
                "encrypted / decrypted: the VM and all disks read back so; unchanged: they "
                "were so already, nothing sent; not_found / invalid_request: no such VM or a "
                "bad id; precondition_failed: the VM is on, or has snapshots for encrypt, "
                "nothing sent; timeout: the task did not finish in time; not_confirmed: the "
                "task ended but the state does not match (often a policy that does not fit)."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "vm_name": {**_NULLABLE_STR, "description": "VM name as vCenter shows it."},
        "action": {"type": "string", "description": "The action asked for."},
        "storage_policy_id": {"type": "string", "description": "The policy MEHO applied."},
        "power_state": {**_NULLABLE_STR, "description": "VM power state before the change."},
        "before": {**_ENCRYPTION_STATE, "description": "State before the change."},
        "after": {**_ENCRYPTION_STATE, "description": "State read after the change."},
        **_TASK_FIELDS,
    },
    "required": ["status", "vm", "action"],
}

VM_ENCRYPTION_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to encrypt a VM and all its disks, or to decrypt them again, by applying a VM "
        "storage policy. For a virtual TPM use vmware.composite.vm.vtpm.add instead; it "
        "encrypts only the VM files."
    ),
    "preconditions": (
        "The VM must be powered off; to encrypt it must have no snapshots. MEHO checks both "
        "and changes nothing otherwise. Encrypt needs a default key provider in vCenter and "
        "an encryption storage policy; decrypt needs a policy without encryption. Find the "
        "policy id with vmware.composite.storage_policy.list. vCenter may refuse to decrypt "
        "the VM files of a VM with a vTPM. The change waits for approval."
    ),
    "parameter_hints": {
        "storage_policy_id": "The 'policy' field of a vmware.composite.storage_policy.list row.",
    },
    "output_shape": (
        "{status: encrypted|decrypted|unchanged|not_found|invalid_request|"
        "precondition_failed|timeout|not_confirmed, vm, vm_name, action, storage_policy_id, "
        "power_state, before, after, task, task_state, guidance}; before and after are "
        "{vm_home_encrypted, key_provider, has_vtpm, disks: [{disk, label, encrypted}]}."
    ),
    "next_step": (
        "Safe to run twice: a VM in the asked state is unchanged. On not_confirmed compare "
        "before and after and check the policy fits the action. Then power the VM on with "
        "vmware.composite.vm.power."
    ),
}
