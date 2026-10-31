# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler and approval card of ``vcfa.provider.regional_networking.set``.

Gives an organization networking in a region: ``POST`` a regional networking
setting that links the organization to a provider gateway and, if given, an
edge cluster. VCF Automation then makes the organization's NSX project,
transit gateway and default VPC. Before the start, MEHO checks two things the
appliance would refuse later: the organization's short log name is set
(else HTTP 400 "Short Log Name is not set"), and the gateway reads REALIZED.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._provider_network import (
    VcfaProviderNetworkError,
    drift_reason,
    job_id_of,
    read_one,
    resolve_org,
    send,
    state_hash,
    status_read,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_ORG_NETWORKING_PATH,
    PROVIDER_REGIONAL_NETWORKING_PATH,
    provider_path,
)
from meho_backplane.connectors.vcf_automation._provider_start import (
    ASYNC_NOTE,
    StartRefusedError,
    check_ids,
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

__all__ = ["provider_regional_networking_set"]

REGIONAL_OP: Final = "vcfa.provider.regional_networking.set"


async def _short_log_name(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    org_id: str,
) -> str | None:
    path = provider_path(target, PROVIDER_ORG_NETWORKING_PATH, orgUrn=org_id)
    settings = await connector._request_json(target, "GET", path, operator=operator)
    value = settings.get("orgNameForLogs")
    return value if isinstance(value, str) and value else None


async def _regional_checks(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``(org, existing setting)``; raises :class:`StartRefusedError` to refuse the start."""
    check_ids(
        region=params["region_id"],
        provider_gateway=params["provider_gateway_id"],
        edge_cluster=params.get("edge_cluster_id"),
    )
    org = await resolve_org(connector, target, operator, params["org"])
    existing = await one_existing(
        connector,
        target,
        operator,
        "regional_networking",
        {"org_id": org["id"], "region_id": params["region_id"]},
    )
    return org, existing


async def provider_regional_networking_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.regional_networking.set`` — give an organization networking in a region."""
    try:
        org, existing = await _regional_checks(connector, target, operator, params)
    except (StartRefusedError, VcfaProviderNetworkError) as exc:
        return start_result("invalid_request", guidance=f"{exc}; nothing was started")
    read = status_read("regional_networking", org_id=org["id"], region_id=params["region_id"])
    drift = drift_reason(REGIONAL_OP, existing)
    if drift is not None:
        return start_result("precondition_failed", object=object_view(existing), guidance=drift)
    running = running_answer(existing, "regional networking", read)
    if running is not None:
        return running
    if existing is not None:
        return start_result(
            "unchanged",
            operation="create",
            object=object_view(existing),
            status_read=read,
            guidance="the organization already has networking in this region; nothing was started",
        )
    if await _short_log_name(connector, target, operator, str(org["id"])) is None:
        return start_result(
            "invalid_request",
            guidance=(
                "Short Log Name is not set for this organization, and VCF Automation refuses "
                "regional networking without it. Set it with vcfa.provider.org.networking.set "
                "first; nothing was started"
            ),
        )
    gateway = await read_one(
        connector, target, operator, "provider_gateways", params["provider_gateway_id"]
    )
    if gateway is None or str(gateway.get("status") or "").upper() != "REALIZED":
        state = "missing" if gateway is None else gateway.get("status")
        return start_result(
            "invalid_request",
            guidance=(
                f"the provider gateway is {state}, not REALIZED. Wait until it reads REALIZED "
                "(vcfa.provider.network.list, kind provider_gateways); nothing was started"
            ),
        )
    body: dict[str, Any] = {
        "orgRef": {"id": org["id"]},
        "regionRef": {"id": params["region_id"]},
        "providerGatewayRef": {"id": params["provider_gateway_id"]},
    }
    if params.get("name"):
        body["name"] = params["name"]
    if params.get("edge_cluster_id"):
        body["serviceEdgeClusterRef"] = {"id": params["edge_cluster_id"]}
    path = provider_path(target, PROVIDER_REGIONAL_NETWORKING_PATH)
    resp = await send(connector, target, operator, "POST", path, body=body)
    return start_result(
        "accepted",
        operation="create",
        object={
            "id": None,
            "name": params.get("name"),
            "org_id": org["id"],
            "region_id": params["region_id"],
        },
        job_id=job_id_of(resp),
        status_read=read,
        guidance=(
            "VCF Automation accepted the job; it now makes the organization's NSX project, "
            "transit gateway and default VPC. Read the status until state is succeeded. "
            f"{ASYNC_NOTE}"
        ),
    )


async def _regional_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    if not isinstance(p.get("org"), str) or not isinstance(p.get("region_id"), str):
        return None
    preview: dict[str, Any] = {
        "action": "set_regional_networking",
        "requested": dict(p),
        "existing": None,
        "state_hash": None,
        "creates": ["NSX project", "transit gateway", "default VPC"],
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        _org, existing = await _regional_checks(connector, ctx.target, ctx.operator, p)
    except (StartRefusedError, VcfaProviderNetworkError, httpx.HTTPError, RuntimeError) as exc:
        return {**preview, "would": "invalid_request", "reason": str(exc)}
    return {
        **preview,
        "existing": object_view(existing),
        "state_hash": state_hash(existing),
        "would": "create" if existing is None else "unchanged",
    }


register_preview_builder(REGIONAL_OP, _regional_preview)
