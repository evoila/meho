# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vsan.disk.claim`` -- claim disks of one host for vSAN.

Adds disks to vSAN as an ESA storage pool
(``VimClusterVsanVcDiskManagementSystem.VsanAddStoragePoolDisk``) or as an OSA
disk group (``...InitializeDiskMappings``). It first reads the disks with
``HostVsanSystem.QueryDisksForVsan`` and sends nothing unless every disk is
``eligible``. The task is followed to its end, then the disks are read again.
The OSA spec carries the ``HostScsiDisk`` objects of the eligible disks, as
the vendor sample ``vsanapisamples.py`` of the vSAN Management SDK builds it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._vsan_config_common import (
    HOST_MO_TYPE,
    VSAN_ADD_STORAGE_POOL_DISK,
    VSAN_INITIALIZE_DISK_MAPPINGS,
    send_vsan_change,
    vsan_op_id,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_disk_reads import (
    OP_QUERY_DISKS_FOR_VSAN_9,
    PROP_VSAN_SYSTEM,
    STATE_ELIGIBLE,
    STATE_IN_USE,
    disk_results_by_name,
    disk_row,
    host_problem,
    read_host,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest._mount import VsanMethod
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["vsan_disk_claim_composite", "vsan_disk_claim_preview"]

#: Child-op list: the reads, then the two vSAN claim methods.
_VSAN_SUB_OPS_DISK_CLAIM: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_QUERY_DISKS_FOR_VSAN_9,
    vsan_op_id(VSAN_ADD_STORAGE_POOL_DISK),
    vsan_op_id(VSAN_INITIALIZE_DISK_MAPPINGS),
)

#: ``StoragePoolDiskType`` of an ESA storage pool disk.
_SINGLE_TIER: Final = "singleTier"

#: Wall-clock bound of the task poll -- the 600 s convention; tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_CLAIMED: Final = "claimed"
STATUS_UNCHANGED: Final = "unchanged"
STATUS_NOT_VERIFIED: Final = "not_verified"
STATUS_INVALID_REQUEST: Final = "invalid_request"
STATUS_PRECONDITION_FAILED: Final = "precondition_failed"
STATUS_TIMEOUT: Final = "timeout"


def _claim_names(params: dict[str, Any]) -> list[str]:
    if params.get("architecture") == "osa":
        return [*(params.get("cache_disks") or []), *(params.get("capacity_disks") or [])]
    return list(params.get("disks") or [])


def _claim_answer(
    status: str,
    params: dict[str, Any],
    *,
    host_name: str | None = None,
    disks: list[dict[str, Any]] | None = None,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "host": params.get("host"),
        "host_name": host_name,
        "architecture": params.get("architecture"),
        "disks": disks or [],
        "task": task,
        "task_state": task_state,
        "guidance": guidance,
    }


def _claim_body(
    params: dict[str, Any], by_name: dict[str, dict[str, Any]]
) -> tuple[VsanMethod, dict[str, Any]]:
    """``(vSAN method, body)`` of the claim, in the vendor field order."""
    host = vim_moref(HOST_MO_TYPE, params["host"])
    if params.get("architecture") == "osa":
        spec = {
            VIM_TYPE_NAME_KEY: "VimVsanHostDiskMappingCreationSpec",
            "host": host,
            "cacheDisks": [by_name[name]["disk"] for name in params["cache_disks"]],
            "capacityDisks": [by_name[name]["disk"] for name in params["capacity_disks"]],
            "creationType": params.get("osa_type") or "allFlash",
        }
        return VSAN_INITIALIZE_DISK_MAPPINGS, {"spec": spec}
    pool_spec = {
        VIM_TYPE_NAME_KEY: "VsanAddStoragePoolDiskSpec",
        "host": host,
        "disks": [
            {VIM_TYPE_NAME_KEY: "VsanStoragePoolDisk", "diskName": name, "diskType": _SINGLE_TIER}
            for name in params["disks"]
        ],
    }
    return VSAN_ADD_STORAGE_POOL_DISK, {"specs": [pool_spec]}


def _claim_precheck(names: list[str], by_name: dict[str, dict[str, Any]]) -> tuple[str, str] | None:
    """``(status, guidance)`` when the claim must not be sent, else ``None``."""
    missing = [name for name in names if name not in by_name]
    if missing:
        return (
            STATUS_PRECONDITION_FAILED,
            f"the host has no disk named {', '.join(missing)}. Nothing was sent.",
        )
    states = {name: by_name[name].get("state") for name in names}
    if all(state == STATE_IN_USE for state in states.values()):
        return STATUS_UNCHANGED, "vSAN already uses every named disk. Nothing was sent."
    not_eligible = {name: state for name, state in states.items() if state != STATE_ELIGIBLE}
    if not_eligible:
        listed = ", ".join(f"{name} ({state})" for name, state in not_eligible.items())
        return (
            STATUS_PRECONDITION_FAILED,
            f"these disks cannot be claimed: {listed}. Nothing was sent. A disk that is "
            "ineligible often has old partitions: wipe it with vmware.composite.vsan.disk.wipe.",
        )
    return None


async def _claim_result(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    params: dict[str, Any],
    host_name: str | None,
    vsan_system: str,
    outcome: VimTaskResult,
) -> dict[str, Any]:
    """Map the task outcome; on success read the disks again."""
    names = _claim_names(params)
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vsan.disk.claim on host {params['host']!r} failed: "
            f"{outcome.fault_type or 'fault'}: {outcome.error_message or '<no fault text>'}"
        )
    if outcome.timed_out:
        timed_out = _claim_answer(
            STATUS_TIMEOUT,
            params,
            host_name=host_name,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"the claim task {outcome.task} did not end within "
                f"{int(_TASK_TIMEOUT_SECONDS)} seconds; it may still end. Follow it with "
                "vmware.tasks.recent, then call this operation again: it sends nothing when "
                "vSAN already uses the disks."
            ),
        )
        timed_out["issues"] = [_task_timeout_issue("vSAN disk claim task", outcome.task)]
        return timed_out
    by_name = await disk_results_by_name(
        connector, target, operator, vsan_system=vsan_system, disks=names
    )
    rows = [disk_row(by_name[name]) for name in names if name in by_name]
    claimed = [row for row in rows if row["vsan_state"] == STATE_IN_USE]
    if len(claimed) == len(names):
        return _claim_answer(
            STATUS_CLAIMED,
            params,
            host_name=host_name,
            disks=rows,
            task=outcome.task,
            task_state=outcome.state,
        )
    return _claim_answer(
        STATUS_NOT_VERIFIED,
        params,
        host_name=host_name,
        disks=rows,
        task=outcome.task,
        task_state=outcome.state,
        guidance=(
            "the claim task ended well, but not every disk reads as used by vSAN yet. Read "
            "the disks again in a minute."
        ),
    )


