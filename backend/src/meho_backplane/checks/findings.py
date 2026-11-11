# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The investigator's finding: parse it, store it, mail it (#2507, #2721, #6183).

A finding is the structured answer of a checks investigator run: a verdict, a
summary, evidence and an optional advice text. MEHO stores it in tenant memory
as ``checks-noise-<group key>``, so a known cause stays quiet next time, and
mails it to the Dashboard's mail address.

Two callers use this module:

* the old investigator path (:mod:`meho_backplane.checks.investigate`), which
  starts the agent itself and waits for the answer;
* the event drain (:mod:`meho_backplane.events.drain`), through
  :func:`record_finding_for_event`. With ``CHECKS_AGENT_EVENTS`` on, an agent
  started by an ordinary event trigger has no step after its run. So the drain
  calls this module when such a run ends.

Like the investigator, this module imports no operations dispatcher. It reads
a stored run and writes memory and mail.
"""

from __future__ import annotations

import re
import uuid
from typing import cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.notify import FindingNotice, schedule_finding_notification
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRun, AgentRunStatus, CheckDashboard
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.settings import get_settings

__all__ = [
    "NOISE_SLUG_PREFIX",
    "ChecksFinding",
    "finding_notice",
    "investigator_operator",
    "parse_finding",
    "persist_finding",
    "record_finding_for_event",
    "render_finding_body",
]

#: Slug prefix for every noise-suppression policy entry. The suppress decision
#: reads the entry's *metadata* (``re_escalate == false`` -> suppress), decided
#: before any model call -- never body prose.
NOISE_SLUG_PREFIX = "checks-noise-"

#: ``metadata.source`` stamp on every policy entry, so a later audit tells
#: investigator-written entries from operator hand-edits or other automation.
_MEMORY_SOURCE = "checks-investigator"

#: Synthetic subject for the per-tenant operator that writes the memory entry,
#: so audit rows name the check investigator, not a real user.
_INVESTIGATOR_SUB = "__checks_investigator__"

#: The run key of a run that an event trigger started for a Dashboard event:
#: ``event:<trigger id>:c:dashboard:<dashboard id>:<group key>``. The format is
#: made by ``events.matcher._coalesce_work_ref`` from the event's
#: ``coalesce_key`` (``checks.events.publish_dashboard_changed``).
_DASHBOARD_RUN_KEY = re.compile(
    r"^event:(?P<trigger>[0-9a-f-]{36}):c:dashboard:(?P<dashboard>[0-9a-f-]{36}):(?P<group>.+)$"
)


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (see ``checks.investigate._log``)."""
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


