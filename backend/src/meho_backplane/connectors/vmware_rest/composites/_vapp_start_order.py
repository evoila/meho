# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Set the start order of the VMs in a vApp -- ``vmware.composite.vapp.start_order.set``.

One synchronous ``VirtualApp.UpdateVAppConfig`` (``caution``) with one
``VAppEntityConfigInfo`` per given VM (``key`` = the VM). vCenter changes only
the fields that are set, so a field the caller leaves out keeps its value, and
a VM the caller does not list keeps all its settings. Before the write MEHO
checks that each VM is in the vApp; settings that are so already are
``unchanged`` (nothing sent). After the write MEHO reads the vApp again:
``updated`` only when every asked setting reads back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_UNCHANGED,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vapp import (
    ENTITY_FIELDS,
    OP_UPDATE_VAPP_CONFIG,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    VAPP_MO_TYPE,
    entity_settings,
    read_vapp,
    sync_write,
    vapp_state,
    view_of,
    vm_moids,
    vm_rows,
)
from meho_backplane.connectors.vmware_rest.composites._vapp_schemas import (
    VAPP_MOID_PATTERN,
    VAPP_START_ORDER_SET_LLM_INSTRUCTIONS,
    VAPP_START_ORDER_SET_PARAMETER_SCHEMA,
    VAPP_START_ORDER_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import _VIRTUAL_MACHINE_MO_TYPE
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "entity_config_spec",
    "vapp_start_order_set_composite",
]

OP_ID_VAPP_START_ORDER_SET: Final = "vmware.composite.vapp.start_order.set"

#: MEHO field name -> ``VAppEntityConfigInfo`` field.
_VIM_FIELD: Final[dict[str, str]] = {name: field for field, name in ENTITY_FIELDS.items()}


def _wanted(entry: dict[str, Any]) -> dict[str, Any]:
    """The settings one ``vms`` entry asks for (``vm`` left out)."""
    return {name: entry[name] for name in _VIM_FIELD if name in entry}


def entity_config_spec(vms: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``UpdateVAppConfig`` request body: one entity entry per VM, set fields only."""
    entries = []
    for entry in vms:
        config: dict[str, Any] = {
            VIM_TYPE_NAME_KEY: "VAppEntityConfigInfo",
            "key": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, entry["vm"]),
        }
        config.update({_VIM_FIELD[name]: value for name, value in _wanted(entry).items()})
        entries.append(config)
    return {"spec": {VIM_TYPE_NAME_KEY: "VAppConfigSpec", "entityConfig": entries}}


def _differences(settings: dict[str, dict[str, Any]], vms: list[dict[str, Any]]) -> list[str]:
    """The VM ids whose asked settings differ from *settings*."""
    current = {vm: settings.get(vm, {}) for vm in (e["vm"] for e in vms)}
    return [
        e["vm"]
        for e in vms
        if any(current[e["vm"]].get(name) != value for name, value in _wanted(e).items())
    ]


def _envelope(params: dict[str, Any], status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vapp": params.get("vapp"),
        "name": None,
        "state": None,
        "vms": [],
        "guidance": None,
    }
    out.update(fields)
    return out


def _input_problem(params: dict[str, Any]) -> str | None:
    problem = moid_problem(params.get("vapp"), VAPP_MOID_PATTERN, "vapp")
    if problem is not None:
        return problem
    seen: set[str] = set()
    for entry in params["vms"]:
        vm = entry.get("vm")
        problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
        if problem is not None:
            return problem
        if vm in seen:
            return f"VM {vm!r} is listed twice; give each VM once"
        seen.add(vm)
    return None


async def _precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> dict[str, Any]:
    """The envelope the handler stops with, or one with ``status=None`` to go on."""
    problem = _input_problem(params)
    if problem is not None:
        return _envelope(params, STATUS_INVALID_REQUEST, guidance=problem)
    vapp = params["vapp"]
    props = await read_vapp(connector, target, operator, vapp)
    if props is None:
        guidance = f"no vApp {vapp!r}; pass a vApp id such as resgroup-v42"
        return _envelope(params, STATUS_NOT_FOUND, guidance=guidance)
    rows, _complete = await vm_rows(connector, target, operator, props)
    base = _envelope(params, None, name=props.get("name"), state=vapp_state(props), vms=rows)
    members = set(vm_moids(props))
    outside = [e["vm"] for e in params["vms"] if e["vm"] not in members]
    if outside:
        guidance = f"not in this vApp: {', '.join(outside)}; move the VMs into it first"
        return {**base, "status": STATUS_PRECONDITION_FAILED, "guidance": guidance}
    if not _differences(entity_settings(props), params["vms"]):
        guidance = "every asked setting is so already; nothing was sent"
        return {**base, "status": STATUS_UNCHANGED, "guidance": guidance}
    return base


async def vapp_start_order_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set the start and stop settings of VMs in one vApp. Op-id: ``vapp.start_order.set``."""
    checked = await _precheck(connector, target, operator, params)
    if checked["status"] is not None:
        return checked
    vapp = params["vapp"]
    gate, _payload = await sync_write(
        connector,
        target,
        operator,
        op_id=OP_UPDATE_VAPP_CONFIG,
        vmomi_path=f"/{VAPP_MO_TYPE}/{vapp}/UpdateVAppConfig",
        body=entity_config_spec(params["vms"]),
        params={"op": OP_ID_VAPP_START_ORDER_SET, "vapp": vapp, "vms": params["vms"]},
    )
    if gate is not None:
        return gate
    after = await read_vapp(connector, target, operator, vapp)
    if after is None:
        return {**checked, "status": STATUS_NOT_CONFIRMED, "guidance": "the vApp did not read back"}
    view = await view_of(connector, target, operator, vapp, after)
    left = _differences(entity_settings(after), params["vms"])
    if left:
        guidance = f"UpdateVAppConfig answered, but these VMs do not read back as asked: {left}"
        return {**checked, **view, "status": STATUS_NOT_CONFIRMED, "guidance": guidance}
    return {**checked, **view, "status": "updated"}


async def _start_order_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VMs' settings now and the asked changes."""
    params = ctx.params
    vapp = params.get("vapp")
    if not isinstance(vapp, str) or not isinstance(params.get("vms"), list):
        return None
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        checked = await _precheck(connector, ctx.target, ctx.operator, params)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read vApp {vapp}, so it cannot show its settings.",
            "vapp": vapp,
            "requested": params["vms"],
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": f"Change the start and stop settings of {len(params['vms'])} VMs in {vapp}.",
        "vapp": vapp,
        "name": checked["name"],
        "current": checked["vms"],
        "requested": params["vms"],
    }
    if checked["status"] is not None:
        preview["summary"] = f"The call will change nothing on {vapp}: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


#: The registration rows; ``_register.py`` collects ``COMPOSITES`` from every module.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VAPP_START_ORDER_SET,
        handler=vapp_start_order_set_composite,
        summary="Set the start order, delays and start and stop actions of VMs in a vApp.",
        description=(
            "Changes how a vApp, named by its id, starts and stops the given VMs: start "
            "group, start and stop delay, wait for the guest, start and stop action. Fields "
            "left out keep their value. Waits for approval."
        ),
        parameter_schema=VAPP_START_ORDER_SET_PARAMETER_SCHEMA,
        response_schema=VAPP_START_ORDER_SET_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "vapp", "start-order", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VAPP_START_ORDER_SET_LLM_INSTRUCTIONS,
        preview=_start_order_preview,
        classifier_other=True,
    ),
)
