# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows, step lists and approval preview of the host power operations.

``_register.py`` splats :data:`HOST_POWER_COMPOSITES` into its
``_COMPOSITES`` table, and ``_governed_subops.py`` reads
:data:`HOST_POWER_GOVERNED_SUBOPS`. Every row is a change with
``requires_approval=True``: ``dangerous`` for maintenance-mode start, reboot,
shut down and standby, ``caution`` for the wake from standby.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _host_maintenance_enter,
    _host_power,
)
from meho_backplane.connectors.vmware_rest.composites._host_power_common import (
    host_state,
    read_host,
)
from meho_backplane.connectors.vmware_rest.composites._host_power_schemas import (
    HOST_POWER_LLM_INSTRUCTIONS,
    HOST_POWER_PARAMETER_SCHEMAS,
    HOST_POWER_RESPONSE_SCHEMAS,
    OP_MAINTENANCE_ENTER,
    OP_REBOOT,
    OP_SHUTDOWN,
    OP_STANDBY_ENTER,
    OP_STANDBY_EXIT,
)
from meho_backplane.connectors.vmware_rest.composites._write import _resolve_vm_list
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["HOST_POWER_COMPOSITES", "HOST_POWER_GOVERNED_SUBOPS", "host_power_preview"]

_TAGS: Final = ["composite", "write", "host", "vi-json"]


def _row(
    op_id: str, handler: Any, summary: str, description: str, safety_level: str, tag: str
) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "handler": handler,
        "summary": summary,
        "description": description,
        "parameter_schema": HOST_POWER_PARAMETER_SCHEMAS[op_id],
        "response_schema": HOST_POWER_RESPONSE_SCHEMAS[op_id],
        "group_key": "host",
        "tags": [*_TAGS, tag],
        "safety_level": safety_level,
        "requires_approval": True,
        "llm_instructions": HOST_POWER_LLM_INSTRUCTIONS[op_id],
    }


HOST_POWER_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    _row(
        OP_MAINTENANCE_ENTER,
        _host_maintenance_enter.host_maintenance_enter_composite,
        "Start maintenance mode on a host, with a vSAN data option, after approval.",
        (
            "Starts maintenance mode on one host with a vSAN data option and a timeout. It "
            "does not move VMs (host.evacuate does). Answers accepted with the vCenter task id "
            "at once; follow it with vmware.task.info."
        ),
        "dangerous",
        "maintenance",
    ),
    _row(
        OP_REBOOT,
        _host_power.host_reboot_composite,
        "Reboot a host that is in maintenance mode, after approval.",
        (
            "Reboots one host. Refuses a host that is not in maintenance mode, unless force "
            "is true. Waits for the vCenter task, then reads the host again."
        ),
        "dangerous",
        "power",
    ),
    _row(
        OP_SHUTDOWN,
        _host_power.host_shutdown_composite,
        "Shut down a host that is in maintenance mode, after approval.",
        (
            "Powers off one host. Refuses a host that is not in maintenance mode, unless "
            "force is true. Waits for the vCenter task, then reads the host again. MEHO "
            "cannot power the host on again."
        ),
        "dangerous",
        "power",
    ),
    _row(
        OP_STANDBY_ENTER,
        _host_power.host_standby_enter_composite,
        "Put a host into standby to save power, after approval.",
        (
            "Puts one host into standby (power saving); vCenter moves its running VMs off "
            "first. Waits for the vCenter task, then reads the host again. A host in standby "
            "answers unchanged."
        ),
        "dangerous",
        "power",
    ),
    _row(
        OP_STANDBY_EXIT,
        _host_power.host_standby_exit_composite,
        "Wake a host from standby, after approval.",
        (
            "Wakes one host from standby. Waits for the vCenter task, then reads the host "
            "again. A powered-on host answers unchanged and nothing is sent."
        ),
        "caution",
        "power",
    ),
)

#: composite op id -> the vendor calls it may make (VI-JSON).
HOST_POWER_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_MAINTENANCE_ENTER: _host_maintenance_enter._VIM_SUB_OPS_HOST_MAINTENANCE_ENTER,
    OP_REBOOT: _host_power._VIM_SUB_OPS_HOST_REBOOT,
    OP_SHUTDOWN: _host_power._VIM_SUB_OPS_HOST_SHUTDOWN,
    OP_STANDBY_ENTER: _host_power._VIM_SUB_OPS_HOST_STANDBY_ENTER,
    OP_STANDBY_EXIT: _host_power._VIM_SUB_OPS_HOST_STANDBY_EXIT,
}


_WHAT_HAPPENS_NEXT: Final[dict[str, str]] = {
    OP_MAINTENANCE_ENTER: (
        "vCenter starts maintenance mode. Running VMs must leave the host first (DRS moves "
        "them when it is fully automated). vSAN handles the host's data as vsan_mode says."
    ),
    OP_REBOOT: "The host restarts. Every VM still running on it stops.",
    OP_SHUTDOWN: (
        "The host powers off. Every VM still running on it stops. MEHO cannot power it on."
    ),
    OP_STANDBY_ENTER: "vCenter moves the running VMs off, then puts the host in standby.",
    OP_STANDBY_EXIT: "vCenter wakes the host. DRS may then move VMs onto it.",
}


async def host_power_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The host's state and the VMs on it, for the approval page. Sends no change.

    A failed read does not block the approval: the preview keeps what it has
    and names what is missing under ``unavailable`` (the fault class only).
    """
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    preview: dict[str, Any] = {"host": host}
    for key in ("vsan_mode", "force", "timeout_seconds", "evacuate_powered_off_vms"):
        if key in ctx.params:
            preview[key] = ctx.params[key]
    unavailable: list[str] = []
    try:
        props = await read_host(connector, ctx.target, ctx.operator, host)
    except Exception as exc:
        unavailable.append(f"the host state: the host read failed ({type(exc).__name__})")
    else:
        if props is not None:
            preview.update(host_state(props).as_fields())
    try:
        rows = await _resolve_vm_list(
            connector=connector,
            target=ctx.target,
            operator=ctx.operator,
            filter_dict={"hosts": [host]},
        )
    except Exception as exc:
        unavailable.append(f"the VMs on the host: the VM listing failed ({type(exc).__name__})")
    else:
        preview["vms"] = [
            {"vm": row.get("vm"), "name": row.get("name"), "power_state": row.get("power_state")}
            for row in rows[:20]
        ]
        preview["vm_count"] = len(rows)
    preview["what_happens_next"] = _WHAT_HAPPENS_NEXT.get(ctx.descriptor.op_id, "")
    if unavailable:
        preview["unavailable"] = unavailable
    return preview


for _op_id in _WHAT_HAPPENS_NEXT:
    register_preview_builder(_op_id, host_power_preview)
