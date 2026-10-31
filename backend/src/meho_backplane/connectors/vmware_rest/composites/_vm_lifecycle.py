# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the VM template, register and instant clone changes.

Five changes use this module:

* ``vm.template.mark`` and ``vm.template.unmark`` (:mod:`._vm_template`),
* ``vm.register`` and ``vm.unregister`` (:mod:`._vm_register`),
* ``vm.instant_clone`` (:mod:`._vm_instant_clone`).

They read the VM through the VIM API as VI-JSON (``RetrievePropertiesEx``),
run their one vendor call through the governed write seams of :mod:`._write`
and read the VM again to return its new state. The VM read gives the same
view in every answer: name, power state, whether it is a template, its
``.vmx`` file and where it sits (folder, resource pool, host).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._teardown import STATUS_TIMEOUT
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _task_timeout_issue,
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult

__all__ = [
    "POWERED_OFF",
    "POWERED_ON",
    "STATUS_NOT_CONFIRMED",
    "STATUS_NOT_FOUND",
    "read_vm",
    "run_vim_change",
    "timeout_fields",
    "vm_view",
]

# --- the vendor calls (VI-JSON method op ids, as the ingest parser emits them)
OP_MARK_AS_TEMPLATE: Final = "POST:/VirtualMachine/{moId}/MarkAsTemplate"
OP_MARK_AS_VIRTUAL_MACHINE: Final = "POST:/VirtualMachine/{moId}/MarkAsVirtualMachine"
OP_REGISTER_VM_TASK: Final = "POST:/Folder/{moId}/RegisterVM_Task"
OP_UNREGISTER_VM: Final = "POST:/VirtualMachine/{moId}/UnregisterVM"

#: Child-op lists, one per change: the VM read and the one vendor call. The
#: ``_VIM_SUB_OPS_*`` names keep the vcenter.yaml sweep off them; the reconcile
#: lane checks them against the pinned vi-json.yaml.
_VIM_SUB_OPS_VM_TEMPLATE_MARK: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_MARK_AS_TEMPLATE,
)
_VIM_SUB_OPS_VM_TEMPLATE_UNMARK: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_MARK_AS_VIRTUAL_MACHINE,
)
_VIM_SUB_OPS_VM_REGISTER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_REGISTER_VM_TASK,
)
_VIM_SUB_OPS_VM_UNREGISTER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_UNREGISTER_VM,
)
#: The instant clone reads the source and the new VM through VI-JSON; its
#: REST call is in :mod:`._vm_instant_clone`.
_VIM_SUB_OPS_VM_INSTANT_CLONE: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES,)

POWERED_OFF: Final = "poweredOff"
POWERED_ON: Final = "poweredOn"

STATUS_NOT_FOUND: Final = "not_found"
STATUS_NOT_CONFIRMED: Final = "not_confirmed"

_PROP_POWER_STATE: Final = "runtime.powerState"
_PROP_TEMPLATE: Final = "config.template"
_PROP_VMX_PATH: Final = "config.files.vmPathName"
_PROP_PARENT: Final = "parent"
_PROP_RESOURCE_POOL: Final = "resourcePool"
_PROP_HOST: Final = "runtime.host"
_VM_PROPS: Final = [
    "name",
    _PROP_POWER_STATE,
    _PROP_TEMPLATE,
    _PROP_VMX_PATH,
    _PROP_PARENT,
    _PROP_RESOURCE_POOL,
    _PROP_HOST,
]

#: Wall-clock bound for a vCenter task poll -- the 600 s convention;
#: module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0


def _task_timeout_seconds() -> float:
    return _TASK_TIMEOUT_SECONDS


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _moid(raw: Any) -> str | None:
    refs = moref_values([raw]) if isinstance(raw, dict) else []
    return refs[0][1] if refs else None


def vm_view(vm: str, props: dict[str, Any]) -> dict[str, Any]:
    """The VM as every answer of these changes shows it."""
    template = props.get(_PROP_TEMPLATE)
    return {
        "vm": vm,
        "name": _str(props.get("name")),
        "power_state": _str(props.get(_PROP_POWER_STATE)),
        "is_template": template if isinstance(template, bool) else None,
        "vmx_path": _str(props.get(_PROP_VMX_PATH)),
        "folder": _moid(props.get(_PROP_PARENT)),
        "resource_pool": _moid(props.get(_PROP_RESOURCE_POOL)),
        "host": _moid(props.get(_PROP_HOST)),
    }


async def read_vm(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> dict[str, Any] | None:
    """The VM view of *vm*, or ``None`` when no such VM exists."""
    props = await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=_VM_PROPS
    )
    return None if props is None else vm_view(vm, props)


async def run_vim_change(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate and send one VI-JSON change; a vendor fault raises with its type and text.

    A parked or denied gate comes back as ``(gate, None)`` with nothing sent.
    """
    try:
        return await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=params,
        )
    except httpx.HTTPStatusError as exc:
        method = vmomi_path.rsplit("/", 1)[-1]
        raise RuntimeError(
            f"{params['op']}: {method} failed: {_vsphere_fault_detail(exc)}"
        ) from exc


def timeout_fields(outcome: VimTaskResult, task_kind: str, reread: str) -> dict[str, Any]:
    return {
        "status": STATUS_TIMEOUT,
        "task": outcome.task,
        "guidance": (
            f"{task_kind} {outcome.task} did not finish within "
            f"{int(_TASK_TIMEOUT_SECONDS)}s; it may still finish. {reread}"
        ),
        "issues": [_task_timeout_issue(task_kind, outcome.task)],
    }
