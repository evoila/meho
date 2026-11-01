# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create a vApp, and power it on or off in its start order.

* ``vmware.composite.vapp.create`` (``caution``) -- ``ResourcePool.CreateVApp``
  in a cluster's root resource pool or in a given pool, shown in a given VM
  folder. The vApp gets the default resources (no reservation, no limit,
  normal shares), the same defaults govmomi's ``DefaultResourceConfigSpec``
  sends. A vApp of the same name in the pool is ``unchanged``.
* ``vmware.composite.vapp.power`` (``dangerous``) -- ``PowerOnVApp_Task`` or
  ``PowerOffVApp_Task``. vCenter starts the VMs in their start order and stops
  them in the reverse order (``force`` skips the order on power off).

The start order itself is set by :mod:`._vapp_start_order`. Each handler checks
the input and reads the objects before any write; the approval previews run
the same checks. See :mod:`._vapp` for the shared steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_UNCHANGED,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vapp import (
    OP_CREATE_VAPP,
    OP_POWER_OFF_VAPP_TASK,
    OP_POWER_ON_VAPP_TASK,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    VAPP_MO_TYPE,
    read_vapp,
    run_task,
    sync_write,
    timeout_fields,
    vapp_state,
    vapp_view,
    vm_rows,
)
from meho_backplane.connectors.vmware_rest.composites._vapp_schemas import (
    RESOURCE_POOL_MOID_PATTERN,
    VAPP_CREATE_LLM_INSTRUCTIONS,
    VAPP_CREATE_PARAMETER_SCHEMA,
    VAPP_CREATE_RESPONSE_SCHEMA,
    VAPP_MOID_PATTERN,
    VAPP_POWER_LLM_INSTRUCTIONS,
    VAPP_POWER_PARAMETER_SCHEMA,
    VAPP_POWER_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "create_vapp_body",
    "vapp_create_composite",
    "vapp_power_composite",
]

OP_ID_VAPP_CREATE: Final = "vmware.composite.vapp.create"
OP_ID_VAPP_POWER: Final = "vmware.composite.vapp.power"

ACTION_POWER_ON: Final = "power_on"
ACTION_POWER_OFF: Final = "power_off"

_STARTED: Final = "started"
_STOPPED: Final = "stopped"
_RESOURCE_POOL_MO_TYPE: Final = "ResourcePool"
_FOLDER_MO_TYPE: Final = "Folder"
_CLUSTER_MO_TYPE: Final = "ClusterComputeResource"


