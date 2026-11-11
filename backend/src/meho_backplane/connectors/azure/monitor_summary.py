# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Body of ``azure.vm.utilization.summary``: CPU and memory use of many VMs.

The body behind a thin method of
:class:`~meho_backplane.connectors.azure.monitor.MonitorOps`. The operation
table and the guidance for the AI are in :mod:`.monitor`; the shared calls are
in :mod:`.monitor_calls` and the numbers in :mod:`.monitor_rows`.

One paged VM list, one size list per region, and one batch call per 50 VMs of a
region. Nothing is read per VM. A missing hour is unknown, never zero. When
Azure refuses one batch call for a reason other than missing access, the VMs of
that batch come back as unknown with the reason, and the other batches still
count.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure import monitor_calls
from meho_backplane.connectors.azure._errors import AzureApiError, AzureForbiddenError
from meho_backplane.connectors.azure.compute_reads import vm_list_impl
from meho_backplane.connectors.azure.monitor_calls import (
    BATCH_SIZE,
    INTERVAL,
    MAX_VMS,
    REGION,
    Ctx,
    days_param,
    floor_hour,
    memory_by_size,
    read_batch,
    require_public_cloud,
    vm_ref,
)
from meho_backplane.connectors.azure.monitor_rows import (
    MIN_COVERAGE_PERCENT,
    HourlyMetrics,
    StatBlock,
    cpu_block,
    failed_text,
    memory_block,
    parse_metrics,
    time_text,
    vm_coverage_reason,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["METHOD_TEXT", "vm_utilization_summary_impl"]

METHOD_TEXT: Final[str] = (
    "Hourly Azure Monitor metrics (Percentage CPU, Available Memory Bytes) over whole "
    "hours that end at the last full hour. CPU: the mean and the nearest-rank p95 of the "
    "hourly averages, and the highest hourly maximum. Memory used % = 100 x (1 - free "
    "memory / memory of the VM size): average and p95 from the hourly average of free "
    "memory, maximum from the hourly lowest free memory. An hour without a sample is "
    "unknown and left out, never counted as zero."
)

_NO_MEMORY_SIZE: Final[str] = (
    "The memory size of this VM size is unknown, so memory use is not worked out."
)


def _stat_dict(block: StatBlock) -> dict[str, Any]:
    return {
        "average_percent": block.average,
        "p95_percent": block.p95,
        "maximum_percent": block.maximum,
        "samples_found": block.samples_found,
        "coverage_percent": block.coverage_percent,
    }


def _memory_reason(
    block: StatBlock, memory_gb: float | None, note: str | None, hours: int
) -> str | None:
    if block.sufficient:
        return None
    if memory_gb is None:
        return note or _NO_MEMORY_SIZE
    if block.samples_found == 0:
        return "Azure has no free-memory samples for this VM."
    return (
        f"Only {block.samples_found} of {hours} hourly memory samples were found "
        f"({block.coverage_percent:g}%); at least {MIN_COVERAGE_PERCENT:g}% is needed."
    )


def _summary_row(
    vm: dict[str, Any],
    parsed: HourlyMetrics | None,
    hours: int,
    memory: tuple[float | None, str | None],
    problem: str | None,
) -> dict[str, Any]:
    """One VM of the summary. *problem* is why nothing could be read, or ``None``.

    *memory* is the memory size of the VM's size, and a note for when it is unknown.
    """
    memory_gb, memory_note = memory
    data = parsed if parsed is not None else HourlyMetrics()
    cpu = cpu_block(data.cpu, hours)
    mem = memory_block(data.memory, memory_gb, hours)
    reason = problem or failed_text(data)
    if reason is None:
        reason = vm_coverage_reason(cpu, hours, vm.get("power_state"), missing_data=parsed is None)
    return {
        "vm_id": vm.get("id"),
        "name": vm.get("name"),
        "resource_group": vm.get("resource_group"),
        "location": vm.get("location"),
        "size": vm.get("size"),
        "power_state": vm.get("power_state"),
        "cpu": _stat_dict(cpu),
        "memory": {
            "memory_gb": memory_gb,
            **_stat_dict(mem),
            "sufficient": mem.sufficient,
            "reason": _memory_reason(mem, memory_gb, memory_note, hours),
        },
        "samples_expected": hours,
        "samples_found": cpu.samples_found,
        "coverage_percent": cpu.coverage_percent,
        "sufficient": problem is None and cpu.sufficient,
        "reason": reason,
    }


def _select_vms(
    listing: dict[str, Any], vm_ids: list[str] | None, subscription_id: str
) -> list[dict[str, Any]]:
    """The VMs to read, sorted by ID. A VM ID not in the list becomes a row with ``missing``."""
    rows: list[dict[str, Any]] = listing["value"]
    if vm_ids is None:
        return sorted(rows, key=lambda row: str(row.get("id")).lower())
    by_id = {str(row.get("id")).lower(): row for row in rows}
    selected: dict[str, dict[str, Any]] = {}
    for position, vm_id in enumerate(vm_ids):
        group, name = vm_ref(vm_id, subscription_id, f"vm_ids[{position}]")
        selected.setdefault(
            vm_id.lower(),
            by_id.get(vm_id.lower())
            or {
                "id": vm_id,
                "name": name,
                "resource_group": group,
                "missing": True,
            },
        )
    return [selected[key] for key in sorted(selected)]


def _unreadable(vm: dict[str, Any], region: str, list_was_cut: bool) -> str | None:
    """Why a VM cannot be read at all, or ``None``."""
    if vm.get("missing"):
        return (
            "No VM with this ID was found in the VM list, which was cut at a limit."
            if list_was_cut
            else "No VM with this ID exists in the subscription."
        )
    if not REGION.fullmatch(region):
        return "The VM has no usable region name, so its metrics were not read."
    return None


def _batch_problem(exc: Exception) -> str:
    status = getattr(exc, "http_status", None)
    how = f"HTTP {status}" if status else "the connection failed"
    return (
        f"Azure did not answer the metrics read for this batch ({how}). "
        "The VM is unknown, not idle."
    )


async def _read_region(
    ctx: Ctx,
    subscription_id: str,
    region: str,
    vms: list[dict[str, Any]],
    window: tuple[datetime, datetime],
    hours: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """The rows of the VMs of one region, and the batches read and failed."""
    sizes, memory_note = await memory_by_size(ctx, region)
    rows: list[dict[str, Any]] = []
    batches = failed = 0
    for offset in range(0, len(vms), BATCH_SIZE):
        chunk = vms[offset : offset + BATCH_SIZE]
        batches += 1
        problem: str | None = None
        answered: dict[str, list[Any]] = {}
        try:
            answered = await read_batch(
                ctx, subscription_id, region, [str(vm["id"]) for vm in chunk], window
            )
        except AzureForbiddenError:
            raise
        except (AzureApiError, httpx.TransportError) as exc:
            failed += 1
            problem = _batch_problem(exc)
        for vm in chunk:
            metrics = answered.get(str(vm["id"]).lower())
            parsed = parse_metrics(metrics, window[0], hours) if metrics is not None else None
            memory = (monitor_calls.memory_gb(sizes, vm.get("size")), memory_note)
            rows.append(_summary_row(vm, parsed, hours, memory, problem))
    return rows, batches, failed


def _omission(listing: dict[str, Any], capped: bool) -> tuple[str | None, str | None]:
    """The message and the kind of what was left out, from the VM list and the VM limit."""
    messages = [listing["omission"]] if listing["omission"] else []
    kinds = [listing["omission_kind"]] if listing["omission_kind"] else []
    if capped:
        messages.append(
            f"MEHO read the first {MAX_VMS} VMs only. Ask again with a resource group or "
            "with vm_ids to read the others."
        )
        kinds.append("vm_limit")
    return (" ".join(messages) or None), (kinds[0] if kinds else None)


async def vm_utilization_summary_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vm.utilization.summary``: CPU and memory use of many VMs over a window."""
    config = resolve_target_config(target)
    require_public_cloud(target, config)
    ctx = Ctx(connector, operator, target)
    days = days_param(params)
    vm_ids, group = params.get("vm_ids"), params.get("resource_group")
    if vm_ids is not None and group is not None:
        raise ValueError("Give vm_ids or resource_group, not both.")
    if vm_ids is not None and (not isinstance(vm_ids, list) or len(vm_ids) > MAX_VMS):
        raise ValueError(f"Parameter 'vm_ids' must be a list of at most {MAX_VMS} VM IDs.")
    for position, vm_id in enumerate(vm_ids or []):
        vm_ref(vm_id, config.subscription_id, f"vm_ids[{position}]")
    hours = days * 24
    end = floor_hour(monitor_calls.utcnow())
    window = (end - timedelta(hours=hours), end)

    listing = await vm_list_impl(
        connector, operator, target, {} if group is None else {"resource_group": group}
    )
    selected = _select_vms(listing, vm_ids, config.subscription_id)
    vms = selected[:MAX_VMS]
    rows: dict[str, dict[str, Any]] = {}
    by_region: dict[str, list[dict[str, Any]]] = {}
    for vm in vms:
        region = vm["location"].lower() if isinstance(vm.get("location"), str) else ""
        problem = _unreadable(vm, region, bool(listing["truncated"]))
        if problem is None:
            by_region.setdefault(region, []).append(vm)
        else:
            rows[str(vm["id"]).lower()] = _summary_row(vm, None, hours, (None, None), problem)

    batches = failed_batches = 0
    for region in sorted(by_region):
        region_rows, count, failed = await _read_region(
            ctx, config.subscription_id, region, by_region[region], window, hours
        )
        batches, failed_batches = batches + count, failed_batches + failed
        rows.update((str(row["vm_id"]).lower(), row) for row in region_rows)

    ordered = [rows[str(vm["id"]).lower()] for vm in vms]
    omission, omission_kind = _omission(listing, len(selected) > MAX_VMS)
    return {
        "value": ordered,
        "truncated": bool(listing["truncated"]) or len(selected) > MAX_VMS,
        "omission": omission,
        "omission_kind": omission_kind,
        "pages_read": listing["pages_read"],
        "window_from": time_text(window[0]),
        "window_to": time_text(window[1]),
        "window_days": days,
        "interval": INTERVAL,
        "samples_expected": hours,
        "method": METHOD_TEXT,
        "vms_total": len(ordered),
        "vms_sufficient": sum(1 for row in ordered if row["sufficient"]),
        "batches": batches,
        "batches_failed": failed_batches,
    }