async def vsan_disk_claim_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Claim disks of one host for vSAN: an ESA storage pool or an OSA disk group.

    Op-id: ``vmware.composite.vsan.disk.claim``. Reads the host and the disks
    first: an unknown host is ``invalid_request``; an unknown or not eligible
    disk is ``precondition_failed``; disks vSAN already uses are
    ``unchanged``. Otherwise one gated vSAN call, the task followed to its
    end, and the disks read again.
    """
    problem = host_problem(
        params,
        *(("cache_disks", "capacity_disks") if params.get("architecture") == "osa" else ("disks",)),
    )
    if problem is not None:
        return _claim_answer(STATUS_INVALID_REQUEST, params, guidance=problem)
    host = params["host"]
    props = await read_host(connector, target, operator, host)
    if props is None:
        guidance = f"no host {host!r}; pass a host moid. Nothing was sent."
        return _claim_answer(STATUS_INVALID_REQUEST, params, guidance=guidance)
    host_name = props.get("name") if isinstance(props.get("name"), str) else None
    vsan_system = _moref_value(props.get(PROP_VSAN_SYSTEM))
    if vsan_system is None:
        guidance = f"host {host!r} has no vSAN system. Nothing was sent."
        return _claim_answer(
            STATUS_PRECONDITION_FAILED, params, host_name=host_name, guidance=guidance
        )
    names = _claim_names(params)
    by_name = await disk_results_by_name(
        connector, target, operator, vsan_system=vsan_system, disks=names
    )
    rows = [disk_row(by_name[name]) for name in names if name in by_name]
    refusal = _claim_precheck(names, by_name)
    if refusal is not None:
        return _claim_answer(
            refusal[0], params, host_name=host_name, disks=rows, guidance=refusal[1]
        )
    method, body = _claim_body(params, by_name)
    gate, answer = await send_vsan_change(
        connector,
        target,
        operator,
        method=method,
        body=body,
        params={
            "host": host,
            "host_name": host_name,
            "architecture": params.get("architecture"),
            "disks": names,
        },
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
    return await _claim_result(
        connector,
        target,
        operator,
        params=params,
        host_name=host_name,
        vsan_system=vsan_system,
        outcome=outcome,
    )


async def vsan_disk_claim_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vsan.disk.claim``: the host, the architecture and the disks (no I/O)."""
    host = ctx.params.get("host")
    if not isinstance(host, str):
        return None
    return {
        "action": "claim_vsan_disks",
        "host": host,
        "architecture": ctx.params.get("architecture"),
        "disks": _claim_names(ctx.params),
    }
