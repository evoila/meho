# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add an NSX manager to VCF Automation (provider plane).

``vcfa.provider.nsx_manager.register`` adds an NSX manager in one vendor
job. An existing NSX manager is matched by the host and port of its URL; a
different one with the same name refuses the call. The vendor refuses a
body without ``description``, so it is always sent. The password comes
from a Vault reference (``password_secret_ref``) read under the caller's
identity and goes only into the request body.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._infra_common import StepLog, job_from_response
from meho_backplane.connectors.vcf_automation._infra_connect import (
    answer,
    match_existing,
    name_taken_guidance,
)
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_NSX_MANAGERS_PATH,
    provider_vcf_path,
)
from meho_backplane.connectors.vcf_automation._infra_reads import list_vcf
from meho_backplane.connectors.vcf_automation._lookups import (
    password_missing_guidance,
    read_password,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["NSX_REGISTER_OP_ID", "provider_nsx_manager_register"]

NSX_REGISTER_OP_ID: Final = "vcfa.provider.nsx_manager.register"
_STATUS_READ: Final = "vcfa.provider.infra.list"
#: NSX manager states of a job that still runs.
_NSX_RUNNING: Final = frozenset({"PENDING", "CONFIGURING"})


def _nsx_object(params: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "nsx_manager",
        "name": (existing or {}).get("name") or params["name"],
        "url": (existing or {}).get("url") or params["url"],
        "id": (existing or {}).get("id"),
        "state": (existing or {}).get("status"),
    }


async def provider_nsx_manager_register(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.nsx_manager.register`` -- add an NSX manager (one job)."""
    log = StepLog(("add",))
    url, name = str(params["url"]), str(params["name"])
    rows = await list_vcf(connector, target, operator, INFRA_NSX_MANAGERS_PATH)
    existing, name_taken_by = match_existing(rows, url, name)
    if name_taken_by is not None:
        log.mark("add", "skipped", "another NSX manager has this name")
        return answer(
            "invalid_request",
            log,
            obj=_nsx_object(params, None),
            guidance=name_taken_guidance("NSX manager", name, name_taken_by),
        )
    if existing is not None:
        state = existing.get("status")
        running = state in _NSX_RUNNING
        log.mark("add", "skipped", f"an NSX manager with this URL exists ({state})")
        return answer(
            "already_running" if running else "unchanged",
            log,
            obj=_nsx_object(params, existing),
            guidance=(
                "VCF Automation is still setting this NSX manager up; read "
                "vcfa.provider.infra.list until its state is REALIZED. Nothing was started."
                if running
                else "Nothing to do: an NSX manager with this URL is already added."
            ),
        )
    password = await read_password(operator, target, params)
    if password is None:
        return answer(
            "invalid_request",
            log,
            obj=_nsx_object(params, None),
            guidance=password_missing_guidance(params),
        )
    body = {
        "name": name,
        # The vendor refuses a body without a description, even an empty one.
        "description": params.get("description") or "",
        "url": url,
        "username": params["username"],
        "password": password,
    }
    resp = await connector._send_with_retry(
        target,
        "POST",
        provider_vcf_path(target, INFRA_NSX_MANAGERS_PATH),
        operator=operator,
        json=body,
    )
    job = job_from_response(resp, status_read=_STATUS_READ)
    log.mark("add", "started", "VCF Automation accepted the job", job_id=job["id"])
    return answer(
        "accepted",
        log,
        obj=_nsx_object(params, None),
        job=job,
        guidance=(
            "Read vcfa.provider.infra.list (with job_id) until the NSX manager state is "
            "REALIZED, then use it in vcfa.provider.region.set."
        ),
    )


async def _nsx_register_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the NSX manager to add, or the one that exists."""
    params = ctx.params
    preview: dict[str, Any] = {
        "action": "register_nsx_manager",
        "name": params.get("name"),
        "url": params.get("url"),
        "username": params.get("username"),
        "would": "unknown",
        "current_name": None,
        "current_url": None,
        "current_state": None,
    }
    connector: Any = ctx.connector_instance
    if connector is None or not params.get("url") or not params.get("name"):
        return preview
    rows = await list_vcf(connector, ctx.target, ctx.operator, INFRA_NSX_MANAGERS_PATH)
    url, name = str(params["url"]), str(params["name"])
    existing, name_taken_by = match_existing(rows, url, name)
    row = existing or name_taken_by
    if row is None:
        return {**preview, "would": "add"}
    state = row.get("status")
    if existing is None:
        would = "name_taken"
    else:
        would = "already_running" if state in _NSX_RUNNING else "unchanged"
    return {
        **preview,
        "would": would,
        "current_name": row.get("name"),
        "current_url": row.get("url"),
        "current_state": state,
    }


register_preview_builder(NSX_REGISTER_OP_ID, _nsx_register_preview)
