# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the VM move and copy jobs (``vm.relocate``, ``vm.clone_from_vm``).

Both send one ``VirtualMachine`` task method through the governed VI-JSON seam
and answer ``accepted`` with the vCenter task id; ``vmware.task.info`` follows
the task. Before a start both look for a task of the same kind that still
runs on the VM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_many,
)
from meho_backplane.connectors.vmware_rest.composites._vm_move_copy_schemas import (
    RESOURCE_POOL_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "FOLDER_MO_TYPE",
    "PLACEMENT_FIELDS",
    "TASK_INFO_OP_ID",
    "VM_MO_TYPE",
    "as_str",
    "moref_id",
    "placement_problem",
    "running_task",
    "send_vm_task",
    "status_read",
]

TASK_INFO_OP_ID: Final = "vmware.task.info"
TASK_MO_TYPE: Final = "Task"
VM_MO_TYPE: Final = "VirtualMachine"
FOLDER_MO_TYPE: Final = "Folder"
RUNNING_TASK_STATES: Final = frozenset({"queued", "running"})

#: (param, vim spec field, managed object type, moid pattern) of a placement target.
PLACEMENT_FIELDS: Final = (
    ("datastore", "datastore", "Datastore", DATASTORE_MOID_PATTERN),
    ("resource_pool", "pool", "ResourcePool", RESOURCE_POOL_MOID_PATTERN),
    ("folder", "folder", FOLDER_MO_TYPE, VM_FOLDER_MOID_PATTERN),
    ("host", "host", "HostSystem", HOST_MOID_PATTERN),
)


def moref_id(raw: Any) -> str | None:
    refs = moref_values([raw] if isinstance(raw, dict) else raw)
    return refs[0][1] if refs else None


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def status_read(task: str | None) -> dict[str, Any] | None:
    return None if task is None else {"op_id": TASK_INFO_OP_ID, "params": {"task": task}}


def placement_problem(params: dict[str, Any]) -> str | None:
    """The first placement moid of the wrong shape, else ``None``."""
    for name, _field, _mo_type, pattern in PLACEMENT_FIELDS:
        if name in params and params[name] is not None:
            problem = moid_problem(params[name], pattern, name)
            if problem is not None:
                return problem
    return None


async def running_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    recent: Any,
    description_id: str,
) -> str | None:
    """The id of a task of *description_id* that still runs on the VM, else ``None``.

    A help, not a lock: two callers can both look before either starts, and a
    task that just ended is no longer running.
    """
    task_ids = [moid for kind, moid in moref_values(recent) if kind in ("", TASK_MO_TYPE)]
    if not task_ids:
        return None
    infos, _complete = await read_many(
        connector, target, operator, mo_type=TASK_MO_TYPE, moids=task_ids, props=["info"]
    )
    for task_id in task_ids:
        info = infos.get(task_id, {}).get("info")
        if (
            isinstance(info, dict)
            and info.get("descriptionId") == description_id
            and info.get("state") in RUNNING_TASK_STATES
        ):
            return task_id
    return None


async def send_vm_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    gate_params: dict[str, Any],
) -> tuple[OperationResult | None, str | None]:
    """Gate and send one ``VirtualMachine`` task method: ``(gate, task id)``."""
    gate, payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=vmomi_path,
        body=body,
        params=gate_params,
    )
    if gate is not None:
        return gate, None
    task = moref_id(_unwrap_value(payload))
    if task is None:
        raise RuntimeError(f"{vmomi_path} returned no task reference")
    return None, task
