# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Encrypt or decrypt a VM -- ``vmware.composite.vm.encryption.set`` (``dangerous``).

One ``VirtualMachine.ReconfigVM_Task`` (see :mod:`._vm_security`). The VM home
and every disk get the given storage policy (``vmProfile`` and each disk's
``profile``):

* **encrypt** -- the policy must be an encryption policy. MEHO sends no
  ``crypto`` spec: the vendor documents that vCenter then makes new keys from
  the default key provider and sets the crypto spec itself. A VM with
  snapshots is refused before any write (vCenter does not encrypt it).
* **decrypt** -- the policy must have no encryption. MEHO sends
  ``CryptoSpecDecrypt`` for the VM and for each encrypted disk.

A VM that is in the asked state already is ``unchanged`` (nothing sent). After
the task MEHO reads the VM again; ``encrypted`` / ``decrypted`` only when the
VM home and every disk read back in the asked state, else ``not_confirmed``.

The schemas and AI guidance live in :mod:`._vm_security_schemas`; the shared
registry in ``_register.py`` only splats :data:`VM_ENCRYPTION_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_UNCHANGED,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vm_security import (
    BACKING_SPEC_TYPE,
    CRYPTO_SPEC_DECRYPT_TYPE,
    DEFINED_PROFILE_SPEC_TYPE,
    PROP_SNAPSHOT,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    disk_encrypted,
    disks,
    encryption_state,
    power_state,
    powered_on_problem,
    read_vm,
    reconfigure,
    timeout_fields,
    vm_name,
)
from meho_backplane.connectors.vmware_rest.composites._vm_security_schemas import (
    VM_ENCRYPTION_SET_LLM_INSTRUCTIONS,
    VM_ENCRYPTION_SET_PARAMETER_SCHEMA,
    VM_ENCRYPTION_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
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
    "VM_ENCRYPTION_COMPOSITES",
    "encryption_spec",
    "vm_encryption_set_composite",
]

OP_ID_VM_ENCRYPTION_SET: Final = "vmware.composite.vm.encryption.set"

ACTION_ENCRYPT: Final = "encrypt"
ACTION_DECRYPT: Final = "decrypt"

STATUS_ENCRYPTED: Final = "encrypted"
STATUS_DECRYPTED: Final = "decrypted"


def _profile(policy_id: str) -> list[dict[str, Any]]:
    return [{VIM_TYPE_NAME_KEY: DEFINED_PROFILE_SPEC_TYPE, "profileId": policy_id}]


def encryption_spec(props: dict[str, Any], *, action: str, policy_id: str) -> dict[str, Any]:
    """The ``VirtualMachineConfigSpec`` fields for encrypt or decrypt.

    The VM home and every disk get the policy. Decrypt adds ``CryptoSpecDecrypt``
    for the VM and for each encrypted disk. Encrypt leaves ``crypto`` out, so
    vCenter makes the keys from the default key provider.
    """
    decrypt = {VIM_TYPE_NAME_KEY: CRYPTO_SPEC_DECRYPT_TYPE}
    changes: list[dict[str, Any]] = []
    for disk in disks(props):
        change: dict[str, Any] = {
            VIM_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
            "operation": "edit",
            "device": disk,
            "profile": _profile(policy_id),
        }
        if action == ACTION_DECRYPT and disk_encrypted(disk):
            change["backing"] = {VIM_TYPE_NAME_KEY: BACKING_SPEC_TYPE, "crypto": decrypt}
        changes.append(change)
    spec: dict[str, Any] = {"vmProfile": _profile(policy_id)}
    if action == ACTION_DECRYPT:
        spec["crypto"] = decrypt
    if changes:
        spec["deviceChange"] = changes
    return spec


def _is_done(state: dict[str, Any], action: str) -> bool:
    flags = [state["vm_home_encrypted"], *(d["encrypted"] for d in state["disks"])]
    return all(flags) if action == ACTION_ENCRYPT else not any(flags)


def _encryption_envelope(
    params: dict[str, Any], status: str | None, **fields: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vm": params["vm"],
        "vm_name": None,
        "action": params["action"],
        "storage_policy_id": params["storage_policy_id"],
        "power_state": None,
        "before": None,
        "after": None,
        "task": None,
        "task_state": None,
        "guidance": None,
    }
    out.update(fields)
    return out


async def _encryption_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``(envelope, VM properties)``; the envelope's ``status`` is ``None`` to go on."""
    vm = params["vm"]
    action = params["action"]
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _encryption_envelope(params, STATUS_INVALID_REQUEST, guidance=problem), None
    props = await read_vm(connector, target, operator, vm)
    if props is None:
        guidance = f"no VM {vm!r}; pass a VM id such as vm-42"
        return _encryption_envelope(params, STATUS_NOT_FOUND, guidance=guidance), None
    state = encryption_state(props)
    base = _encryption_envelope(
        params, None, vm_name=vm_name(props), power_state=power_state(props), before=state
    )
    if _is_done(state, action):
        done = "encrypted" if action == ACTION_ENCRYPT else "not encrypted"
        guidance = f"the VM and all its disks are {done} already; nothing was sent"
        return {**base, "status": STATUS_UNCHANGED, "after": state, "guidance": guidance}, props
    problem = powered_on_problem(props)
    if problem is None and action == ACTION_ENCRYPT and props.get(PROP_SNAPSHOT):
        problem = "the VM has snapshots; vCenter encrypts only a VM without snapshots"
    if problem is not None:
        return {**base, "status": STATUS_PRECONDITION_FAILED, "guidance": problem}, props
    return base, props


async def vm_encryption_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Encrypt or decrypt one powered-off VM. Op-id: ``vm.encryption.set``."""
    checked, props = await _encryption_precheck(connector, target, operator, params)
    if checked["status"] is not None or props is None:
        return checked
    vm = params["vm"]
    action = params["action"]
    policy_id = params["storage_policy_id"]
    gate, outcome = await reconfigure(
        connector,
        target,
        operator,
        spec=encryption_spec(props, action=action, policy_id=policy_id),
        params={
            "op": OP_ID_VM_ENCRYPTION_SET,
            "vm": vm,
            "action": action,
            "storage_policy_id": policy_id,
        },
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.timed_out:
        return {**checked, **timeout_fields(outcome, "Run this call again to read the state.")}
    after_props = await read_vm(connector, target, operator, vm)
    after = encryption_state(after_props) if after_props is not None else None
    done = {**checked, "after": after, "task": outcome.task, "task_state": outcome.state}
    if after is None or not _is_done(after, action):
        return {
            **done,
            "status": STATUS_NOT_CONFIRMED,
            "guidance": (
                "ReconfigVM_Task succeeded, but the VM and its disks do not all read back as "
                f"{'encrypted' if action == ACTION_ENCRYPT else 'decrypted'}. Check that the "
                "storage policy fits the action, then compare 'before' and 'after'"
            ),
        }
    return {**done, "status": STATUS_ENCRYPTED if action == ACTION_ENCRYPT else STATUS_DECRYPTED}


async def _encryption_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, its encryption state now and the policy it gets."""
    params = ctx.params
    if (
        not isinstance(params.get("vm"), str)
        or params.get("action") not in (ACTION_ENCRYPT, ACTION_DECRYPT)
        or not isinstance(params.get("storage_policy_id"), str)
        or ctx.connector_instance is None
    ):
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    vm = params["vm"]
    try:
        checked, _props = await _encryption_precheck(connector, ctx.target, ctx.operator, params)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read VM {vm}, so it cannot show its state.",
            "vm": vm,
            "action": params["action"],
            "storage_policy_id": params["storage_policy_id"],
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": (
            f"{params['action'].capitalize()} {vm} and its disks with storage policy "
            f"{params['storage_policy_id']}."
        ),
        "vm": vm,
        "vm_name": checked["vm_name"],
        "action": params["action"],
        "storage_policy_id": params["storage_policy_id"],
        "power_state": checked["power_state"],
        "current": checked["before"],
    }
    if checked["status"] is not None:
        preview["summary"] = f"The call will change nothing on {vm}: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


register_preview_builder(OP_ID_VM_ENCRYPTION_SET, _encryption_preview)


#: The registration row, as ``_CompositeSpec`` keyword arguments.
VM_ENCRYPTION_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_ENCRYPTION_SET,
        "handler": vm_encryption_set_composite,
        "summary": "Encrypt or decrypt a powered-off VM and its disks with a storage policy.",
        "description": (
            "Encrypts or decrypts a VM, named by its VM id, and all its disks by applying a "
            "storage policy: an encryption policy to encrypt, a policy without encryption to "
            "decrypt. The VM must be off. Encrypt needs a default key provider. Waits for "
            "approval."
        ),
        "parameter_schema": VM_ENCRYPTION_SET_PARAMETER_SCHEMA,
        "response_schema": VM_ENCRYPTION_SET_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "security", "encryption", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_ENCRYPTION_SET_LLM_INSTRUCTIONS,
    },
)
