# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers of the VCFA provider reads for regions and organization quotas.

* ``vcfa.provider.infra.list`` -- vCenters, NSX managers and Supervisors as
  one flat list of rows.
* ``vcfa.provider.region.get`` -- one region with its zones, storage
  policies and VM classes.
* ``vcfa.provider.org_quota.list`` -- one row per organization quota (a
  "virtual datacenter" in the API): CPU and memory per zone, storage limits
  and VM classes.

They are also the status reads of the changes in :mod:`._infra_writes`.
Each takes an optional ``job_id`` (the vendor job a change started) and then
also reads that job. When the system does not answer, they say
``state: not_reachable`` with the job id and the time, never ``failed``.
A login that expired is renewed by the connector (one new login on HTTP 401).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.vcf_automation._infra_common import (
    is_unreachable,
    now_iso,
    read_job,
    ref,
)
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_NSX_MANAGERS_PATH,
    INFRA_REGION_PATH,
    INFRA_REGION_STORAGE_POLICIES_PATH,
    INFRA_SUPERVISORS_PATH,
    INFRA_VCENTERS_PATH,
    INFRA_VDC_STORAGE_POLICIES_PATH,
    INFRA_VDC_VM_CLASSES_PATH,
    INFRA_VDCS_PATH,
    INFRA_VM_CLASSES_PATH,
    INFRA_ZONES_PATH,
    provider_vcf_path,
    safe_segment,
)
from meho_backplane.connectors.vcf_automation._lookups import fiql_safe, list_all
from meho_backplane.connectors.vcf_automation.typed_ops import provider_regions_path

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "find_region",
    "list_vcf",
    "provider_infra_list",
    "provider_org_quota_list",
    "provider_region_get",
    "quota_rows",
    "region_detail",
]

_REGION_URN_PREFIX = "urn:vcloud:region:"
_REGION_FIELDS = (
    "id",
    "name",
    "description",
    "status",
    "nsxManager",
    "supervisors",
    "storagePolicies",
    "cpuCapacityMHz",
    "cpuReservationCapacityMHz",
    "memoryCapacityMiB",
    "memoryReservationCapacityMiB",
    "loadBalancerType",
)


async def list_vcf(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    path: str,
    *,
    fiql: str | None = None,
) -> list[dict[str, Any]]:
    """Every row of a provider list, in the path form of *target*'s version."""
    params = {"filter": fiql} if fiql else None
    return await list_all(
        connector, target, operator, provider_vcf_path(target, path), params=params
    )


def _pick(row: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys}


async def _with_reachability(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
    read: Callable[[], Awaitable[dict[str, Any]]],
    empty: dict[str, Any],
) -> dict[str, Any]:
    """Run *read*, add the job when ``job_id`` is set, and map "no answer"."""
    job_id = params.get("job_id")
    try:
        answer = await read()
        job = await read_job(connector, target, operator, str(job_id)) if job_id else None
    except (httpx.HTTPError, RuntimeError) as exc:
        if not is_unreachable(exc):
            raise
        return {
            **empty,
            "state": "not_reachable",
            "reachable": False,
            "checked_at": now_iso(),
            "job": {"id": job_id, "state": "not_reachable"} if job_id else None,
            "guidance": (
                "VCF Automation did not answer (it may be restarting or upgrading). "
                "This is not a failure of the job. Read again in a few minutes with "
                "the same job_id."
            ),
        }
    return {"reachable": True, "checked_at": now_iso(), **answer, "job": job}


# ---------------------------------------------------------------------------
# vcfa.provider.infra.list
# ---------------------------------------------------------------------------


def _vcenter_row(vc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "vcenter",
        "id": vc.get("vcId"),
        "name": vc.get("name"),
        "url": vc.get("url"),
        "state": vc.get("listenerState"),
        "is_enabled": vc.get("isEnabled"),
        "is_connected": vc.get("isConnected"),
        "version": vc.get("vcVersion"),
        "region": None,
        "vcenter": None,
    }


