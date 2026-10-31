# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create or change a VCF Automation region (provider plane).

``vcfa.provider.region.set`` builds a region from one NSX manager, at least
one Supervisor and at least one vCenter storage policy name. A new region is
a ``POST``, a change a ``PUT`` of the region as read; both answer ``202``
and start a vendor job, so the op returns ``accepted`` with the job id and
names ``vcfa.provider.region.get`` as the status read.

Before a start the op reads the region: while its job still runs
(``NOT_READY``) it starts nothing and says ``already_running``. The park
preview stores a hash of the region as read; the approved run reads it again
and answers ``precondition_failed`` when it changed.

Wire facts follow ``go-vcloud-director`` v3 (``govcd/tm_region.go``,
``Region`` in ``types/v56/tm.go``) and the VCF Automation 9.0, 9.1 and 9.1.1
provider API references.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_NSX_MANAGERS_PATH,
    INFRA_REGION_PATH,
    INFRA_REGIONS_PATH,
    INFRA_SUPERVISORS_PATH,
    provider_vcf_path,
    safe_segment,
)
from meho_backplane.connectors.vcf_automation._infra_reads import find_region, list_vcf
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["REGION_SET_OP_ID", "provider_region_set"]

REGION_SET_OP_ID: Final = "vcfa.provider.region.set"
_NOT_READY: Final = "NOT_READY"


def _answer(
    status: str,
    log: StepLog,
    *,
    obj: dict[str, Any] | None,
    guidance: str,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "object": obj,
        "job": job,
        "steps": log.as_list(),
        "warnings": [],
        "checked_at": now_iso(),
        "guidance": guidance,
    }


def _region_state(region: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if region is None:
        return None
    return {
        "id": region.get("id"),
        "status": region.get("status"),
        "description": region.get("description"),
        "nsxManager": (region.get("nsxManager") or {}).get("id"),
        "supervisors": ids_of(region.get("supervisors")),
        "storagePolicies": sorted(region.get("storagePolicies") or []),
    }


def _would(region: Mapping[str, Any] | None, wanted: Mapping[str, Any]) -> str:
    if region is None:
        return "create"
    if region.get("status") == _NOT_READY:
        return "already_running"
    current = _region_state(region) or {}
    same = (
        current["description"] == wanted["description"]
        and current["nsxManager"] == wanted["nsxManager"]["id"]
        and current["supervisors"] == ids_of(wanted["supervisors"])
        and current["storagePolicies"] == wanted["storagePolicies"]
    )
    return "unchanged" if same else "update"


async def _region_plan(call: Call, params: Mapping[str, Any]) -> dict[str, Any]:
    """The region as read, the wanted region, and what the call would do."""
    connector, target, operator = call.connector, call.target, call.operator
    name = str(params["name"])
    region = await find_region(connector, target, operator, name)
    nsx_rows = await list_vcf(connector, target, operator, INFRA_NSX_MANAGERS_PATH)
    nsx = match_row(nsx_rows, str(params["nsx_manager"]), "NSX manager")
    sup_rows = await list_vcf(connector, target, operator, INFRA_SUPERVISORS_PATH)
    supervisors = [
        ref(match_row(sup_rows, str(s), "Supervisor", "supervisorId"), id_field="supervisorId")
        for s in params["supervisors"]
    ]
    description = params.get("description")
    if description is None:
        description = (region or {}).get("description") or ""
    wanted = {
        "name": name,
        "description": description,
        "nsxManager": ref(nsx),
        "supervisors": supervisors,
        "storagePolicies": sorted(set(params["storage_policies"])),
    }
    would = _would(region, wanted)
    return {
        "would": would,
        "region": region,
        "wanted": wanted,
        "state_hash": state_hash({"would": would, "region": _region_state(region)}),
    }


_RUNNING_TEXT: Final = (
    "A job on this region still runs; read vcfa.provider.region.get until its status is "
    "READY. Nothing was started."
)


async def provider_region_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.region.set`` -- create or change one region (one job)."""
    log = StepLog(("region",))
    try:
        plan = await _region_plan(Call(connector, target, operator), params)
    except InvalidRequestError as exc:
        return _answer(
            "invalid_request",
            log,
            obj=None,
            guidance=f"{exc}; nothing was written. Read vcfa.provider.infra.list for the names",
        )
    region, would = plan["region"], plan["would"]
    obj = {"kind": "region", "name": params["name"], "id": (region or {}).get("id")}
    problem = approved_state_problem(REGION_SET_OP_ID, plan["state_hash"])
    if problem is not None:
        return _answer("precondition_failed", log, obj=obj, guidance=problem)
    if would in {"already_running", "unchanged"}:
        log.mark("region", "skipped", f"the region status is {region.get('status')}")
        guidance = (
            _RUNNING_TEXT
            if would == "already_running"
            else "Nothing to do: the region already looks like this."
        )
        return _answer(would, log, obj=obj, guidance=guidance)
    if would == "create":
        verb, path, body = "POST", INFRA_REGIONS_PATH, plan["wanted"]
    else:
        urn = safe_segment(region.get("id"), "region id")
        verb, path = "PUT", INFRA_REGION_PATH.format(regionUrn=urn)
        body = {**region, **plan["wanted"]}
    resp = await connector._send_with_retry(
        target, verb, provider_vcf_path(target, path), operator=operator, json=body
    )
    job = job_from_response(resp, status_read="vcfa.provider.region.get")
    log.mark("region", "started", f"VCF Automation accepted the {would}", job_id=job["id"])
    return _answer(
        "accepted",
        log,
        obj=obj,
        job=job,
        guidance=(
            "Read vcfa.provider.region.get (with job_id) until the region status is READY. "
            "A storage policy name must exist in every vCenter of the region."
        ),
    )


async def _region_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the region as read, the wanted region, and a state hash."""
    base = {"action": "set_region", "name": ctx.params.get("name")}
    connector: Any = ctx.connector_instance
    if connector is None:
        return {**base, "would": "unknown"}
    try:
        plan = await _region_plan(Call(connector, ctx.target, ctx.operator), ctx.params)
    except InvalidRequestError as exc:
        return {**base, "would": "invalid_request", "reason": str(exc)}
    return {
        **base,
        "would": plan["would"],
        "current": _region_state(plan["region"]),
        "requested": plan["wanted"],
        "state_hash": plan["state_hash"],
    }


register_preview_builder(REGION_SET_OP_ID, _region_set_preview)
