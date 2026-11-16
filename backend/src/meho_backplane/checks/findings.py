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
  calls this module when such a run ends. It checks the stored run, its
  trigger and the Dashboard event that started it before it writes anything.

Like the investigator, this module imports no operations dispatcher. It reads
a stored run and writes memory and mail.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.events import CHECK_DASHBOARD_CHANGED
from meho_backplane.checks.notify import FindingNotice, schedule_finding_notification
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    CheckDashboard,
    EventOutbox,
    ScheduledTrigger,
    ScheduledTriggerKind,
)
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

#: The run key of a run that an event trigger started for a keyed event:
#: ``event:<trigger id>:c:<coalesce key>`` (``events.matcher._coalesce_work_ref``).
#: A Dashboard event's key is ``dashboard:<dashboard id>:<group key>``
#: (``checks.events.publish_dashboard_changed``), but a key over 200 characters
#: is stored as a hash. So the Dashboard and the group come from the event.
_EVENT_RUN_KEY = re.compile(r"^event:(?P<trigger>[0-9a-f-]{36}):c:(?P<key>.+)$")

#: A coalesce key that the matcher stored as a hash: the first 32 characters
#: of a SHA-256 hex digest (``events.matcher._coalesce_work_ref``).
_HASHED_KEY = re.compile(r"^[0-9a-f]{32}$")

#: Run sources of a run that a trigger started. A direct run, or a run that
#: another agent started, never counts as a Dashboard event run.
_TRIGGER_RUN_SOURCES = frozenset({AgentRunTrigger.SCHEDULED.value, AgentRunTrigger.EVENT.value})


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
    switch is off, the run did not succeed, a Dashboard event trigger did not
    start the run (:func:`_load_dashboard_run`), the answer is not a finding
    (any agent may answer in another shape), or this run's finding is already
    stored (a replayed event). Never raises, so a bad run cannot stop the
    drain loop.
    """
    try:
        return await _record_finding_for_event(payload)
    except Exception:
        _log().warning("checks_event_finding_failed", run_id=payload.get("run_id"), exc_info=True)
        return False


@dataclass(frozen=True)
class _DashboardRun:
    """A succeeded run that an event trigger started for a Dashboard event."""

    output: dict[str, object] | None
    order: tuple[datetime, uuid.UUID]
    dashboard_id: uuid.UUID
    dashboard_name: str
    recipient: str | None
    group_key: str


def _payload_ids(payload: dict[str, object]) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Return ``(run id, tenant id)`` of a succeeded run, else ``None``."""
    if payload.get("status") != AgentRunStatus.SUCCEEDED.value:
        return None
    try:
        return uuid.UUID(str(payload.get("run_id"))), uuid.UUID(str(payload.get("tenant_id")))
    except ValueError:
        return None


async def _load_dashboard_run(run_id: uuid.UUID, tenant_id: uuid.UUID) -> _DashboardRun | None:
    """Load the run, but only when a Dashboard event trigger started it.

    The stored run, not the event payload, is the source. ``None`` unless all
    of these hold:

    * the run is in *tenant_id*, succeeded, and a trigger started it (never a
      direct run or a run that another agent started);
    * its run key names an event trigger of the same tenant and agent, and no
      cron or one-off trigger of the tenant carries that run key;
    * a ``check.dashboard.changed`` event with a cause group gives exactly
      this run key for that trigger and passes its filter.

    The Dashboard and the group key come from that event, so a long group key
    that the run key holds only as a hash still works.
    """
    async with get_sessionmaker()() as session:
        run = await session.get(AgentRun, run_id)
        if (
            run is None
            or run.tenant_id != tenant_id
            or run.status != AgentRunStatus.SUCCEEDED.value
            or run.trigger not in _TRIGGER_RUN_SOURCES
            or run.work_ref is None
        ):
            return None
        trigger = await _event_trigger_for_run(session, run, run.work_ref)
        if trigger is None:
            return None
        cause = await _cause_event(session, trigger, run.work_ref)
        if cause is None:
            _log().info("checks_event_finding_no_cause", run_id=str(run_id))
            return None
        dashboard_id, group_key = cause
        dashboard = await session.get(CheckDashboard, dashboard_id)
        if dashboard is None or dashboard.tenant_id != tenant_id:
            _log().info(
                "checks_event_finding_no_dashboard",
                run_id=str(run_id),
                dashboard_id=str(dashboard_id),
            )
            return None
        return _DashboardRun(
            output=run.output,
            order=_run_order(run),
            dashboard_id=dashboard_id,
            dashboard_name=dashboard.name,
            recipient=dashboard.notify_email,
            group_key=group_key,
        )


async def _event_trigger_for_run(
    session: AsyncSession, run: AgentRun, work_ref: str
) -> ScheduledTrigger | None:
    """Return the event trigger the run key names, if it really owns the run."""
    match = _EVENT_RUN_KEY.match(work_ref)
    if match is None:
        return None
    try:
        trigger_id = uuid.UUID(match.group("trigger"))
    except ValueError:
        return None
    trigger = await session.get(ScheduledTrigger, trigger_id)
    if (
        trigger is None
        or trigger.tenant_id != run.tenant_id
        or trigger.kind != ScheduledTriggerKind.EVENT.value
        or trigger.agent_definition_id != run.agent_definition_id
    ):
        return None
    # A cron or one-off trigger may carry any run key, so one that copies
    # this key could start the same agent. Such a run is not trusted.
    copied = await session.execute(
        select(ScheduledTrigger.id)
        .where(
            ScheduledTrigger.tenant_id == run.tenant_id,
            ScheduledTrigger.kind != ScheduledTriggerKind.EVENT.value,
            ScheduledTrigger.work_ref == work_ref,
        )
        .limit(1)
    )
    if copied.scalar_one_or_none() is not None:
        return None
    return trigger