def _nsx_row(nsx: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "nsx_manager",
        "id": nsx.get("id"),
        "name": nsx.get("name"),
        "url": nsx.get("url"),
        "state": nsx.get("status"),
        "is_enabled": nsx.get("active"),
        "is_connected": None,
        "version": nsx.get("version"),
        "region": None,
        "vcenter": None,
    }


def _supervisor_row(sup: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "supervisor",
        "id": sup.get("supervisorId"),
        "name": sup.get("name"),
        "url": None,
        "state": "in_region" if sup.get("region") else "free",
        "is_enabled": None,
        "is_connected": None,
        "version": None,
        "region": sup.get("region") or None,
        "vcenter": sup.get("virtualCenter") or None,
    }


async def provider_infra_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.infra.list`` -- vCenters, NSX managers and Supervisors.

    Only named fields are copied, so no password the vendor might echo
    reaches the answer.
    """

    async def _read() -> dict[str, Any]:
        vcenters = await list_vcf(connector, target, operator, INFRA_VCENTERS_PATH)
        nsx_managers = await list_vcf(connector, target, operator, INFRA_NSX_MANAGERS_PATH)
        supervisors = await list_vcf(connector, target, operator, INFRA_SUPERVISORS_PATH)
        items = [
            *(_vcenter_row(vc) for vc in vcenters),
            *(_nsx_row(nsx) for nsx in nsx_managers),
            *(_supervisor_row(sup) for sup in supervisors),
        ]
        return {"state": "ok", "items": items, "guidance": None}

    return await _with_reachability(connector, target, operator, params, _read, {"items": []})


# ---------------------------------------------------------------------------
# vcfa.provider.region.get
# ---------------------------------------------------------------------------


async def find_region(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    wanted: str,
) -> dict[str, Any] | None:
    """The region with the URN or name *wanted* (case-insensitive name), or ``None``."""
    if wanted.startswith(_REGION_URN_PREFIX):
        path = provider_vcf_path(
            target, INFRA_REGION_PATH.format(regionUrn=safe_segment(wanted, "region"))
        )
        try:
            return await connector._request_json(target, "GET", path, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
    params = {"filter": f"name=={wanted}"} if fiql_safe(wanted) else None
    rows = await list_all(connector, target, operator, provider_regions_path(target), params=params)
    needle = wanted.casefold()
    return next((row for row in rows if str(row.get("name", "")).casefold() == needle), None)


async def region_detail(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    region: Mapping[str, Any],
) -> dict[str, Any]:
    """The zones, storage policies and VM classes of *region*."""
    fiql = f"region.id=={region.get('id')}"
    zones = await list_vcf(connector, target, operator, INFRA_ZONES_PATH, fiql=fiql)
    policies = await list_vcf(
        connector, target, operator, INFRA_REGION_STORAGE_POLICIES_PATH, fiql=fiql
    )
    vm_classes = await list_vcf(connector, target, operator, INFRA_VM_CLASSES_PATH, fiql=fiql)
    return {
        "zones": [
            _pick(
                zone,
                (
                    "id",
                    "name",
                    "cpuLimitMHz",
                    "cpuReservationMHz",
                    "cpuUsedMHz",
                    "memoryLimitMiB",
                    "memoryReservationMiB",
                    "memoryUsedMiB",
                ),
            )
            for zone in zones
        ],
        "storage_policies": [
            _pick(policy, ("id", "name", "status", "storageCapacityMiB", "storageConsumedMiB"))
            for policy in policies
        ],
        "vm_classes": [
            _pick(vmc, ("id", "name", "cpuCount", "memoryMiB", "cpuReservationMHz", "reserved"))
            for vmc in vm_classes
        ],
    }


async def provider_region_get(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.region.get`` -- one region with its zones, policies and VM classes."""
    wanted = str(params["region"]).strip()
    empty: dict[str, Any] = {"region": None, "zones": [], "storage_policies": [], "vm_classes": []}

    async def _read() -> dict[str, Any]:
        region = await find_region(connector, target, operator, wanted)
        if region is None:
            return {
                **empty,
                "state": "not_found",
                "guidance": (
                    f"no region named {wanted!r}. A create that was just accepted shows up "
                    "after a moment; list the regions with vcfa.provider.region.list"
                ),
            }
        detail = await region_detail(connector, target, operator, region)
        return {
            "state": "ok",
            "region": _pick(region, _REGION_FIELDS),
            **detail,
            "guidance": None,
        }

    return await _with_reachability(connector, target, operator, params, _read, empty)


