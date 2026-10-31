# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create or change an organization quota in a region (provider plane).

``vcfa.provider.org_quota.set`` works on a quota (a "virtual datacenter" in
the API) in three steps:

1. ``quota`` -- the quota itself: its Supervisors and the CPU and memory
   limits and reservations per zone (``POST`` a new one, ``PUT`` a change;
   both answer ``202`` and start a vendor job).
2. ``storage_policies`` -- a storage limit per storage policy (``PUT`` a
   changed limit; ``POST`` new ones as one ``values`` list, the only shape
   the vendor accepts).
3. ``vm_classes`` -- the VM classes the quota offers (``PUT`` the whole
   list as ``values``).

A new quota is a job; its storage and VM classes need the quota's id, so
they run on the next call with the same inputs, once the quota reads
READY. A quota without storage or VM classes gets a warning: namespaces in
it fail later.

Before a start, the op reads the quota: while its job still runs
(``NOT_READY``) it starts nothing and says ``already_running``. The park
preview stores a hash of the quota as read; the approved run reads it again
and answers ``precondition_failed`` when it changed. A step that fails after
an earlier step ran returns the steps that ran, the one that failed (with the
vendor's error) and the ones that did not run; nothing is undone.

Wire facts follow ``go-vcloud-director`` v3 (``govcd/tm_region_quota.go``,
``tm_region_quota_storage_policy.go``; ``TmVdc`` in ``types/v56/tm.go``) and
the VCF Automation 9.0, 9.1 and 9.1.1 provider API references.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._infra_common import (
    Call,
    InvalidRequestError,
    StepLog,
    approved_state_problem,
    ids_of,
    job_from_response,
    match_row,
    now_iso,
    ref,
    state_hash,
)
from meho_backplane.connectors.vcf_automation._infra_connect import StepFailedError, run_step
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_REGION_STORAGE_POLICIES_PATH,
    INFRA_SUPERVISORS_PATH,
    INFRA_VDC_PATH,
    INFRA_VDC_STORAGE_POLICIES_PATH,
    INFRA_VDC_STORAGE_POLICY_PATH,
    INFRA_VDC_VM_CLASSES_PATH,
    INFRA_VDCS_PATH,
    INFRA_VM_CLASSES_PATH,
    INFRA_ZONES_PATH,
    provider_vcf_path,
    safe_segment,
)
from meho_backplane.connectors.vcf_automation._infra_reads import find_region, list_vcf
from meho_backplane.connectors.vcf_automation._lookups import find_org, list_all
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["ORG_QUOTA_SET_OP_ID", "provider_org_quota_set"]

ORG_QUOTA_SET_OP_ID: Final = "vcfa.provider.org_quota.set"
_STATUS_READ: Final = "vcfa.provider.org_quota.list"
_NOT_READY: Final = "NOT_READY"
_STEPS: Final = ("quota", "storage_policies", "vm_classes")


def _answer(
    status: str,
    log: StepLog,
    *,
    obj: dict[str, Any] | None,
    guidance: str,
    job: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "object": obj,
        "job": job,
        "steps": log.as_list(),
        "warnings": warnings or [],
        "checked_at": now_iso(),
        "guidance": guidance,
    }


def _alloc_key(allocs: Any) -> list[tuple[Any, ...]]:
    """The zone allocations as sorted tuples, to compare two quotas."""
    out = []
    for alloc in allocs or []:
        res = alloc.get("resourceAllocation") or {}
        out.append(
            (
                str((alloc.get("zone") or {}).get("id")),
                res.get("cpuLimitMHz"),
                res.get("cpuReservationMHz"),
                res.get("memoryLimitMiB"),
                res.get("memoryReservationMiB"),
            )
        )
    return sorted(out)


# ---------------------------------------------------------------------------
# Plan: resolve the names, read the quota, decide each step
# ---------------------------------------------------------------------------


