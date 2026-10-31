# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Register a VM from its files, and unregister it -- ``vm.register`` and ``vm.unregister``.

Both use the VIM API as VI-JSON, like the template changes:

* ``vmware.composite.vm.register`` (``caution``) -- ``Folder.RegisterVM_Task``
  on the given VM folder, with the ``.vmx`` (or ``.vmtx``) datastore path, an
  optional name, and a resource pool (a VM) or a host (a template). The REST
  call ``POST /api/vcenter/vm?action=register`` cannot register a template,
  so MEHO uses the VIM method for both. MEHO polls the task and reads the new
  VM (the task result) back.
* ``vmware.composite.vm.unregister`` (``dangerous``) --
  ``VirtualMachine.UnregisterVM`` (synchronous). The files stay on the
  datastore. A powered-on VM is refused before any write. The answer keeps
  the VM's ``.vmx`` path and folder, so ``vm.register`` can add it back.

Shared parts: :mod:`._vm_lifecycle`; schemas: :mod:`._vm_lifecycle_schemas`;
the registry in ``_register.py`` only splats :data:`VM_REGISTER_COMPOSITES`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
)
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle import (
    OP_REGISTER_VM_TASK,
    OP_UNREGISTER_VM,
    POWERED_ON,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    _task_timeout_seconds,
    read_vm,
    run_vim_change,
    timeout_fields,
)
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle_schemas import (
    RESOURCE_POOL_MOID_PATTERN,
    VM_REGISTER_LLM_INSTRUCTIONS,
    VM_REGISTER_PARAMETER_SCHEMA,
    VM_REGISTER_RESPONSE_SCHEMA,
    VM_UNREGISTER_LLM_INSTRUCTIONS,
    VM_UNREGISTER_PARAMETER_SCHEMA,
    VM_UNREGISTER_RESPONSE_SCHEMA,
    VMX_PATH_PATTERN,
)
from meho_backplane.connectors.vmware_rest.composites._write import _unwrap_value
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
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
    "VM_REGISTER_COMPOSITES",
    "register_body",
    "vm_register_composite",
    "vm_unregister_composite",
]

OP_ID_VM_REGISTER: Final = "vmware.composite.vm.register"
OP_ID_VM_UNREGISTER: Final = "vmware.composite.vm.unregister"

STATUS_REGISTERED: Final = "registered"
STATUS_UNREGISTERED: Final = "unregistered"

_VMX_PATH_RE: Final = re.compile(VMX_PATH_PATTERN)

#: What vCenter drops with the inventory entry (the vendor's unregister text).
_LOST_WITH_IT: Final = (
    "the VM's statistics, permissions, alarms and resource pool settings in vCenter; the "
    "files on the datastore stay"
)


# ===========================================================================
# vm.register
# ===========================================================================


def _register_problem(params: dict[str, Any]) -> str | None:
    """Why the register inputs are not usable, or ``None``."""
    path = params.get("vmx_path")
    if not isinstance(path, str) or _VMX_PATH_RE.fullmatch(path) is None:
        return (
            "vmx_path must be a datastore path like '[<datastore-name>] <folder>/<file>.vmx' "
            f"(or .vmtx); got {path!r}"
        )
    problem = moid_problem(params.get("folder"), VM_FOLDER_MOID_PATTERN, "folder")
    if problem is not None:
        return problem
    pool, host = params.get("resource_pool"), params.get("host")
    if params.get("as_template"):
        if pool is not None or host is None:
            return "a template needs host and no resource_pool"
    elif pool is None:
        return "a VM needs resource_pool"
    if pool is not None and (
        problem := moid_problem(pool, RESOURCE_POOL_MOID_PATTERN, "resource_pool")
    ):
        return problem
    if host is not None and (problem := moid_problem(host, HOST_MOID_PATTERN, "host")):
        return problem
    return None


def register_body(params: dict[str, Any]) -> dict[str, Any]:
    """The ``RegisterVM_Task`` request body."""
    body: dict[str, Any] = {
        "path": params["vmx_path"],
        "asTemplate": bool(params.get("as_template", False)),
    }
    if params.get("name") is not None:
        body["name"] = params["name"]
    if params.get("resource_pool") is not None:
        body["pool"] = vim_moref("ResourcePool", params["resource_pool"])
    if params.get("host") is not None:
        body["host"] = vim_moref("HostSystem", params["host"])
    return body


def _register_envelope(params: dict[str, Any], status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vmx_path": params.get("vmx_path"),
        "vm": None,
        "after": None,
        "task": None,
        "guidance": None,
    }
    out.update(fields)
    return out


