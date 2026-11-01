# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads of the vSAN disk changes (:mod:`._vsan_disk_claim`, :mod:`._vsan_disk_wipe`).

The host read, ``HostVsanSystem.QueryDisksForVsan`` by disk name, one flat
row per disk, and the shape check of the host and disk inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    HOST_MO_TYPE,
    query_disks_for_vsan,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import HOST_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import fault_message, fault_type_name

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_QUERY_DISKS_FOR_VSAN",
    "OP_QUERY_DISKS_FOR_VSAN_9",
    "PROP_STORAGE_SYSTEM",
    "PROP_VSAN_SYSTEM",
    "STATE_ELIGIBLE",
    "STATE_IN_USE",
    "disk_results_by_name",
    "disk_row",
    "exception_text",
    "host_problem",
    "read_host",
]

#: ``HostVsanSystem.QueryDisksForVsan``: the 8.0 VI-JSON path (the vendor
#: files of 8.0 U1 to U3) and the 9.x path (``/vsan/...``). Both are reads.
OP_QUERY_DISKS_FOR_VSAN: Final = "POST:/HostVsanSystem/{moId}/QueryDisksForVsan"
OP_QUERY_DISKS_FOR_VSAN_9: Final = "POST:/vsan/HostVsanSystem/{moId}/QueryDisksForVsan"

PROP_VSAN_SYSTEM: Final = "configManager.vsanSystem"
PROP_STORAGE_SYSTEM: Final = "configManager.storageSystem"

#: ``VsanHostDiskResult.state`` values (``VsanHostDiskResultState``).
STATE_IN_USE: Final = "inUse"
STATE_ELIGIBLE: Final = "eligible"


def _capacity_bytes(disk: dict[str, Any]) -> int | None:
    """``HostScsiDisk.capacity`` (block size times blocks) in bytes."""
    capacity = disk.get("capacity")
    if not isinstance(capacity, dict):
        return None
    block, size = capacity.get("block"), capacity.get("blockSize")
    if isinstance(block, int) and isinstance(size, int):
        return block * size
    return None


def disk_row(result: dict[str, Any]) -> dict[str, Any]:
    """One ``VsanHostDiskResult`` as a flat row."""
    raw = result.get("disk")
    disk: dict[str, Any] = raw if isinstance(raw, dict) else {}
    error = result.get("error")
    return {
        "name": disk.get("canonicalName"),
        "display_name": disk.get("displayName"),
        "device_path": disk.get("devicePath"),
        "ssd": disk.get("ssd"),
        "capacity_bytes": _capacity_bytes(disk),
        "vsan_state": result.get("state"),
        "vsan_uuid": result.get("vsanUuid"),
        "vsan_error": fault_message(error) if isinstance(error, dict) else None,
    }


async def read_host(
    connector: VmwareRestConnector, target: Any, operator: Operator, host: str
) -> dict[str, Any] | None:
    return await read_object(
        connector,
        target,
        operator,
        mo_type=HOST_MO_TYPE,
        moid=host,
        props=["name", PROP_VSAN_SYSTEM, PROP_STORAGE_SYSTEM],
    )


async def disk_results_by_name(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vsan_system: str,
    disks: list[str],
) -> dict[str, dict[str, Any]]:
    """``{canonical name: VsanHostDiskResult}`` for the named disks."""
    results = await query_disks_for_vsan(
        connector, target, operator, vsan_system=vsan_system, disks=disks
    )
    by_name: dict[str, dict[str, Any]] = {}
    for result in results:
        disk = result.get("disk")
        name = disk.get("canonicalName") if isinstance(disk, dict) else None
        if isinstance(name, str):
            by_name[name] = result
    return by_name


def host_problem(params: dict[str, Any], *disk_fields: str) -> str | None:
    """Why the host or the disk names cannot be used, or ``None``."""
    problem = moid_problem(params.get("host"), HOST_MOID_PATTERN, "host")
    if problem is not None:
        return problem
    names: list[str] = []
    for name in disk_fields:
        value = params.get(name) or []
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            return f"{name} must be a list of disk canonical names"
        names.extend(value)
    if not names:
        return "name at least one disk"
    if len(names) != len(set(names)):
        return "a disk is named twice"
    return None


def exception_text(exc: Exception) -> str:
    """The vendor's fault type and text of a failed call, else the error text."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            text = fault_message(body)
            kind = fault_type_name(body)
            if text:
                return text if kind is None or text.startswith(kind) else f"{kind}: {text}"
    return str(exc) or type(exc).__name__