async def _resolve(call: Call, params: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the names the caller gave into vendor references (or refuse)."""
    connector, target, operator = call.connector, call.target, call.operator
    org = await find_org(connector, target, operator, str(params["org"]))
    if org is None:
        raise InvalidRequestError(f"no organization named {params['org']!r}")
    region = await find_region(connector, target, operator, str(params["region"]))
    if region is None:
        raise InvalidRequestError(f"no region named {params['region']!r}")
    region_id = region.get("id")
    fiql = f"region.id=={region_id}"
    sup_rows = [
        row
        for row in await list_vcf(connector, target, operator, INFRA_SUPERVISORS_PATH)
        if (row.get("region") or {}).get("id") == region_id
    ]
    zone_rows = await list_vcf(connector, target, operator, INFRA_ZONES_PATH, fiql=fiql)
    resolved: dict[str, Any] = {
        "org": ref(org),
        "region": ref(region),
        "supervisors": [
            ref(
                match_row(sup_rows, str(s), "Supervisor in this region", "supervisorId"),
                id_field="supervisorId",
            )
            for s in params["supervisors"]
        ],
        "zones": [
            {
                "zone": ref(match_row(zone_rows, str(z["zone"]), "zone in this region")),
                "resourceAllocation": {
                    "cpuLimitMHz": z["cpu_limit_mhz"],
                    "cpuReservationMHz": z["cpu_reservation_mhz"],
                    "memoryLimitMiB": z["memory_limit_mib"],
                    "memoryReservationMiB": z["memory_reservation_mib"],
                },
            }
            for z in params["zones"]
        ],
        "storage": None,
        "vm_classes": None,
    }
    if params.get("storage_policies"):
        rows = await list_vcf(
            connector, target, operator, INFRA_REGION_STORAGE_POLICIES_PATH, fiql=fiql
        )
        resolved["storage"] = [
            (
                ref(match_row(rows, str(sp["name"]), "storage policy in this region")),
                sp["storage_limit_mib"],
            )
            for sp in params["storage_policies"]
        ]
    if params.get("vm_classes"):
        rows = await list_vcf(connector, target, operator, INFRA_VM_CLASSES_PATH, fiql=fiql)
        resolved["vm_classes"] = [
            ref(match_row(rows, str(name), "VM class in this region"))
            for name in params["vm_classes"]
        ]
    return resolved


async def _read_quota(
    call: Call, name: str, org_id: Any
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """The quota as read, with its storage limits and VM classes (empty when new)."""
    connector, target, operator = call.connector, call.target, call.operator
    vdc = next(
        (
            row
            for row in await list_vcf(connector, target, operator, INFRA_VDCS_PATH)
            if (row.get("org") or {}).get("id") == org_id
            and str(row.get("name") or "").casefold() == name.casefold()
        ),
        None,
    )
    if vdc is None or vdc.get("status") == _NOT_READY:
        return vdc, [], []
    vdc_id = vdc.get("id")
    storage = [
        row
        for row in await list_vcf(
            connector,
            target,
            operator,
            INFRA_VDC_STORAGE_POLICIES_PATH,
            fiql=f"virtualDatacenter.id=={vdc_id}",
        )
        if (row.get("virtualDatacenter") or {}).get("id") == vdc_id
    ]
    path = INFRA_VDC_VM_CLASSES_PATH.format(vdcUrn=safe_segment(vdc_id, "quota id"))
    classes = await list_all(connector, target, operator, provider_vcf_path(target, path))
    return vdc, storage, classes


def _decide(
    resolved: Mapping[str, Any],
    vdc: Mapping[str, Any] | None,
    storage: list[dict[str, Any]],
    classes: list[dict[str, Any]],
) -> dict[str, str]:
    """What each step would do: create, update, set, replace, unchanged, after_ready."""
    if vdc is None:
        return {"quota": "create", "storage_policies": "after_ready", "vm_classes": "after_ready"}
    if vdc.get("status") == _NOT_READY:
        return dict.fromkeys(_STEPS, "already_running")
    same_quota = ids_of(vdc.get("supervisors")) == ids_of(resolved["supervisors"]) and (
        _alloc_key(vdc.get("zoneResourceAllocation")) == _alloc_key(resolved["zones"])
    )
    steps = {"quota": "unchanged" if same_quota else "update"}
    steps["storage_policies"] = "unchanged"
    if resolved["storage"] is not None:
        limits = {
            (row.get("regionStoragePolicy") or {}).get("id"): row.get("storageLimitMiB")
            for row in storage
        }
        if any(limits.get(rsp["id"], -1) != limit for rsp, limit in resolved["storage"]):
            steps["storage_policies"] = "set"
    steps["vm_classes"] = "unchanged"
    if resolved["vm_classes"] is not None and ids_of(classes) != ids_of(resolved["vm_classes"]):
        steps["vm_classes"] = "replace"
    return steps


@dataclass
class _Plan:
    resolved: dict[str, Any]
    vdc: dict[str, Any] | None
    storage: list[dict[str, Any]]
    steps: dict[str, str]
    current: dict[str, Any] | None
    state_hash: str


async def _plan(call: Call, params: Mapping[str, Any]) -> _Plan:
    resolved = await _resolve(call, params)
    vdc, storage, classes = await _read_quota(call, str(params["name"]), resolved["org"]["id"])
    steps = _decide(resolved, vdc, storage, classes)
    current = None
    if vdc is not None:
        current = {
            "id": vdc.get("id"),
            "status": vdc.get("status"),
            "supervisors": ids_of(vdc.get("supervisors")),
            "zones": _alloc_key(vdc.get("zoneResourceAllocation")),
            "storage": sorted((str(row.get("id")), row.get("storageLimitMiB")) for row in storage),
            "vm_classes": ids_of(classes),
        }
    digest = state_hash({"steps": steps, "quota": current})
    return _Plan(resolved, vdc, storage, steps, current, digest)


def _warnings(params: Mapping[str, Any], vdc: Mapping[str, Any] | None) -> list[str]:
    """The warnings of a new quota without storage or VM classes."""
    if vdc is not None:
        return []
    warnings = []
    if not params.get("storage_policies"):
        warnings.append(
            "no storage policy given: namespaces of this quota will fail to start until the "
            "quota has one"
        )
    if not params.get("vm_classes"):
        warnings.append(
            "no VM classes given: VMs in namespaces of this quota will fail until the quota "
            "offers VM classes"
        )
    return warnings


# ---------------------------------------------------------------------------
# Run the steps
# ---------------------------------------------------------------------------


@dataclass
class _QuotaRun:
    """One run of the steps, with the log of what happened."""

    call: Call
    params: Mapping[str, Any]
    plan: _Plan
    log: StepLog

    async def send(self, step: str, verb: str, path: str, body: Mapping[str, Any]) -> None:
        """Send one call of *step* and log it as started (``202``) or done."""
        call = self.call
        resp = await run_step(
            self.log,
            step,
            lambda: call.connector._send_with_retry(
                call.target,
                verb,
                provider_vcf_path(call.target, path),
                operator=call.operator,
                json=body,
            ),
        )
        if resp.status_code == 202:
            job = job_from_response(resp, status_read=_STATUS_READ)
            self.log.mark(step, "started", "VCF Automation accepted the job", job_id=job["id"])
        else:
            self.log.mark(step, "done", f"HTTP {resp.status_code}")

    async def storage(self) -> None:
        vdc = self.plan.vdc or {}
        by_rsp = {
            (row.get("regionStoragePolicy") or {}).get("id"): row for row in self.plan.storage
        }
        new: list[dict[str, Any]] = []
        for rsp, limit in self.plan.resolved["storage"]:
            row = by_rsp.get(rsp["id"])
            if row is None:
                new.append(
                    {
                        "virtualDatacenter": ref(vdc),
                        "regionStoragePolicy": rsp,
                        "storageLimitMiB": limit,
                    }
                )
            elif row.get("storageLimitMiB") != limit:
                urn = safe_segment(row.get("id"), "storage policy id")
                path = INFRA_VDC_STORAGE_POLICY_PATH.format(vdcStoragePolicyUrn=urn)
                await self.send("storage_policies", "PUT", path, {**row, "storageLimitMiB": limit})
        if new:
            # The vendor refuses a storage request without the "values" wrapper.
            await self.send(
                "storage_policies", "POST", INFRA_VDC_STORAGE_POLICIES_PATH, {"values": new}
            )

    async def run(self) -> None:
        steps, resolved, vdc = self.plan.steps, self.plan.resolved, self.plan.vdc or {}
        quota_body = {
            "supervisors": resolved["supervisors"],
            "zoneResourceAllocation": resolved["zones"],
        }
        if steps["quota"] == "create":
            body = {
                "name": self.params["name"],
                "description": self.params.get("description") or "",
                "org": resolved["org"],
                "region": resolved["region"],
                **quota_body,
            }
            await self.send("quota", "POST", INFRA_VDCS_PATH, body)
            for step in ("storage_policies", "vm_classes"):
                self.log.mark(step, "skipped", "runs on the next call, once the quota reads READY")
            return
        vdc_urn = safe_segment(vdc.get("id"), "quota id")
        if steps["quota"] == "update":
            path = INFRA_VDC_PATH.format(vdcUrn=vdc_urn)
            await self.send("quota", "PUT", path, {**vdc, **quota_body})
        else:
            self.log.mark("quota", "skipped", "unchanged")
        if steps["storage_policies"] == "set":
            await self.storage()
        else:
            self.log.mark("storage_policies", "skipped", "unchanged")
        if steps["vm_classes"] == "replace":
            path = INFRA_VDC_VM_CLASSES_PATH.format(vdcUrn=vdc_urn)
            await self.send("vm_classes", "PUT", path, {"values": resolved["vm_classes"]})
        else:
            self.log.mark("vm_classes", "skipped", "unchanged")


def _final_status(log: StepLog) -> str:
    results = {row["result"] for row in log.as_list()}
    if "started" in results:
        return "accepted"
    return "changed" if "done" in results else "unchanged"


_PART_WAY: Final = (
    "A step failed after an earlier step ran. The steps that ran are not undone. Read "
    "vcfa.provider.org_quota.list, fix the cause, then call again; steps that are already "
    "done are skipped."
)
_RUNNING: Final = (
    "A job on this quota still runs; read vcfa.provider.org_quota.list until its status is "
    "READY. Nothing was started."
)
_NEXT: Final = (
    "Read vcfa.provider.org_quota.list (with job_id) until the quota status is READY. A new "
    "quota gets its storage limits and VM classes on the next call with the same inputs."
)


async def provider_org_quota_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org_quota.set`` -- create or change one organization quota."""
    log = StepLog(_STEPS)
    try:
        plan = await _plan(Call(connector, target, operator), params)
    except InvalidRequestError as exc:
        guidance = (
            f"{exc}; nothing was written. Read vcfa.provider.region.get for the zones, storage "
            "policies and VM classes of the region"
        )
        return _answer("invalid_request", log, obj=None, guidance=guidance)
    obj = {
        "kind": "org_quota",
        "name": params["name"],
        "id": (plan.vdc or {}).get("id"),
        "org": plan.resolved["org"],
        "region": plan.resolved["region"],
    }
    warnings = _warnings(params, plan.vdc)
    problem = approved_state_problem(ORG_QUOTA_SET_OP_ID, plan.state_hash)
    if problem is not None:
        return _answer("precondition_failed", log, obj=obj, guidance=problem)
    if plan.steps["quota"] == "already_running":
        for step in _STEPS:
            log.mark(step, "skipped", "a job on this quota still runs")
        return _answer("already_running", log, obj=obj, guidance=_RUNNING)
    try:
        await _QuotaRun(Call(connector, target, operator), params, plan, log).run()
    except StepFailedError:
        return _answer("failed_part_way", log, obj=obj, warnings=warnings, guidance=_PART_WAY)
    started = [row["job_id"] for row in log.as_list() if row["result"] == "started"]
    job = {"id": started[0], "status_read": _STATUS_READ} if started else None
    return _answer(_final_status(log), log, obj=obj, job=job, warnings=warnings, guidance=_NEXT)


async def _org_quota_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: what each step would do, the quota as read, a state hash."""
    base = {
        "action": "set_org_quota",
        "org": ctx.params.get("org"),
        "region": ctx.params.get("region"),
        "name": ctx.params.get("name"),
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return {**base, "would": "unknown"}
    try:
        plan = await _plan(Call(connector, ctx.target, ctx.operator), ctx.params)
    except InvalidRequestError as exc:
        return {**base, "would": "invalid_request", "reason": str(exc)}
    return {
        **base,
        "would": plan.steps,
        "current": plan.current,
        "warnings": _warnings(ctx.params, plan.vdc),
        "state_hash": plan.state_hash,
    }


register_preview_builder(ORG_QUOTA_SET_OP_ID, _org_quota_set_preview)