class ChecksFinding(BaseModel):
    """Structured output of one investigation run.

    Mirrors the r1 ``PolicyDecision`` minus its model-chosen ``alert_class``
    (suppression here keys on the deterministic topology group key, decided
    before any model call). Because
    :meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled` does not
    synthesise a persisted ``output_schema`` into a Pydantic ``output_type``, the
    terminal run's answer arrives as free text projected to ``{"text": ...}``;
    this wiring parses that text into a :class:`ChecksFinding` caller-side.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str = Field(pattern=r"^(benign|acknowledged|actionable)$")
    re_escalate: bool
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


def finding_notice(
    finding: ChecksFinding,
    *,
    dashboard_id: uuid.UUID,
    dashboard_name: str,
    recipient: str | None,
    run_id: uuid.UUID,
) -> FindingNotice:
    """Project a finding into the notifier's input (#2721).

    ``recommended_action`` rides along only for an ``actionable`` verdict --
    the same gate :func:`render_finding_body` applies to the memory entry,
    so the mail and the memory entry cannot disagree about whether there is
    an action to take.
    """
    return FindingNotice(
        dashboard_id=dashboard_id,
        dashboard_name=dashboard_name,
        run_id=run_id,
        verdict=finding.verdict,
        summary=finding.summary,
        evidence=tuple(finding.evidence),
        recommended_action=(
            finding.recommended_action if finding.verdict == "actionable" else None
        ),
        recipient=recipient,
    )


def parse_finding(output: dict[str, object] | None, *, run_id: uuid.UUID) -> ChecksFinding | None:
    """Parse a terminal run's output into a :class:`ChecksFinding`, or ``None``.

    ``run_scheduled`` projects a free-text answer as ``{"text": <json>}`` (it
    does not map a stored ``output_schema`` to an ``output_type``), so the JSON
    lives in ``output["text"]``; a genuinely structured dict is accepted
    directly. A non-JSON / schema-mismatched answer logs
    ``checks_investigation_unparseable`` and skips the write.
    """
    if not isinstance(output, dict):
        _log().warning("checks_investigation_unparseable", run_id=str(run_id), reason="no_output")
        return None
    text = output.get("text")
    try:
        if isinstance(text, str):
            return ChecksFinding.model_validate_json(text)
        return ChecksFinding.model_validate(output)
    except ValidationError:
        _log().warning("checks_investigation_unparseable", run_id=str(run_id))
        return None


async def persist_finding(
    memory: MemoryService,
    operator: Operator,
    group_key: str,
    finding: ChecksFinding,
    *,
    run_id: uuid.UUID,
) -> None:
    """Write the finding back to tenant memory as the noise-suppression policy.

    Idempotent on ``(scope, slug)`` -- ``MemoryService.remember`` upserts, so a
    re-investigation of the same group overwrites rather than duplicates. The
    metadata carries the load-bearing suppression fields (``re_escalate``) plus
    provenance (``source``, ``verdict``, ``run_id``).
    """
    await memory.remember(
        operator,
        MemoryScope.TENANT,
        render_finding_body(finding),
        slug=f"{NOISE_SLUG_PREFIX}{group_key}",
        metadata={
            "source": _MEMORY_SOURCE,
            "verdict": finding.verdict,
            "re_escalate": finding.re_escalate,
            "run_id": str(run_id),
        },
    )


def render_finding_body(finding: ChecksFinding) -> str:
    """Render a :class:`ChecksFinding` into the memory body (r1 render mould).

    Fixed sections so a future read parses deterministically: verdict +
    ``re_escalate`` header, ``## Summary``, optional ``## Evidence`` (fenced so
    BM25 ranks the literal fragments), and ``## Recommended action`` only when
    the verdict is ``actionable`` -- diagnose-only advisory text, never executed.
    """
    parts: list[str] = [
        f"verdict: {finding.verdict}",
        f"re_escalate: {str(finding.re_escalate).lower()}",
        "",
        "## Summary",
        "",
        finding.summary.rstrip(),
    ]
    if finding.evidence:
        parts.append("")
        parts.append("## Evidence")
        parts.append("")
        parts.extend(f"- `{line}`" for line in finding.evidence)
    if finding.verdict == "actionable" and finding.recommended_action:
        parts.append("")
        parts.append("## Recommended action")
        parts.append("")
        parts.append(finding.recommended_action.rstrip())
    parts.append("")
    return "\n".join(parts)


def investigator_operator(tenant_id: uuid.UUID) -> Operator:
    """Build the synthetic per-tenant operator the investigation runs reads as.

    ``raw_jwt`` is empty (no token forwarded downstream) and the scope is
    exactly *tenant_id* -- no cross-tenant reach, the #2505 runner precedent.
    Role is ``TENANT_ADMIN`` because the closed-loop noise-suppression write is a
    ``TENANT``-scoped (tenant-shared) memory write, which the memory RBAC matrix
    restricts to ``tenant_admin``. This operator only reads topology and
    reads/writes memory; it never dispatches an op -- the agent run authenticates
    independently as its own principal, so no execution path inherits this role.
    """
    return Operator(
        sub=_INVESTIGATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def record_finding_for_event(payload: dict[str, object]) -> bool:
    """Store and mail the finding of a run that a Dashboard event started (#6183).

    *payload* is the payload of an ``agent_run.completed`` event. Returns
    ``True`` when a finding was stored. Does nothing (``False``) when the
    switch is off, the run did not succeed, the run key is not the Dashboard
    form, the Dashboard is not in the run's tenant, or the answer is not a
    finding (any agent may answer in another shape). Never raises, so a bad
    run cannot stop the drain loop.
    """
    try:
        return await _record_finding_for_event(payload)
    except Exception:
        _log().warning("checks_event_finding_failed", run_id=payload.get("run_id"), exc_info=True)
        return False


def _parse_run_key(
    payload: dict[str, object],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str] | None:
    """Return ``(run id, tenant id, dashboard id, group key)`` for a Dashboard run.

    ``None`` when the run did not succeed, the run key is not the Dashboard
    form, or an id is not a UUID.
    """
    work_ref = payload.get("work_ref")
    if payload.get("status") != AgentRunStatus.SUCCEEDED.value or not isinstance(work_ref, str):
        return None
    match = _DASHBOARD_RUN_KEY.match(work_ref)
    if match is None:
        return None
    try:
        return (
            uuid.UUID(str(payload.get("run_id"))),
            uuid.UUID(str(payload.get("tenant_id"))),
            uuid.UUID(match.group("dashboard")),
            match.group("group"),
        )
    except ValueError:
        return None


async def _load_run_output_and_dashboard(
    run_id: uuid.UUID, tenant_id: uuid.UUID, dashboard_id: uuid.UUID
) -> tuple[dict[str, object] | None, str, str | None] | None:
    """Read the run's answer and the Dashboard's name and mail address.

    ``None`` when the run or the Dashboard is not in *tenant_id*.
    """
    async with get_sessionmaker()() as session:
        run = await session.get(AgentRun, run_id)
        if run is None or run.tenant_id != tenant_id:
            return None
        dashboard = await session.get(CheckDashboard, dashboard_id)
        if dashboard is None or dashboard.tenant_id != tenant_id:
            _log().info(
                "checks_event_finding_no_dashboard",
                run_id=str(run_id),
                dashboard_id=str(dashboard_id),
            )
            return None
        return run.output, dashboard.name, dashboard.notify_email


async def _record_finding_for_event(payload: dict[str, object]) -> bool:
    if not get_settings().checks_agent_events:
        return False
    parsed = _parse_run_key(payload)
    if parsed is None:
        return False
    run_id, tenant_id, dashboard_id, group_key = parsed
    loaded = await _load_run_output_and_dashboard(run_id, tenant_id, dashboard_id)
    if loaded is None:
        return False
    output, dashboard_name, recipient = loaded
    finding = parse_finding(output, run_id=run_id)
    if finding is None:
        return False
    await persist_finding(
        MemoryService(),
        investigator_operator(tenant_id),
        group_key,
        finding,
        run_id=run_id,
    )
    _log().info(
        "checks_event_finding_recorded",
        run_id=str(run_id),
        group_key=group_key,
        verdict=finding.verdict,
        re_escalate=finding.re_escalate,
        tenant_id=str(tenant_id),
    )
    schedule_finding_notification(
        finding_notice(
            finding,
            dashboard_id=dashboard_id,
            dashboard_name=dashboard_name,
            recipient=recipient,
            run_id=run_id,
        )
    )
    return True
