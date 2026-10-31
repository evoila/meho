# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler and approval card of ``vcfa.provider.provider_gateway.set``.

A new gateway: read the free Tier-0 gateways of the region (always with the
``regionId`` filter; without a filter the list is empty), then ``POST`` the
gateway with its first IP spaces (``ipSpaceRefs``). The backing type is
``NSX_TIER0``, or ``NSX_VRF_TIER0`` for a Tier-0 VRF. The reference pages only
show ``backingType`` as a string; the two values come from the vendor's Go SDK
(``go-vcloud-director`` v3, ``types/v56/tm.go``). An existing gateway: link the
IP spaces it does not have yet, one ``POST`` to the IP space links each. When a
link is refused part-way, the answer lists the links that started, the one
that failed (with the vendor's error) and the ones that did not run; nothing
is undone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._lookups import fiql_safe, list_all
from meho_backplane.connectors.vcf_automation._provider_network import (
    VcfaProviderNetworkError,
    drift_reason,
    job_id_of,
    list_kind,
    send,
    status_read,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_GATEWAYS_PATH,
    PROVIDER_IP_SPACE_ASSOCIATIONS_PATH,
    PROVIDER_TIER0_ROUTERS_PATH,
    is_v1_target,
    provider_path,
    target_version_label,
)
from meho_backplane.connectors.vcf_automation._provider_start import (
    ASYNC_NOTE,
    StartRefusedError,
    check_ids,
    existing_card,
    object_view,
    one_existing,
    read_with_job,
    running_answer,
    start_result,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["GATEWAY_V91_FIELDS", "provider_gateway_set"]

GATEWAY_OP: Final = "vcfa.provider.provider_gateway.set"
#: Gateway params whose fields only the 9.1 reference lists. VCF Automation
#: 9.0 refuses an unknown field with HTTP 400 ("Unrecognized field").
GATEWAY_V91_FIELDS: Final = (
    "inbound_remote_networks",
    "allow_advertising_private_ip_blocks",
    "nat",
)


async def _free_tier0(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    region_id: str,
    wanted: str,
) -> dict[str, Any]:
    """The free Tier-0 gateway *wanted* (its id or display name) of the region."""
    if not fiql_safe(region_id):
        raise StartRefusedError(f"region id {region_id!r} cannot be used in a filter")
    rows = await list_all(
        connector,
        target,
        operator,
        PROVIDER_TIER0_ROUTERS_PATH,
        params={"filter": f"regionId=={region_id}"},
    )
    matches = [row for row in rows if wanted in (row.get("id"), row.get("displayName"))]
    if len(matches) != 1:
        free = sorted(str(row.get("displayName")) for row in rows if not row.get("alreadyImported"))
        raise StartRefusedError(
            f"no single free Tier-0 gateway {wanted!r} in this region; free ones: {free or 'none'}"
        )
    if matches[0].get("alreadyImported"):
        raise StartRefusedError(f"Tier-0 gateway {wanted!r} is already used by a provider gateway")
    return matches[0]


def _gateway_body(params: Mapping[str, Any], tier0: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": params["name"],
        "regionRef": {"id": params["region_id"]},
        "backingType": "NSX_VRF_TIER0" if tier0.get("parentTier0Id") else "NSX_TIER0",
        "backingRef": {"id": tier0.get("id")},
        "ipSpaceRefs": [{"id": ip_space_id} for ip_space_id in params["ip_space_ids"]],
    }
    if params.get("description"):
        body["description"] = params["description"]
    if "inbound_remote_networks" in params:
        body["inboundRemoteNetworks"] = list(params["inbound_remote_networks"])
    if "allow_advertising_private_ip_blocks" in params:
        body["allowAdvertisingPrivateIpBlocks"] = params["allow_advertising_private_ip_blocks"]
    nat = params.get("nat")
    if nat:
        body["natConfig"] = {
            "enableSnat": nat.get("enable_snat", False),
            "logging": nat.get("logging", False),
            **({"ipSpaceRef": {"id": nat["ip_space_id"]}} if nat.get("ip_space_id") else {}),
        }
    return body


async def _link_ip_spaces(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    gateway: Mapping[str, Any],
    missing: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One link per IP space; stops at the first refused link. ``(steps, failed)``."""
    path = provider_path(target, PROVIDER_IP_SPACE_ASSOCIATIONS_PATH)
    steps: list[dict[str, Any]] = []
    for ip_space_id in missing:
        body = {"ipSpaceRef": {"id": ip_space_id}, "providerGatewayRef": {"id": gateway.get("id")}}
        try:
            resp = await send(connector, target, operator, "POST", path, body=body)
        except httpx.HTTPStatusError as exc:
            failed = {
                "step": f"link_ip_space:{ip_space_id}",
                "http_status": exc.response.status_code,
                "error": exc.response.text[:500],
            }
            return steps, failed
        steps.append(
            {
                "step": f"link_ip_space:{ip_space_id}",
                "status": "accepted",
                "job_id": job_id_of(resp),
            }
        )
    return steps, None


async def _gateway_links(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
    gateway: dict[str, Any],
    read: dict[str, Any],
) -> dict[str, Any]:
    """An existing gateway: link the IP spaces it does not have yet."""
    links = await list_kind(
        connector, target, operator, "ip_space_associations", {"provider_gateway_id": gateway["id"]}
    )
    linked = {(link.get("ipSpaceRef") or {}).get("id") for link in links}
    missing = [ip_space_id for ip_space_id in params["ip_space_ids"] if ip_space_id not in linked]
    view = object_view(gateway)
    if not missing:
        return start_result(
            "unchanged",
            operation="link_ip_spaces",
            object=view,
            status_read=read,
            guidance="the gateway exists and has every IP space linked; nothing was started",
        )
    steps, failed = await _link_ip_spaces(connector, target, operator, gateway, missing)
    if failed is None:
        return start_result(
            "accepted",
            operation="link_ip_spaces",
            object=view,
            job_id=steps[0]["job_id"],
            steps=steps,
            status_read=status_read("ip_space_associations", provider_gateway_id=gateway["id"]),
            guidance=(
                "VCF Automation accepted the links; read them with the status read. " + ASYNC_NOTE
            ),
        )
    done = {step["step"] for step in steps} | {failed["step"]}
    return start_result(
        "failed_part_way",
        operation="link_ip_spaces",
        object=view,
        steps=steps,
        step_failed=failed,
        steps_not_run=[f"link_ip_space:{i}" for i in missing if f"link_ip_space:{i}" not in done],
        status_read=status_read("ip_space_associations", provider_gateway_id=gateway["id"]),
        guidance=(
            "a link was refused; the links before it were started and are not undone. Read "
            "the links, fix the cause and run the op again: it links only what is missing"
        ),
    )


async def provider_gateway_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.provider_gateway.set`` — create a gateway, or link more IP spaces."""
    name, region_id = params["name"], params["region_id"]
    read = status_read("provider_gateways", name=name, region_id=region_id)
    newer = [key for key in GATEWAY_V91_FIELDS if key in params]
    if newer and not is_v1_target(target):
        return start_result(
            "invalid_request",
            guidance=(
                f"{newer} need VCF Automation 9.1 or newer; this target runs "
                f"{target_version_label(target) or 'an unknown version (taken as 9.0)'}, which "
                "refuses these fields (HTTP 400 Unrecognized field). Leave them out; nothing "
                "was started"
            ),
        )
    try:
        check_ids(
            region=region_id, **{f"ip_space_{i}": v for i, v in enumerate(params["ip_space_ids"])}
        )
        existing = await one_existing(
            connector, target, operator, "provider_gateways", {"name": name, "region_id": region_id}
        )
        drift = drift_reason(GATEWAY_OP, existing)
        if drift is not None:
            return start_result("precondition_failed", object=object_view(existing), guidance=drift)
        running = running_answer(existing, "provider gateway", read)
        if running is not None:
            return running
        if existing is not None:
            return await _gateway_links(connector, target, operator, params, existing, read)
        if not params.get("tier0"):
            raise StartRefusedError(
                "a new gateway needs tier0: a free Tier-0 gateway of the region "
                "(vcfa.provider.network.list, kind tier0_gateways)"
            )
        tier0 = await _free_tier0(connector, target, operator, region_id, params["tier0"])
    except (StartRefusedError, VcfaProviderNetworkError) as exc:
        return start_result("invalid_request", guidance=f"{exc}; nothing was started")
    path = provider_path(target, PROVIDER_GATEWAYS_PATH)
    resp = await send(connector, target, operator, "POST", path, body=_gateway_body(params, tier0))
    job_id = job_id_of(resp)
    return start_result(
        "accepted",
        operation="create",
        object={"id": None, "name": name, "region_id": region_id, "tier0_id": tier0.get("id")},
        job_id=job_id,
        steps=[{"step": "create_gateway", "status": "accepted", "job_id": job_id}],
        status_read=read_with_job(read, job_id),
        guidance=(
            "VCF Automation accepted the job; it is not done yet. Read the gateway with the "
            f"status read until state is succeeded; regional networking needs it then. {ASYNC_NOTE}"
        ),
    )


async def _gateway_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    if not isinstance(p.get("name"), str) or not isinstance(p.get("region_id"), str):
        return None
    return await existing_card(
        ctx,
        "provider_gateways",
        {"name": p["name"], "region_id": p["region_id"]},
        "set_provider_gateway",
    )


register_preview_builder(GATEWAY_OP, _gateway_preview)
