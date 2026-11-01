# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers and approval cards for the organization settings changes.

* ``vcfa.provider.org.update`` -- ``GET /cloudapi/1.0.0/orgs/{orgUrn}``, then
  ``PUT`` the whole organization with the changed fields. The name and the
  kind (VM Apps or All Apps) never change. Turning an organization off stops
  its users from signing in.
* ``vcfa.provider.org.networking.set`` -- ``GET`` and ``PUT``
  ``/cloudapi/1.0.0/orgs/{orgUrn}/networkingSettings``: networking on or off,
  and the short name for logs. The short name becomes the id of the
  organization's NSX project, so MEHO refuses to change it once it is set.

Both are ``dangerous`` and wait for a person's approval. The approval card
shows the current values and a hash of them; the approved run reads the
object again and stops with ``precondition_failed`` when it changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._provider_network import (
    VcfaProviderNetworkError,
    drift_reason,
    job_id_of,
    resolve_org,
    send,
    state_hash,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_ORG_NETWORKING_PATH,
    PROVIDER_ORG_PATH,
    provider_path,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["provider_org_networking_set", "provider_org_update"]

ORG_UPDATE_OP: Final = "vcfa.provider.org.update"
ORG_NETWORKING_OP: Final = "vcfa.provider.org.networking.set"
#: Op param -> ``Org`` field the update may change.
_ORG_FIELDS: Final = {
    "display_name": "displayName",
    "description": "description",
    "is_enabled": "isEnabled",
    "can_publish": "canPublish",
}
_SIGN_IN_WARNING: Final = (
    "turning the organization off stops its users from signing in until it is turned on again"
)


def _org_state(org: Mapping[str, Any]) -> dict[str, Any]:
    """The organization fields the update can change, plus its identity."""
    return {
        "id": org.get("id"),
        "name": org.get("name"),
        "displayName": org.get("displayName"),
        "description": org.get("description"),
        "isEnabled": org.get("isEnabled"),
        "canPublish": org.get("canPublish"),
    }


def _org_changes(org: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """``{Org field: new value}`` for every asked field that differs."""
    return {
        wire: params[param]
        for param, wire in _ORG_FIELDS.items()
        if param in params and org.get(wire) != params[param]
    }


async def provider_org_update(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org.update`` — change an organization's settings."""
    base: dict[str, Any] = {"org": None, "previous": None, "job_id": None, "guidance": None}
    try:
        org = await resolve_org(connector, target, operator, params["org"])
    except VcfaProviderNetworkError as exc:
        return {**base, "status": "invalid_request", "guidance": f"{exc}; nothing was written"}
    previous = _org_state(org)
    drift = drift_reason(ORG_UPDATE_OP, previous)
    if drift is not None:
        return {**base, "status": "precondition_failed", "previous": previous, "guidance": drift}
    changes = _org_changes(org, params)
    if not changes:
        return {
            **base,
            "status": "unchanged",
            "org": previous,
            "previous": previous,
            "guidance": "the organization already has these settings; nothing was written",
        }
    path = provider_path(target, PROVIDER_ORG_PATH, orgUrn=str(org["id"]))
    resp = await send(connector, target, operator, "PUT", path, body={**org, **changes})
    accepted = resp.status_code == 202
    after = _org_state({**org, **changes})
    guidance = _SIGN_IN_WARNING if changes.get("isEnabled") is False else None
    if accepted:
        guidance = "; ".join(
            part
            for part in (
                "VCF Automation accepted the change and finishes it on its own; read the "
                "organization with vcfa.provider.org.list to see it",
                guidance,
            )
            if part
        )
    return {
        **base,
        "status": "accepted" if accepted else "changed",
        "org": after,
        "previous": previous,
        "job_id": job_id_of(resp) if accepted else None,
        "guidance": guidance,
    }


def _networking_problem(current: Mapping[str, Any], params: Mapping[str, Any]) -> str | None:
    """Why the networking change must not run, or ``None``."""
    current_name = current.get("orgNameForLogs") or None
    wanted_name = params.get("org_name_for_logs")
    if current_name and wanted_name and wanted_name != current_name:
        return (
            f"the short name for logs is already {current_name!r}. It is the id of the "
            "organization's NSX project, so it is chosen once and MEHO does not change it"
        )
    if params["networking_enabled"] and not (wanted_name or current_name):
        return (
            "networking needs a short name for logs (org_name_for_logs, at most 8 "
            "characters); it becomes the id of the organization's NSX project"
        )
    return None


async def _read_networking(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    org_id: str,
) -> dict[str, Any]:
    path = provider_path(target, PROVIDER_ORG_NETWORKING_PATH, orgUrn=org_id)
    return await connector._request_json(target, "GET", path, operator=operator)


def _networking_view(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "networking_enabled": settings.get("networkingTenancyEnabled"),
        "org_name_for_logs": settings.get("orgNameForLogs") or None,
    }


async def provider_org_networking_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org.networking.set`` — networking on or off, and the short log name."""
    base: dict[str, Any] = {"org_id": None, "settings": None, "previous": None, "guidance": None}
    try:
        org = await resolve_org(connector, target, operator, params["org"])
    except VcfaProviderNetworkError as exc:
        return {**base, "status": "invalid_request", "guidance": f"{exc}; nothing was written"}
    org_id = str(org["id"])
    current = await _read_networking(connector, target, operator, org_id)
    previous = _networking_view(current)
    base = {**base, "org_id": org_id, "previous": previous}
    drift = drift_reason(ORG_NETWORKING_OP, current)
    if drift is not None:
        return {**base, "status": "precondition_failed", "guidance": drift}
    problem = _networking_problem(current, params)
    if problem is not None:
        return {**base, "status": "invalid_request", "guidance": f"{problem}; nothing was written"}
    body = {
        "networkingTenancyEnabled": bool(params["networking_enabled"]),
        "orgNameForLogs": params.get("org_name_for_logs") or current.get("orgNameForLogs") or "",
    }
    if _networking_view(body) == previous:
        return {
            **base,
            "status": "unchanged",
            "settings": previous,
            "guidance": (
                "the organization already has these networking settings; nothing was written"
            ),
        }
    path = provider_path(target, PROVIDER_ORG_NETWORKING_PATH, orgUrn=org_id)
    resp = await send(connector, target, operator, "PUT", path, body=body)
    try:
        answered = resp.json() if resp.content else None
    except ValueError:
        answered = None
    settings = _networking_view(answered if isinstance(answered, dict) else body)
    return {**base, "status": "changed", "settings": settings, "guidance": None}


# ---------------------------------------------------------------------------
# Approval cards
# ---------------------------------------------------------------------------


async def _org_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The card: the organization as it is now, what changes, and a hash of now."""
    p = ctx.params
    org_value = p.get("org")
    if not isinstance(org_value, str):
        return None
    requested = {param: p[param] for param in _ORG_FIELDS if param in p}
    preview: dict[str, Any] = {
        "action": "update_org",
        "org": org_value,
        "requested": requested,
        "current": None,
        "changes": None,
        "state_hash": None,
        "warning": _SIGN_IN_WARNING if requested.get("is_enabled") is False else None,
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        org = await resolve_org(connector, ctx.target, ctx.operator, org_value)
    except (VcfaProviderNetworkError, httpx.HTTPError, RuntimeError) as exc:
        return {**preview, "would": "invalid_request", "reason": str(exc)}
    current = _org_state(org)
    changes = _org_changes(org, p)
    return {
        **preview,
        "current": current,
        "changes": changes,
        "state_hash": state_hash(current),
        "would": "change" if changes else "unchanged",
    }


async def _org_networking_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    org_value = p.get("org")
    if not isinstance(org_value, str) or not isinstance(p.get("networking_enabled"), bool):
        return None
    preview: dict[str, Any] = {
        "action": "set_org_networking",
        "org": org_value,
        "requested": {
            "networking_enabled": p["networking_enabled"],
            "org_name_for_logs": p.get("org_name_for_logs"),
        },
        "current": None,
        "state_hash": None,
        "note": "the short name for logs becomes the id of the organization's NSX project",
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        org = await resolve_org(connector, ctx.target, ctx.operator, org_value)
        current = await _read_networking(connector, ctx.target, ctx.operator, str(org["id"]))
    except (VcfaProviderNetworkError, httpx.HTTPError, RuntimeError) as exc:
        return {**preview, "would": "invalid_request", "reason": str(exc)}
    problem = _networking_problem(current, p)
    return {
        **preview,
        "org_id": org.get("id"),
        "current": _networking_view(current),
        "state_hash": state_hash(current),
        "would": "invalid_request" if problem else "set_if_different",
        "reason": problem,
    }


register_preview_builder(ORG_UPDATE_OP, _org_update_preview)
register_preview_builder(ORG_NETWORKING_OP, _org_networking_preview)