async def _cause_event(
    session: AsyncSession, trigger: ScheduledTrigger, work_ref: str
) -> tuple[uuid.UUID, str] | None:
    """Find the Dashboard event that gave *work_ref*: ``(dashboard id, group key)``.

    The search keeps only the tenant's ``check.dashboard.changed`` events with
    the run key's coalesce key, so other events never push the cause out of
    it. A hashed key cannot be searched for, so then every event with a key
    over 200 characters is checked. The run key is made the way the event
    matcher makes it, so the check is the same in both cases.
    """
    # Imported here: the matcher pulls in the agent runtime.
    from meho_backplane.events.matcher import (
        _COALESCE_KEY_MAX_LEN,
        _coalesce_work_ref,
        _payload_contains,
    )

    match = _EVENT_RUN_KEY.match(work_ref)
    if match is None:
        return None
    key = match.group("key")
    event_key = func.trim(EventOutbox.payload["coalesce_key"].as_string())
    same_key = event_key == key
    if _HASHED_KEY.match(key):
        same_key = or_(same_key, func.length(event_key) > _COALESCE_KEY_MAX_LEN)
    events = await session.execute(
        select(EventOutbox)
        .where(
            EventOutbox.tenant_id == trigger.tenant_id,
            EventOutbox.event_kind == CHECK_DASHBOARD_CHANGED,
            same_key,
        )
        .order_by(EventOutbox.event_id.desc())
    )
    for event in events.scalars():
        if _coalesce_work_ref(trigger, event) != work_ref:
            continue
        if not _payload_contains(event.payload, trigger.event_filter or {}):
            continue
        return _dashboard_and_group(event.payload)
    return None


def _dashboard_and_group(payload: dict[str, object]) -> tuple[uuid.UUID, str] | None:
    """Read ``(dashboard id, group key)`` from a Dashboard event payload."""
    source = payload.get("source")
    cause_group = payload.get("cause_group")
    if not isinstance(source, dict) or not isinstance(cause_group, dict):
        return None
    group_key = cause_group.get("key")
    if not isinstance(group_key, str) or not group_key:
        return None
    try:
        return uuid.UUID(str(source.get("id"))), group_key
    except ValueError:
        return None


def _run_order(run: AgentRun) -> tuple[datetime, uuid.UUID]:
    """Order runs by when they ended; the run id breaks a tie."""
    return run.ended_at or run.created_at, run.id


async def _same_or_newer_finding_stored(
    memory: MemoryService, operator: Operator, group_key: str, loaded: _DashboardRun
) -> bool:
    """``True`` when the group's stored finding is from this run or a later one.

    The drain marks an event processed only after this step, so a restart or
    a failed commit delivers the same ``agent_run.completed`` event again,
    maybe after another run stored a finding for the group. The stored entry
    keeps its run id. When that run ended at the same time as this run or
    later, this run's finding is already sent or is older. So a second
    delivery stores and mails nothing, and never replaces a newer finding.
    """
    entry = await memory.recall(operator, MemoryScope.TENANT, f"{NOISE_SLUG_PREFIX}{group_key}")
    if entry is None:
        return False
    try:
        stored_run_id = uuid.UUID(str(entry.metadata.get("run_id")))
    except ValueError:
        return False
    async with get_sessionmaker()() as session:
        stored_run = await session.get(AgentRun, stored_run_id)
        if stored_run is None:
            return False
        return _run_order(stored_run) >= loaded.order


async def _record_finding_for_event(payload: dict[str, object]) -> bool:
    if not get_settings().checks_agent_events:
        return False
    ids = _payload_ids(payload)
    if ids is None:
        return False
    run_id, tenant_id = ids
    loaded = await _load_dashboard_run(run_id, tenant_id)
    if loaded is None:
        return False
    finding = parse_finding(loaded.output, run_id=run_id)
    if finding is None:
        return False
    memory = MemoryService()
    operator = investigator_operator(tenant_id)
    if await _same_or_newer_finding_stored(memory, operator, loaded.group_key, loaded):
        _log().info("checks_event_finding_already_recorded", run_id=str(run_id))
        return False
    await persist_finding(memory, operator, loaded.group_key, finding, run_id=run_id)
    _log().info(
        "checks_event_finding_recorded",
        run_id=str(run_id),
        group_key=loaded.group_key,
        verdict=finding.verdict,
        re_escalate=finding.re_escalate,
        tenant_id=str(tenant_id),
    )
    schedule_finding_notification(
        finding_notice(
            finding,
            dashboard_id=loaded.dashboard_id,
            dashboard_name=loaded.dashboard_name,
            recipient=loaded.recipient,
            run_id=run_id,
        )
    )
    return True
