# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the provider networking starts (IP space, gateway, regional networking).

VCF Automation answers each start with HTTP 202, an empty body and the task in
the ``Location`` header, then works on its own. So each start returns
``accepted`` with the vendor's task id (``job_id``) and the status read that
follows it (``vcfa.provider.network.list``); it never says the work is done.
Before a start, the op reads the object: when VCF Automation still works on it
(``PENDING`` or ``CONFIGURING``), it answers ``already_running`` and starts
nothing. This check helps but is not a lock: two callers can both check
before either starts.

A start that waits for approval binds the object state the approver saw (a
hash on the card); the approved run stops with ``precondition_failed`` when
the object changed in between.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._provider_network import (
    RUNNING_STATUSES,
    VcfaProviderNetworkError,
    list_kind,
    state_hash,
)
from meho_backplane.connectors.vcf_automation._provider_paths import bad_id_reason
from meho_backplane.operations._preview import PreviewContext

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "ASYNC_NOTE",
    "StartRefusedError",
    "check_ids",
    "existing_card",
    "object_view",
    "one_existing",
    "read_with_job",
    "running_answer",
    "start_result",
]

ASYNC_NOTE: Final = (
    "Call it with async: true, so MEHO keeps this answer (with the job id) under a run "
    "handle. After a lost reply, read the status first; never start again blindly."
)


class StartRefusedError(Exception):
    """A start that must not run; the message is the guidance."""


def check_ids(**ids: Any) -> None:
    for what, value in ids.items():
        if value is None:
            continue
        problem = bad_id_reason(value, what.replace("_", " "))
        if problem is not None:
            raise StartRefusedError(problem)


def start_result(status: str, *, guidance: str | None = None, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": status,
        "operation": None,
        "object": None,
        "job_id": None,
        "steps": [],
        "status_read": None,
        "guidance": guidance,
    }
    return {**base, **fields}


def read_with_job(read: dict[str, Any], job_id: str | None) -> dict[str, Any]:
    """The status read *read* with the vendor job id, so it also reads the job."""
    if job_id is None:
        return read
    return {**read, "params": {**read["params"], "job_id": job_id}}


def object_view(row: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row.get("id"), "name": row.get("name"), "status": row.get("status"), **extra}


async def one_existing(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    filters: Mapping[str, Any],
) -> dict[str, Any] | None:
    rows = await list_kind(connector, target, operator, kind, filters)
    if len(rows) > 1:
        raise StartRefusedError(
            f"{len(rows)} objects match {dict(filters)}; MEHO does not guess between them"
        )
    return rows[0] if rows else None


def running_answer(
    existing: Mapping[str, Any] | None, kind: str, read: dict[str, Any]
) -> dict[str, Any] | None:
    """The ``already_running`` answer when VCF Automation still works on *existing*."""
    if existing is None or str(existing.get("status") or "").upper() not in RUNNING_STATUSES:
        return None
    return start_result(
        "already_running",
        object=object_view(existing),
        status_read=read,
        guidance=(
            f"VCF Automation still works on this {kind} (status {existing.get('status')}); "
            "nothing was started. Read it with the status read until it ends"
        ),
    )


async def existing_card(
    ctx: PreviewContext, kind: str, filters: Mapping[str, Any], action: str
) -> dict[str, Any]:
    """The card: what is asked, the object as it is now, and a hash of now."""
    preview: dict[str, Any] = {
        "action": action,
        "requested": dict(ctx.params),
        "existing": None,
        "state_hash": None,
        "would": "create_or_change",
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        existing = await one_existing(connector, ctx.target, ctx.operator, kind, filters)
    except (StartRefusedError, VcfaProviderNetworkError, httpx.HTTPError, RuntimeError) as exc:
        return {**preview, "would": "invalid_request", "reason": str(exc)}
    return {
        **preview,
        "existing": object_view(existing),
        "state_hash": state_hash(existing),
        "would": "create" if existing is None else "change_if_different",
    }
