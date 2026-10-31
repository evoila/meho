# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler and approval card of ``vcfa.provider.ip_space.set``.

Creates (``POST``) or changes (``PUT``) an IP space of a region: its address
ranges and its default limits. A change only adds ranges; it never drops one.
VCF Automation makes a new NSX IP block for each new range, even when NSX has
one with the same addresses, and both blocks would hand out the same
addresses. So a new range needs ``range_is_new_in_nsx: true`` from the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._provider_network import (
    VcfaProviderNetworkError,
    drift_reason,
    job_id_of,
    send,
    status_read,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_IP_SPACE_PATH,
    PROVIDER_IP_SPACES_PATH,
    provider_path,
)
from meho_backplane.connectors.vcf_automation._provider_start import (
    ASYNC_NOTE,
    StartRefusedError,
    check_ids,
    existing_card,
    object_view,
    one_existing,
    running_answer,
    start_result,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["provider_ip_space_set"]

IP_SPACE_OP: Final = "vcfa.provider.ip_space.set"


def _quota(params: Mapping[str, Any]) -> dict[str, Any]:
    quota = params.get("default_quota") or {}
    wire = {
        "max_cidr_count": "maxCidrCount",
        "max_ip_count": "maxIpCount",
        "max_subnet_size": "maxSubnetSize",
    }
    return {wire[key]: value for key, value in quota.items() if key in wire}


def _blocks(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"cidr": block["cidr"], **({"name": block["name"]} if block.get("name") else {})}
        for block in params.get("cidr_blocks") or []
    ]


def _ip_space_plan(
    existing: Mapping[str, Any] | None, params: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """``(operation, body, new_cidrs)``; body ``None`` means nothing to change."""
    blocks = _blocks(params)
    if existing is None:
        body: dict[str, Any] = {
            "name": params["name"],
            "regionRef": {"id": params["region_id"]},
            "internalScopeCidrBlocks": blocks,
            "defaultQuota": _quota(params),
        }
        if params.get("description"):
            body["description"] = params["description"]
        return "create", body, [block["cidr"] for block in blocks]
    current_blocks = [
        b for b in existing.get("internalScopeCidrBlocks") or [] if isinstance(b, dict)
    ]
    known = {b.get("cidr") for b in current_blocks}
    added = [block for block in blocks if block["cidr"] not in known]
    quota = {**(existing.get("defaultQuota") or {}), **_quota(params)}
    changed = {key: value for key, value in existing.items() if key != "status"}
    changed["internalScopeCidrBlocks"] = current_blocks + added
    changed["defaultQuota"] = quota
    if "description" in params:
        changed["description"] = params["description"]
    same = (
        not added
        and quota == (existing.get("defaultQuota") or {})
        and changed.get("description") == existing.get("description")
    )
    return "update", None if same else changed, [block["cidr"] for block in added]


async def provider_ip_space_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.ip_space.set`` — create or change an IP space of a region."""
    name, region_id = params["name"], params["region_id"]
    read = status_read("ip_spaces", name=name, region_id=region_id)
    try:
        check_ids(region=region_id)
        existing = await one_existing(
            connector, target, operator, "ip_spaces", {"name": name, "region_id": region_id}
        )
    except (StartRefusedError, VcfaProviderNetworkError) as exc:
        return start_result("invalid_request", guidance=f"{exc}; nothing was started")
    drift = drift_reason(IP_SPACE_OP, existing)
    if drift is not None:
        return start_result("precondition_failed", object=object_view(existing), guidance=drift)
    running = running_answer(existing, "IP space", read)
    if running is not None:
        return running
    operation, body, new_cidrs = _ip_space_plan(existing, params)
    if operation == "create" and not new_cidrs:
        return start_result(
            "invalid_request",
            operation=operation,
            guidance="a new IP space needs at least one range in cidr_blocks; nothing was started",
        )
    if body is None:
        return start_result(
            "unchanged",
            operation=operation,
            object=object_view(existing),
            status_read=read,
            guidance="the IP space already has these ranges and limits; nothing was started",
        )
    if new_cidrs and params.get("range_is_new_in_nsx") is not True:
        return start_result(
            "invalid_request",
            operation=operation,
            object=object_view(existing),
            guidance=(
                f"confirm that NSX has none of {new_cidrs} yet (range_is_new_in_nsx: true). "
                "VCF Automation makes a new NSX IP block even when one exists, and both would "
                "hand out the same addresses; nothing was started"
            ),
        )
    if existing is None:
        path = provider_path(target, PROVIDER_IP_SPACES_PATH)
        resp = await send(connector, target, operator, "POST", path, body=body)
    else:
        check_ids(ip_space=existing.get("id"))
        path = provider_path(target, PROVIDER_IP_SPACE_PATH, ipSpaceId=str(existing["id"]))
        resp = await send(connector, target, operator, "PUT", path, body=body)
    return start_result(
        "accepted",
        operation=operation,
        object={
            "id": existing.get("id") if existing else None,
            "name": name,
            "region_id": region_id,
        },
        job_id=job_id_of(resp),
        status_read=read,
        guidance=(
            "VCF Automation accepted the job; it is not done yet. Read the status with "
            f"vcfa.provider.network.list until state is succeeded or failed. {ASYNC_NOTE}"
        ),
    )


async def _ip_space_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    if not isinstance(p.get("name"), str) or not isinstance(p.get("region_id"), str):
        return None
    card = await existing_card(
        ctx, "ip_spaces", {"name": p["name"], "region_id": p["region_id"]}, "set_ip_space"
    )
    return {
        **card,
        "warning": (
            "VCF Automation makes a new NSX IP block for each range, even when NSX has one; "
            "two blocks would then hand out the same addresses"
        ),
    }


register_preview_builder(IP_SPACE_OP, _ip_space_preview)