# ---------------------------------------------------------------------------
# vcfa.provider.org_quota.list
# ---------------------------------------------------------------------------


def _zone_rows(vdc: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for alloc in vdc.get("zoneResourceAllocation") or []:
        if not isinstance(alloc, dict):
            continue
        res = alloc.get("resourceAllocation") or {}
        rows.append(
            {
                "zone": alloc.get("zone"),
                "cpu_limit_mhz": res.get("cpuLimitMHz"),
                "cpu_reservation_mhz": res.get("cpuReservationMHz"),
                "memory_limit_mib": res.get("memoryLimitMiB"),
                "memory_reservation_mib": res.get("memoryReservationMiB"),
            }
        )
    return rows


def _quota_warnings(storage: list[Any], vm_classes: list[Any]) -> list[str]:
    warnings: list[str] = []
    if not storage:
        warnings.append(
            "no storage policy: namespaces of this quota will fail to start; add one "
            "with vcfa.provider.org_quota.set"
        )
    if not vm_classes:
        warnings.append(
            "no VM classes: VMs in namespaces of this quota will fail; add them with "
            "vcfa.provider.org_quota.set"
        )
    return warnings


async def quota_rows(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    vdcs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per quota in *vdcs*, with its storage limits and VM classes."""
    policies = await list_vcf(connector, target, operator, INFRA_VDC_STORAGE_POLICIES_PATH)
    rows: list[dict[str, Any]] = []
    for vdc in vdcs:
        vdc_id = vdc.get("id")
        storage = [
            {
                "id": policy.get("id"),
                "name": policy.get("name"),
                "region_storage_policy": policy.get("regionStoragePolicy"),
                "storage_limit_mib": policy.get("storageLimitMiB"),
                "storage_used_mib": policy.get("storageUsedMiB"),
            }
            for policy in policies
            if (policy.get("virtualDatacenter") or {}).get("id") == vdc_id
        ]
        path = provider_vcf_path(
            target, INFRA_VDC_VM_CLASSES_PATH.format(vdcUrn=safe_segment(vdc_id, "quota id"))
        )
        classes = await list_all(connector, target, operator, path)
        vm_classes = [ref(vmc) for vmc in classes]
        rows.append(
            {
                "id": vdc_id,
                "name": vdc.get("name"),
                "org": vdc.get("org"),
                "region": vdc.get("region"),
                "status": vdc.get("status"),
                "supervisors": vdc.get("supervisors") or [],
                "zones": _zone_rows(vdc),
                "storage_policies": storage,
                "vm_classes": vm_classes,
                "warnings": _quota_warnings(storage, vm_classes),
            }
        )
    return rows


def _name_of(value: Any) -> str:
    return str((value or {}).get("name") or "").casefold() if isinstance(value, dict) else ""


async def provider_org_quota_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org_quota.list`` -- the organization quotas, one row each."""
    org = str(params.get("org") or "").strip().casefold()
    region = str(params.get("region") or "").strip().casefold()

    async def _read() -> dict[str, Any]:
        vdcs = [
            vdc
            for vdc in await list_vcf(connector, target, operator, INFRA_VDCS_PATH)
            if (not org or _name_of(vdc.get("org")) == org)
            and (not region or _name_of(vdc.get("region")) == region)
        ]
        items = await quota_rows(connector, target, operator, vdcs)
        return {"state": "ok", "items": items, "guidance": None}

    return await _with_reachability(connector, target, operator, params, _read, {"items": []})
