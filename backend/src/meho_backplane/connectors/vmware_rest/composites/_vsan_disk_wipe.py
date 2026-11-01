# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vsan.disk.wipe`` -- wipe disks so vSAN can claim them.

All data on the disks is lost. Two methods:

* ``partitions`` (the default) writes an empty partition table on each disk
  with ``HostStorageSystem.UpdateDiskPartitions``, one disk after the other.
* ``secure`` runs the vSAN secure wipe (``VsanSystemEx.VsanHostWipeDisk``) as
  one task; ``VsanHostQueryWipeDisk`` gives the state before and after.

The wipe uses the plan-then-act seam of :mod:`._teardown`: one read-only plan
feeds the approval preview (the ``blast_radius``) and the handler. The plan
refuses a host or a disk that does not exist, a disk that vSAN uses, a disk
that holds a VMFS datastore, and (secure wipe) a disk vSAN cannot wipe. It
lists each disk with its partitions, so the approver sees what is erased.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    LIST_CAP,
    TeardownPlan,
    blast_radius,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object
from meho_backplane.connectors.vmware_rest.composites._vsan_config_common import (
    VSAN_HOST_QUERY_WIPE_DISK,
    VSAN_HOST_WIPE_DISK,
    disk_results,
    host_vsan_system_ex,
    send_vsan_change,
    vsan_op_id,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_disk_reads import (
    OP_QUERY_DISKS_FOR_VSAN_9,
    PROP_STORAGE_SYSTEM,
    PROP_VSAN_SYSTEM,
    STATE_IN_USE,
    disk_results_by_name,
    disk_row,
    exception_text,
    host_problem,
    read_host,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    fault_message,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["plan_disk_wipe", "vsan_disk_wipe_composite", "vsan_disk_wipe_preview"]

_OP_RETRIEVE_DISK_PARTITION_INFO: Final = "POST:/HostStorageSystem/{moId}/RetrieveDiskPartitionInfo"
_OP_UPDATE_DISK_PARTITIONS: Final = "POST:/HostStorageSystem/{moId}/UpdateDiskPartitions"

#: Child-op list: the reads, then the two ways to wipe.
_VSAN_SUB_OPS_DISK_WIPE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_QUERY_DISKS_FOR_VSAN_9,
    _OP_RETRIEVE_DISK_PARTITION_INFO,
    vsan_op_id(VSAN_HOST_QUERY_WIPE_DISK),
    _OP_UPDATE_DISK_PARTITIONS,
    vsan_op_id(VSAN_HOST_WIPE_DISK),
)

_STORAGE_SYSTEM_MO_TYPE: Final = "HostStorageSystem"
_PROP_FILE_SYSTEM_VOLUME_INFO: Final = "fileSystemVolumeInfo"

#: Wall-clock bound of the secure wipe poll -- the 600 s convention; tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_WIPED: Final = "wiped"
STATUS_UNCHANGED: Final = "unchanged"
STATUS_INVALID_REQUEST: Final = "invalid_request"
STATUS_PRECONDITION_FAILED: Final = "precondition_failed"
STATUS_FAILED_PART_WAY: Final = "failed_part_way"
STATUS_TIMEOUT: Final = "timeout"

#: What the approver reads next to ``irreversibility``.
_WIPE_EFFECT: Final = (
    "All partitions and all data on these disks are erased. This cannot be undone."
)


def _vmfs_disks(volume_info: Any) -> dict[str, str]:
    """``{disk canonical name: VMFS volume name}`` from ``fileSystemVolumeInfo``."""
    found: dict[str, str] = {}
    info = unwrap_vim_value(volume_info)
    mounts = info.get("mountInfo") if isinstance(info, dict) else None
    for mount in disk_results(mounts):
        volume = mount.get("volume")
        if not isinstance(volume, dict):
            continue
        for extent in disk_results(volume.get("extent")):
            name = extent.get("diskName")
            if isinstance(name, str):
                found[name] = str(volume.get("name") or "")
    return found


def _partitions(info: dict[str, Any]) -> list[dict[str, Any]]:
    """The partitions of one ``HostDiskPartitionInfo``, as flat rows."""
    raw = info.get("spec")
    spec: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return [
        {
            "partition": part.get("partition"),
            "type": part.get("type"),
            "start_sector": part.get("startSector"),
            "end_sector": part.get("endSector"),
        }
        for part in disk_results(spec.get("partition"))
    ]


async def _partition_info(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    storage_system: str,
    device_paths: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """``{device path: partitions}`` from ``RetrieveDiskPartitionInfo``."""
    if not device_paths:
        return {}
    answer = await connector._post_vmomi_json(
        target,
        f"/HostStorageSystem/{storage_system}/RetrieveDiskPartitionInfo",
        operator=operator,
        json={"devicePath": device_paths},
    )
    return {
        str(info.get("deviceName")): _partitions(info)
        for info in disk_results(answer)
        if isinstance(info.get("deviceName"), str)
    }


async def _secure_wipe_status(
    connector: VmwareRestConnector, target: Any, operator: Operator, *, host: str, disks: list[str]
) -> dict[str, dict[str, Any]]:
    """``{disk: VsanHostWipeDiskStatus}`` (a read, not gated)."""
    answer = await connector._post_vsan(
        target,
        host_vsan_system_ex(VSAN_HOST_QUERY_WIPE_DISK, host),
        operator=operator,
        json={"disks": disks},
    )
    return {
        str(row.get("disk")): row
        for row in disk_results(answer)
        if isinstance(row.get("disk"), str)
    }


def _wipe_status_row(status: dict[str, Any] | None) -> dict[str, Any]:
    if status is None:
        return {"wipe_eligible": None, "wipe_state": None, "wipe_percent": None}
    reasons = [
        fault_message(reason) or reason.get("message")
        for reason in disk_results(status.get("ineligibleReason"))
    ]
    return {
        "wipe_eligible": status.get("eligible"),
        "wipe_state": status.get("wipeState"),
        "wipe_percent": status.get("percentageCompleted"),
        "wipe_ineligible_reasons": [r for r in reasons if r],
    }


def _wipe_blockers(
    names: list[str],
    by_name: dict[str, dict[str, Any]],
    vmfs: dict[str, str],
    secure: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Why each disk must not be wiped (empty when all may be wiped)."""
    blockers: list[dict[str, Any]] = []
    for name in names:
        result = by_name.get(name)
        if result is None:
            blockers.append({"disk": name, "reason": "the host has no disk with this name"})
            continue
        if result.get("state") == STATE_IN_USE:
            blockers.append(
                {"disk": name, "reason": "vSAN uses this disk; remove it from vSAN first"}
            )
        if name in vmfs:
            blockers.append(
                {"disk": name, "reason": f"the disk holds the VMFS datastore {vmfs[name]!r}"}
            )
        if secure is not None and (secure.get(name) or {}).get("eligible") != "Yes":
            blockers.append(
                {"disk": name, "reason": "vSAN says this disk cannot be securely wiped"}
            )
    return blockers


async def plan_disk_wipe(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> TeardownPlan:
    """Plan a disk wipe (read-only): the disks, their partitions, and the blockers."""
    host = params.get("host")
    names = list(params.get("disks") or [])
    method = params.get("method") or "partitions"
    obj: dict[str, Any] = {"kind": "vsan_disks", "host": host, "method": method, "disks": names}
    problem = host_problem(params, "disks")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    assert isinstance(host, str)
    props = await read_host(connector, target, operator, host)
    if props is None:
        refusal = (STATUS_INVALID_REQUEST, f"host {host!r} not found; pass a host moid")
        return TeardownPlan(obj, False, refusal=refusal)
    obj["host_name"] = props.get("name") if isinstance(props.get("name"), str) else None
    vsan_system = _moref_value(props.get(PROP_VSAN_SYSTEM))
    storage_system = _moref_value(props.get(PROP_STORAGE_SYSTEM))
    if vsan_system is None or storage_system is None:
        refusal = (STATUS_PRECONDITION_FAILED, f"host {host!r} has no vSAN or storage system")
        return TeardownPlan(obj, False, refusal=refusal)
    by_name = await disk_results_by_name(
        connector, target, operator, vsan_system=vsan_system, disks=names
    )
    storage = await read_object(
        connector,
        target,
        operator,
        mo_type=_STORAGE_SYSTEM_MO_TYPE,
        moid=storage_system,
        props=[_PROP_FILE_SYSTEM_VOLUME_INFO],
    )
    vmfs = _vmfs_disks((storage or {}).get(_PROP_FILE_SYSTEM_VOLUME_INFO))
    rows = {name: disk_row(result) for name, result in by_name.items()}
    paths = [row["device_path"] for row in rows.values() if isinstance(row["device_path"], str)]
    partitions = await _partition_info(
        connector, target, operator, storage_system=storage_system, device_paths=paths
    )
    secure = None
    if method == "secure":
        secure = await _secure_wipe_status(connector, target, operator, host=host, disks=names)
    children: list[dict[str, Any]] = []
    for name in names:
        if name not in rows:
            continue
        row = rows[name]
        child = {**row, "partitions": partitions.get(str(row["device_path"]), [])}
        if secure is not None:
            child.update(_wipe_status_row(secure.get(name)))
        children.append(child)
    plan = TeardownPlan(
        object=obj,
        present=True,
        # The preview shows the first LIST_CAP disks; the wipe runs on all of them.
        children=children[:LIST_CAP],
        context={
            "storage_system": storage_system,
            "vsan_system": vsan_system,
            "disks": children,
        },
    )
    plan.blockers = _wipe_blockers(names, by_name, vmfs, secure)
    if plan.blockers:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            "some disks must not be wiped (see blockers). Nothing was wiped.",
        )
    elif method == "partitions" and not any(child["partitions"] for child in children):
        plan.present = False
    return plan


def _wipe_answer(
    plan: TeardownPlan,
    status: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
    disks_after: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "object": plan.object,
        "disks": plan.children,
        "blockers": plan.blockers,
        "steps": steps or [],
        "disks_after": disks_after,
        "task": task,
        "task_state": task_state,
        "guidance": guidance,
    }


async def _disks_after(
    connector: VmwareRestConnector, target: Any, operator: Operator, plan: TeardownPlan
) -> list[dict[str, Any]] | None:
    """The disks read again after the wipe; ``None`` when the read fails."""
    names = list(plan.object["disks"])
    try:
        by_name = await disk_results_by_name(
            connector, target, operator, vsan_system=plan.context["vsan_system"], disks=names
        )
    except Exception:
        return None
    return [disk_row(by_name[name]) for name in names if name in by_name]


async def _wipe_partitions(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    plan: TeardownPlan,
) -> dict[str, Any] | OperationResult:
    """Erase the partition table of each disk, one after the other.

    A failure stops the run. The answer lists the disks that were wiped, the
    one that failed with vCenter's error, and the ones not touched. Nothing
    is undone.
    """
    storage_system = plan.context["storage_system"]
    host = plan.object["host"]
    steps = [
        {"disk": child["name"], "device_path": child["device_path"], "status": "not_run"}
        for child in plan.context["disks"]
    ]
    for step in steps:
        try:
            gate, _answer = await _write_vmomi_sub_op(
                connector,
                target,
                operator,
                op_id=_OP_UPDATE_DISK_PARTITIONS,
                vmomi_path=f"/HostStorageSystem/{storage_system}/UpdateDiskPartitions",
                body={
                    "devicePath": step["device_path"],
                    "spec": {VIM_TYPE_NAME_KEY: "HostDiskPartitionSpec"},
                },
                params={"host": host, "disk": step["disk"], "device_path": step["device_path"]},
            )
        except Exception as exc:
            step.update(status="failed", error=exception_text(exc))
            failed = _wipe_answer(
                plan,
                STATUS_FAILED_PART_WAY,
                steps=steps,
                guidance=(
                    f"the wipe of {step['disk']} failed; the disks before it are wiped, the "
                    "disks after it are not touched. Nothing was undone. Fix the cause, then "
                    "call this operation again with the disks that are not wiped."
                ),
            )
            failed["issues"] = [
                issue("connector", "error", f"wipe of {step['disk']} failed: {step['error']}")
            ]
            return failed
        if gate is not None:
            return gate
        step["status"] = "done"
    after = await _disks_after(connector, target, operator, plan)
    return _wipe_answer(plan, STATUS_WIPED, steps=steps, disks_after=after)


async def _wipe_secure(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    plan: TeardownPlan,
) -> dict[str, Any] | OperationResult:
    """Run the vSAN secure wipe on all disks in one task, then read the wipe status."""
    host = plan.object["host"]
    names = list(plan.object["disks"])
    gate, answer = await send_vsan_change(
        connector,
        target,
        operator,
        method=host_vsan_system_ex(VSAN_HOST_WIPE_DISK, host),
        body={"disks": names},
        params={"host": host, "disks": names, "method": "secure"},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(answer),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vsan.disk.wipe (secure) on host {host!r} failed: "
            f"{outcome.fault_type or 'fault'}: {outcome.error_message or '<no fault text>'}"
        )
    if outcome.timed_out:
        timed_out = _wipe_answer(
            plan,
            STATUS_TIMEOUT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"the secure wipe task {outcome.task} did not end within "
                f"{int(_TASK_TIMEOUT_SECONDS)} seconds; a secure wipe can take hours. Follow "
                "it with vmware.tasks.recent. Do not start it again."
            ),
        )
        timed_out["issues"] = [_task_timeout_issue("VsanHostWipeDisk", outcome.task)]
        return timed_out
    status = await _secure_wipe_status(connector, target, operator, host=host, disks=names)
    after = [{"name": name, **_wipe_status_row(status.get(name))} for name in names]
    return _wipe_answer(
        plan,
        STATUS_WIPED,
        task=outcome.task,
        task_state=outcome.state,
        disks_after=after,
    )


async def vsan_disk_wipe_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Wipe disks of one host so vSAN can claim them. All data on them is lost.

    Op-id: ``vmware.composite.vsan.disk.wipe``. Plans first (the same plan the
    approval preview shows): a refusal or a blocker sends nothing; disks
    with no partitions are ``unchanged`` (partitions method). Then the
    partition tables are erased one disk after the other, or the vSAN secure
    wipe runs as one task.
    """
    plan = await plan_disk_wipe(connector, target, operator, params)
    if plan.refusal is not None:
        status, guidance = plan.refusal
        return _wipe_answer(plan, status, guidance=guidance)
    if not plan.present:
        return _wipe_answer(
            plan, STATUS_UNCHANGED, guidance="the disks have no partitions; nothing was wiped"
        )
    if plan.object["method"] == "secure":
        return await _wipe_secure(connector, target, operator, plan)
    return await _wipe_partitions(connector, target, operator, plan)


async def vsan_disk_wipe_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of ``vsan.disk.wipe``: the handler's own plan."""
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    plan = await plan_disk_wipe(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        dict(ctx.params),
    )
    preview = blast_radius(plan)
    preview["blast_radius"]["effect"] = _WIPE_EFFECT
    return preview