async def _preview_or_error(
    ctx: PreviewContext,
    what: str,
    build: Callable[[VmwareRestConnector], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Run *build* on the preview's connector; a failed read becomes ``read_error``."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        return await build(connector)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read {what}, so it cannot show what changes.",
            "params": dict(ctx.params),
            "read_error": preview_failure_reason(exc),
        }


def _stops_with(preview: dict[str, Any], checked: dict[str, Any], label: str) -> dict[str, Any]:
    if checked["status"] is not None:
        preview["summary"] = f"The call will change nothing on {label}: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


# ===========================================================================
# vapp.create
# ===========================================================================


def _allocation() -> dict[str, Any]:
    """Default ``ResourceAllocationInfo``: CreateVApp needs every field set."""
    return {
        VIM_TYPE_NAME_KEY: "ResourceAllocationInfo",
        "reservation": 0,
        "expandableReservation": True,
        "limit": -1,
        "shares": {VIM_TYPE_NAME_KEY: "SharesInfo", "shares": 0, "level": "normal"},
    }


def create_vapp_body(name: str, folder: str, annotation: str | None) -> dict[str, Any]:
    """The ``ResourcePool.CreateVApp`` request body."""
    config: dict[str, Any] = {VIM_TYPE_NAME_KEY: "VAppConfigSpec"}
    if annotation:
        config["annotation"] = annotation
    return {
        "name": name,
        "resSpec": {
            VIM_TYPE_NAME_KEY: "ResourceConfigSpec",
            "cpuAllocation": _allocation(),
            "memoryAllocation": _allocation(),
        },
        "configSpec": config,
        "vmFolder": vim_moref(_FOLDER_MO_TYPE, folder),
    }


def _create_envelope(params: dict[str, Any], status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vapp": None,
        "name": params.get("name"),
        "state": None,
        "vms": [],
        "resource_pool": params.get("resource_pool"),
        "folder": params.get("folder"),
        "guidance": None,
    }
    out.update(fields)
    return out


def _create_input_problem(params: dict[str, Any]) -> str | None:
    if ("cluster" in params) == ("resource_pool" in params):
        return "give exactly one of 'cluster' or 'resource_pool'"
    if "cluster" in params:
        problem = moid_problem(params["cluster"], CLUSTER_MOID_PATTERN, "cluster")
    else:
        problem = moid_problem(params["resource_pool"], RESOURCE_POOL_MOID_PATTERN, "resource_pool")
    return problem or moid_problem(params.get("folder"), VM_FOLDER_MOID_PATTERN, "folder")


async def _resolve_pool(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, str]:
    """``(pool id, guidance)``; the id is ``None`` when the cluster or pool is missing."""
    if "cluster" in params:
        cluster = params["cluster"]
        props = await read_object(
            connector,
            target,
            operator,
            mo_type=_CLUSTER_MO_TYPE,
            moid=cluster,
            props=["resourcePool"],
        )
        pool = _moref_value(props.get("resourcePool")) if props is not None else None
        return pool, f"no cluster {cluster!r} with a root resource pool"
    return params["resource_pool"], f"no resource pool {params['resource_pool']!r}"


async def _create_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> dict[str, Any]:
    """The envelope the handler stops with, or one with ``status=None`` to go on."""
    problem = _create_input_problem(params)
    if problem is not None:
        return _create_envelope(params, STATUS_INVALID_REQUEST, guidance=problem)
    pool, missing = await _resolve_pool(connector, target, operator, params)
    pool_props = (
        await read_object(
            connector,
            target,
            operator,
            mo_type=_RESOURCE_POOL_MO_TYPE,
            moid=pool,
            props=["resourcePool"],
        )
        if pool is not None
        else None
    )
    if pool is None or pool_props is None:
        return _create_envelope(params, STATUS_NOT_FOUND, guidance=missing)
    base = _create_envelope(params, None, resource_pool=pool)
    folder = params["folder"]
    folder_props = await read_object(
        connector, target, operator, mo_type=_FOLDER_MO_TYPE, moid=folder, props=["childType"]
    )
    if folder_props is None:
        return {**base, "status": STATUS_NOT_FOUND, "guidance": f"no folder {folder!r}"}
    child_types = folder_props.get("childType")
    if not isinstance(child_types, list) or VAPP_MO_TYPE not in child_types:
        guidance = f"folder {folder!r} cannot hold vApps; pass a VM folder"
        return {**base, "status": STATUS_PRECONDITION_FAILED, "guidance": guidance}
    return await _name_check(connector, target, operator, base, pool_props)


async def _name_check(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    base: dict[str, Any],
    pool_props: dict[str, Any],
) -> dict[str, Any]:
    """``unchanged`` for a vApp of the same name in the pool; refuse a pool of that name."""
    children = moref_values(pool_props.get("resourcePool"))
    for mo_type in (VAPP_MO_TYPE, _RESOURCE_POOL_MO_TYPE):
        moids = [moid for kind, moid in children if kind == mo_type]
        names = await read_names(connector, target, operator, mo_type=mo_type, moids=moids)
        match = next((moid for moid, name in names.items() if name == base["name"]), None)
        if match is None:
            continue
        if mo_type == _RESOURCE_POOL_MO_TYPE:
            guidance = f"resource pool {match!r} in this pool has the name already; pick another"
            return {**base, "status": STATUS_PRECONDITION_FAILED, "guidance": guidance}
        return {
            **base,
            "status": STATUS_UNCHANGED,
            "vapp": match,
            "guidance": "a vApp with this name is in the pool already; nothing was sent",
        }
    return base


async def vapp_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create one empty vApp. Op-id: ``vmware.composite.vapp.create``."""
    checked = await _create_precheck(connector, target, operator, params)
    if checked["status"] is not None:
        if checked["status"] == STATUS_UNCHANGED:
            view = await vapp_view(connector, target, operator, checked["vapp"])
            return {**checked, **(view or {})}
        return checked
    pool = checked["resource_pool"]
    gate, payload = await sync_write(
        connector,
        target,
        operator,
        op_id=OP_CREATE_VAPP,
        vmomi_path=f"/{_RESOURCE_POOL_MO_TYPE}/{pool}/CreateVApp",
        body=create_vapp_body(params["name"], params["folder"], params.get("annotation")),
        params={
            "op": OP_ID_VAPP_CREATE,
            "name": params["name"],
            "resource_pool": pool,
            "folder": params["folder"],
        },
    )
    if gate is not None:
        return gate
    vapp = _moref_value(payload)
    view = await vapp_view(connector, target, operator, vapp) if vapp is not None else None
    if view is None or view["name"] != params["name"]:
        return {
            **checked,
            "status": STATUS_NOT_CONFIRMED,
            "vapp": vapp,
            "guidance": "CreateVApp answered, but the new vApp does not read back; read it again",
        }
    return {**checked, **view, "status": "created"}


async def _create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the pool, the folder and whether the call stops before the write."""
    params = ctx.params

    async def build(connector: VmwareRestConnector) -> dict[str, Any]:
        checked = await _create_precheck(connector, ctx.target, ctx.operator, params)
        preview = {
            "summary": (
                f"Create the empty vApp {params.get('name')!r} in resource pool "
                f"{checked['resource_pool']}, shown in folder {params.get('folder')}."
            ),
            "name": params.get("name"),
            "resource_pool": checked["resource_pool"],
            "folder": params.get("folder"),
        }
        return _stops_with(preview, checked, "the inventory")

    return await _preview_or_error(ctx, "the cluster, pool or folder", build)


# ===========================================================================
# vapp.power
# ===========================================================================


def _power_envelope(params: dict[str, Any], status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vapp": params.get("vapp"),
        "name": None,
        "action": params.get("action"),
        "force": bool(params.get("force", False)),
        "state_before": None,
        "state": None,
        "vms": [],
        "task": None,
        "task_state": None,
        "guidance": None,
    }
    out.update(fields)
    return out


def _wanted_state(action: str) -> str:
    return _STARTED if action == ACTION_POWER_ON else _STOPPED


async def _power_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> dict[str, Any]:
    """The envelope the handler stops with, or one with ``status=None`` to go on."""
    vapp = params["vapp"]
    problem = moid_problem(vapp, VAPP_MOID_PATTERN, "vapp")
    if problem is None and params.get("force") and params["action"] != ACTION_POWER_OFF:
        problem = "'force' works only with action power_off"
    if problem is not None:
        return _power_envelope(params, STATUS_INVALID_REQUEST, guidance=problem)
    props = await read_vapp(connector, target, operator, vapp)
    if props is None:
        guidance = f"no vApp {vapp!r}; pass a vApp id such as resgroup-v42"
        return _power_envelope(params, STATUS_NOT_FOUND, guidance=guidance)
    rows, _complete = await vm_rows(connector, target, operator, props)
    state = vapp_state(props)
    base = _power_envelope(
        params, None, name=props.get("name"), state_before=state, state=state, vms=rows
    )
    if state == _wanted_state(params["action"]):
        guidance = f"the vApp is {state} already; nothing was sent"
        return {**base, "status": STATUS_UNCHANGED, "guidance": guidance}
    if state not in (_STARTED, _STOPPED):
        guidance = (
            f"the vApp is {state or 'in an unknown state'}; wait until it ends, then call again"
        )
        return {**base, "status": STATUS_PRECONDITION_FAILED, "guidance": guidance}
    return base


async def vapp_power_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Power one vApp on or off in its start order. Op-id: ``vmware.composite.vapp.power``."""
    checked = await _power_precheck(connector, target, operator, params)
    if checked["status"] is not None:
        return checked
    vapp = params["vapp"]
    power_on = params["action"] == ACTION_POWER_ON
    method = "PowerOnVApp_Task" if power_on else "PowerOffVApp_Task"
    gate, outcome = await run_task(
        connector,
        target,
        operator,
        op_id=OP_POWER_ON_VAPP_TASK if power_on else OP_POWER_OFF_VAPP_TASK,
        vmomi_path=(
            f"/{VAPP_MO_TYPE}/{vapp}/PowerOnVApp_Task"
            if power_on
            else f"/{VAPP_MO_TYPE}/{vapp}/PowerOffVApp_Task"
        ),
        body={} if power_on else {"force": checked["force"]},
        params={"op": OP_ID_VAPP_POWER, "vapp": vapp, "action": params["action"]}
        | ({} if power_on else {"force": checked["force"]}),
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.timed_out:
        reread = "Run this call again: it answers unchanged once the vApp is there."
        return {**checked, **timeout_fields(outcome, method, reread)}
    view = await vapp_view(connector, target, operator, vapp)
    done = {**checked, **(view or {}), "task": outcome.task, "task_state": outcome.state}
    if view is None or view["state"] != _wanted_state(params["action"]):
        return {
            **done,
            "status": STATUS_NOT_CONFIRMED,
            "guidance": (
                f"{method} succeeded, but the vApp reads back as "
                f"{(view or {}).get('state') or 'unknown'}; read it again"
            ),
        }
    return {**done, "status": "powered_on" if power_on else "powered_off"}


async def _power_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the vApp, its VMs in start order, and the action."""
    params = ctx.params
    if not isinstance(params.get("vapp"), str) or params.get("action") not in (
        ACTION_POWER_ON,
        ACTION_POWER_OFF,
    ):
        return None

    async def build(connector: VmwareRestConnector) -> dict[str, Any]:
        checked = await _power_precheck(connector, ctx.target, ctx.operator, params)
        if params["action"] == ACTION_POWER_ON:
            how = "Start its VMs in their start order"
        elif checked["force"]:
            how = "Power off all its VMs at once, without the stop order"
        else:
            how = "Stop its VMs in the reverse start order"
        preview = {
            "summary": f"{how}: vApp {params['vapp']} ({len(checked['vms'])} VMs).",
            "vapp": params["vapp"],
            "name": checked["name"],
            "action": params["action"],
            "force": checked["force"],
            "state": checked["state_before"],
            "vms": checked["vms"],
        }
        return _stops_with(preview, checked, params["vapp"])

    return await _preview_or_error(ctx, f"vApp {params['vapp']}", build)


#: The registration rows; ``_register.py`` collects ``COMPOSITES`` from every module.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VAPP_CREATE,
        handler=vapp_create_composite,
        summary="Create an empty vApp in a cluster or resource pool.",
        description=(
            "Creates an empty vApp, a container that starts and stops its VMs in a set "
            "order, in a cluster's root resource pool or in a given pool, shown in a VM "
            "folder. Default resources. Waits for approval."
        ),
        parameter_schema=VAPP_CREATE_PARAMETER_SCHEMA,
        response_schema=VAPP_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "vapp", "resource-pool", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VAPP_CREATE_LLM_INSTRUCTIONS,
        preview=_create_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_VAPP_POWER,
        handler=vapp_power_composite,
        summary="Power a vApp on or off; its VMs follow the vApp start order.",
        description=(
            "Powers on a vApp, named by its id, so vCenter starts its VMs in their start "
            "order, or powers it off so they stop in the reverse order. Returns the vApp "
            "state and each VM's power state. Waits for approval."
        ),
        parameter_schema=VAPP_POWER_PARAMETER_SCHEMA,
        response_schema=VAPP_POWER_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "vapp", "power", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VAPP_POWER_LLM_INSTRUCTIONS,
        preview=_power_preview,
        classifier_other=True,
    ),
)
