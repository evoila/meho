# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Small helpers shared by the VM boot, upgrade and advanced-settings changes.

* :func:`refused` turns a vCenter HTTP 400 into a ``refused`` answer with
  vCenter's error type and text. Nothing changed in that case.
* :func:`reconfigure_vm` runs one governed ``ReconfigVM_Task`` and polls it to
  its end. A task fault raises with the vim fault type and text, so the
  dispatcher records a failed call and the caller sees why.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _VM_CONFIG_SPEC_TYPE,
    _parse_vsphere_error,
    _task_timeout_issue,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "RECONFIG_VM_TASK_CALL",
    "VM_ID_PROPERTY",
    "reconfigure_vm",
    "refused",
    "timeout_answer",
]

RECONFIG_VM_TASK_CALL: Final = _OP_RECONFIG_VM_TASK

#: Wall-clock bound for the ``ReconfigVM_Task`` poll; module-global so tests can zero it.
TASK_TIMEOUT_SECONDS = 600.0

#: The ``vm`` parameter of every change: the VM id only, never a name, so after
#: approval the change runs on the VM the approver saw.
VM_ID_PROPERTY: Final[dict[str, Any]] = {
    "vm": {
        "type": "string",
        "pattern": VM_MOID_PATTERN,
        "description": (
            "VM id, such as 'vm-42'. Get it from vmware.vm.info or vmware.vm.list. A change "
            "takes no VM name, so after approval it runs on the VM the approver saw."
        ),
    },
}


def refused(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map a vCenter HTTP 400 to ``refused``; ``None`` for any other fault."""
    status, error_type = _parse_vsphere_error(exc)
    if status != 400:
        return None
    return {
        **envelope,
        "status": "refused",
        "error_type": error_type,
        "guidance": f"vCenter refused the change: {_vsphere_fault_detail(exc)}",
    }


def timeout_answer(envelope: dict[str, Any], task: str, reread: str) -> dict[str, Any]:
    """The ``timeout`` answer for a task that did not finish in time."""
    return {
        **envelope,
        "status": "timeout",
        "task": task,
        "guidance": (
            f"ReconfigVM_Task {task} did not finish within {int(TASK_TIMEOUT_SECONDS)}s; it "
            f"may still finish. {reread}"
        ),
        "issues": [_task_timeout_issue("ReconfigVM_Task", task)],
    }


async def reconfigure_vm(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    spec_fields: dict[str, Any],
    params: dict[str, Any],
    label: str,
) -> tuple[OperationResult | None, str | None, bool]:
    """Run one governed ``ReconfigVM_Task`` with *spec_fields* and poll it.

    Returns ``(gate, task, timed_out)``. A gate (the write waits for approval
    or was denied) means nothing was sent. A task fault raises
    :exc:`RuntimeError` naming the vim fault type and text. *params* is the
    logical record of the write for the approval queue; *label* names the
    operation in the error.
    """
    spec = {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE, **spec_fields}
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/VirtualMachine/{vm}/ReconfigVM_Task",
        body={"spec": spec},
        params={"vm": vm, **params},
    )
    if gate is not None:
        return gate, None, False
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        fault = outcome.fault_type or "fault"
        raise RuntimeError(
            f"{label}: ReconfigVM_Task on vm {vm!r} failed ({fault}): "
            f"{outcome.error_message or '<no fault text>'}"
        )
    return None, outcome.task, outcome.timed_out