async def vm_register_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Register a VM or template from its files. Op-id: ``vmware.composite.vm.register``."""
    problem = _register_problem(params)
    if problem is not None:
        return _register_envelope(params, STATUS_INVALID_REQUEST, guidance=problem)
    folder = params["folder"]
    gate_params: dict[str, Any] = {
        "op": OP_ID_VM_REGISTER,
        **{
            k: params[k]
            for k in ("vmx_path", "folder", "name", "as_template", "resource_pool", "host")
            if k in params
        },
    }
    gate, payload = await run_vim_change(
        connector,
        target,
        operator,
        op_id=OP_REGISTER_VM_TASK,
        vmomi_path=f"/Folder/{folder}/RegisterVM_Task",
        body=register_body(params),
        params=gate_params,
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(payload),
        timeout_seconds=_task_timeout_seconds(),
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"{OP_ID_VM_REGISTER}: RegisterVM_Task for {params['vmx_path']!r} faulted "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            **_register_envelope(params, None),
            **timeout_fields(
                outcome, "RegisterVM_Task", "List the folder's VMs to see the result."
            ),
        }
    refs = moref_values([outcome.result]) if isinstance(outcome.result, dict) else []
    vm = refs[0][1] if refs else None
    after = await read_vm(connector, target, operator, vm) if vm is not None else None
    done = _register_envelope(params, STATUS_REGISTERED, vm=vm, after=after, task=outcome.task)
    if after is None:
        done["status"] = STATUS_NOT_CONFIRMED
        done["guidance"] = "RegisterVM_Task succeeded, but the new VM does not read back yet"
    return done


async def _register_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver which files are registered where."""
    params = ctx.params
    problem = _register_problem(params)
    kind = "template" if params.get("as_template") else "VM"
    preview: dict[str, Any] = {
        "summary": (
            f"Register the {kind} in {params.get('vmx_path')} into folder {params.get('folder')}."
        ),
        "vmx_path": params.get("vmx_path"),
        "folder": params.get("folder"),
        "name": params.get("name"),
        "resource_pool": params.get("resource_pool"),
        "host": params.get("host"),
    }
    if problem is not None:
        preview["summary"] = f"The call will send nothing: {problem}."
        preview["stops_with"] = STATUS_INVALID_REQUEST
    return preview


# ===========================================================================
# vm.unregister
# ===========================================================================


def _unregister_envelope(vm: str, status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vm": vm,
        "before": None,
        "task": None,
        "guidance": None,
    }
    out.update(fields)
    return out


async def _unregister_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> dict[str, Any]:
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _unregister_envelope(vm, STATUS_INVALID_REQUEST, guidance=problem)
    before = await read_vm(connector, target, operator, vm)
    if before is None:
        return _unregister_envelope(
            vm, STATUS_NOT_FOUND, guidance=f"no VM {vm!r} in the inventory; nothing was sent"
        )
    if before["power_state"] == POWERED_ON:
        return _unregister_envelope(
            vm,
            STATUS_PRECONDITION_FAILED,
            before=before,
            guidance="the VM is powered on; power it off first",
        )
    return _unregister_envelope(vm, None, before=before)


async def vm_unregister_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove a VM from the inventory, keep its files. Op-id: ``vm.unregister``."""
    vm = params["vm"]
    checked = await _unregister_precheck(connector, target, operator, vm)
    if checked["status"] is not None:
        return checked
    gate, _ = await run_vim_change(
        connector,
        target,
        operator,
        op_id=OP_UNREGISTER_VM,
        vmomi_path=f"/VirtualMachine/{vm}/UnregisterVM",
        body={},
        params={"op": OP_ID_VM_UNREGISTER, "vm": vm},
    )
    if gate is not None:
        return gate
    if await read_vm(connector, target, operator, vm) is not None:
        return {
            **checked,
            "status": STATUS_STILL_PRESENT,
            "guidance": "UnregisterVM succeeded, but the VM still reads back; read it again",
        }
    before = checked["before"]
    return {
        **checked,
        "status": STATUS_UNREGISTERED,
        "guidance": (
            f"The files stay at {before['vmx_path']}. To add the VM back, call "
            "vmware.composite.vm.register with this vmx_path and folder."
        ),
    }


async def _unregister_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, its files, and what vCenter drops."""
    vm = ctx.params.get("vm")
    if not isinstance(vm, str) or ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        checked = await _unregister_precheck(connector, ctx.target, ctx.operator, vm)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read VM {vm}, so it cannot show its state.",
            "vm": vm,
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": f"Remove VM {vm} from the vCenter inventory. Its files stay on the datastore.",
        "vm": vm,
        "current": checked["before"],
        "lost_with_it": _LOST_WITH_IT,
    }
    if checked["status"] is not None:
        preview["summary"] = f"The call will change nothing on {vm}: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


register_preview_builder(OP_ID_VM_REGISTER, _register_preview)
register_preview_builder(OP_ID_VM_UNREGISTER, _unregister_preview)


#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_REGISTER_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_REGISTER,
        "handler": vm_register_composite,
        "summary": "Register a VM or template from its .vmx or .vmtx file on a datastore.",
        "description": (
            "Adds a VM to the vCenter inventory from its files (VIM RegisterVM_Task): the "
            ".vmx path, a VM folder and a resource pool, or a host for a template. Returns "
            "the new VM id and state. Waits for approval."
        ),
        "parameter_schema": VM_REGISTER_PARAMETER_SCHEMA,
        "response_schema": VM_REGISTER_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "register", "vi-json"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": VM_REGISTER_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_VM_UNREGISTER,
        "handler": vm_unregister_composite,
        "summary": "Remove a VM from the vCenter inventory; its files stay on the datastore.",
        "description": (
            "Unregisters a VM or template, named by its VM id (VIM UnregisterVM). The VM must "
            "not be powered on. Its files stay; vCenter drops its statistics, permissions and "
            "alarms. Returns the .vmx path to register it again. Waits for approval."
        ),
        "parameter_schema": VM_UNREGISTER_PARAMETER_SCHEMA,
        "response_schema": VM_UNREGISTER_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "register", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_UNREGISTER_LLM_INSTRUCTIONS,
    },
)
