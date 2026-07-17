# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tiered-triage investigator wiring for Dashboard non-green transitions (#2507).

Task #2507 under Initiative #2416 (parent goal #221). Productises the
``examples/r1-tiered-triage`` mould against the deterministic check layer
(#2503-#2506): when a Dashboard's five-state rollup crosses from green into a
non-green state, correlate the affected non-green Sensors through the topology
blast-radius graph so one underlying cause produces exactly **one**
investigation, check tenant memory for a known-noise policy that suppresses
re-escalation, and -- only for novel, non-suppressed groups -- fire a real,
durable, budget-gated agent run via
:meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled`.

Where the mould goes
====================

The r1 harness (``examples/r1-tiered-triage/workflow.py``) drives an in-process
``PydanticAgentRun`` whose *cheap tier* is an LLM classifier. Here the cheap
tier is **replaced by the deterministic layer** (#2503-#2506): the check-runner
(#2505) already evaluates every Sensor with no model in the path, and #2506's
pure rollup fold already answers "is this Dashboard OK?". So there is no
``invoke_agent`` hop -- the deep tier is invoked directly through the same
production seam the cron scheduler uses (``scheduler/loop.py`` step 4), which
gives, for free: the durable pollable ``agent_run`` row (agent-as-subject audit
provenance), the pre-run budget gate + kill switch, bounded waiting, and
``work_ref`` stamping.

The trigger seam
================

There is no event machinery to ride (``kind=event`` trigger creation is refused
until the #826 matcher lands, #2325) and #2506's rollup is read-path-only by
decision, so the seam is a **hook at #2505's runner result-persist path**:
:func:`investigate_on_transition` is called immediately after every
``record_sensor_result`` persist. It recomputes the rollup for exactly the
Dashboards containing the just-evaluated Sensor, compares against the
``check_dashboards.last_rollup_state`` memo (#2506 shipped it UNWRITTEN and
reserved for this Task; this hook is its only writer), and updates the memo.
Only a **worsening** transition into a non-green state escalates -- improving or
unchanged states just maintain the memo. Every evaluation is processed (not only
state changes) because a ``for:`` hold expiring flips a Dashboard non-green with
no sensor-state change; the memo-equality check is the cheap exit.

Diagnose-only (the CRUX security property)
==========================================

This wiring **never executes a change op**. It reads topology, reads/writes
tenant memory, and invokes a diagnose-only-configured agent whose structured
finding (verdict, summary, evidence, suggested action) lands in the durable
``agent_run`` row and is written back to memory as the noise-suppression policy.
``recommended_action`` is persisted as text only. Any write op the *agent*
itself attempts parks in the existing approval queue (#817) via the policy gate
-- this module imports no operations dispatcher and calls no execution seam.

Identity
========

Topology reads and memory read/write run under a synthetic per-tenant
:class:`~meho_backplane.auth.operator.Operator` (:func:`_investigator_operator`),
confined to the Sensor's own tenant -- no cross-tenant reach, no JWT forwarded,
the #2505 runner precedent. The role is ``TENANT_ADMIN`` (not ``OPERATOR``)
because the closed-loop noise-suppression policy is a **tenant-shared** memory
write, and the memory RBAC matrix restricts ``TENANT``-scope writes to
``tenant_admin`` (``memory/rbac.py``). This synthetic operator never dispatches
an op; the actual agent run authenticates independently as its own principal
via ``client_credentials`` (agent-as-subject), so no privilege elevation crosses
into an execution path.

Containment
===========

Every failure mode -- unconfigured agent, unresolved credentials, budget
refusal, unparseable output, topology miss, task crash -- is contained so the
runner's result-persist path never fails because of this hook. The expensive
work (topology correlation + agent run) is a fire-and-forget :class:`asyncio.Task`
anchored in a module-level strong-reference set, so the runner's persist path
neither blocks nor raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentInvocationError,
    AgentNotFoundError,
    AgentRunOutcome,
    AgentRunStatusView,
    get_agent_invoker,
)
from meho_backplane.agent.run import BudgetExceededError
from meho_backplane.auth.agent_token import AgentTokenError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.assertions import CheckState
from meho_backplane.checks.dashboard_repository import members_by_dashboard
from meho_backplane.checks.rollup import (
    MemberEvaluation,
    MemberState,
    evaluate_member,
    fold,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    resolve_agent_credentials,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import find_dependencies
from meho_backplane.topology.resolvers import AmbiguousNodeError, NodeNotFoundError

__all__ = [
    "ChecksFinding",
    "investigate_on_transition",
    "run_investigation",
]

#: Slug prefix for every noise-suppression policy entry this wiring writes to
#: tenant memory (mirrors ``POLICY_SLUG_PREFIX`` in the r1 mould). The suppress
#: decision reads the entry's *metadata* (``re_escalate == false`` -> suppress),
#: decided before any model call -- never body prose.
_NOISE_SLUG_PREFIX = "checks-noise-"

#: ``work_ref`` prefix stamped onto every fired ``agent_run`` -- the
#: change-ticket reference the runs list filters on and the in-flight dedupe
#: keys on. Shape: ``checks:<dashboard-slug>:<group-key>``. Unlike a memory
#: slug, ``work_ref`` is a free Text column so ``:`` separators are legal.
_WORK_REF_PREFIX = "checks"

#: ``metadata.source`` stamp on every policy entry this wiring writes, so a
#: later audit / promote sweep tells investigator-written entries from operator
#: hand-edits or other automation.
_MEMORY_SOURCE = "checks-investigator"

#: The topology node ``kind`` a registered target manifests as
#: (:data:`~meho_backplane.db.models.WELL_KNOWN_NODE_KINDS`); the correlation
#: anchor lookup pins ``resolve_node`` on ``(tenant_id, "target", name)``.
_TARGET_NODE_KIND = "target"

#: Synthetic subject for the investigator's per-tenant operator -- the
#: ``_system_operator`` mould with a dedicated sub so audit rows attribute the
#: topology / memory reads to the check-investigator, not a real user.
_INVESTIGATOR_SUB = "__checks_investigator__"

#: Fixed poll cadence for the bounded post-``run_scheduled`` wait when a run
#: outlives the sync timeout (converts to async). A dumb constant, same posture
#: as the runner's fixed bounds.
_POLL_INTERVAL_SECONDS = 5.0

#: The three non-terminal ``agent_run`` statuses. An in-flight run in any of
#: these states for the same ``(tenant_id, work_ref)`` suppresses a duplicate
#: fire (an ``awaiting_approval`` run is still in flight -- do not re-fire it).
_NON_TERMINAL_STATUSES: tuple[AgentRunStatus, ...] = (
    AgentRunStatus.PENDING,
    AgentRunStatus.RUNNING,
    AgentRunStatus.AWAITING_APPROVAL,
)

#: The three terminal ``agent_run`` statuses the bounded poll waits for.
_TERMINAL_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
)

#: Worsening-transition rank. Only ``degraded`` / ``critical`` are firing
#: targets; ``ok`` / ``skip`` / ``unknown`` rank 0 (a Dashboard losing all
#: members -> ``unknown`` or going all-paused -> ``skip`` is not a symptom to
#: investigate). A transition fires iff ``rank[current] > rank[previous]`` and
#: ``current`` is a firing target -- exactly the matrix on #2507.
_FIRE_RANK: dict[str, int] = {
    "ok": 0,
    "skip": 0,
    "unknown": 0,
    "degraded": 1,
    "critical": 2,
}

#: Characters not admitted by ``memory.schemas.SLUG_PATTERN``
#: (``[A-Za-z0-9_\\-\\.]``). Runs collapse to a single ``-`` in :func:`_slugify`.
_SLUGIFY_RE = re.compile(r"[^a-z0-9_.-]+")

#: Per-process strong references to in-flight investigation tasks. ``asyncio``
#: holds only weak references to bare tasks, so without this set a
#: fire-and-forget investigation could be GC'd mid-flight (the run-store
#: rationale in ``agent/invocation.py``). The done-callback discards each entry
#: on completion so a long-lived worker does not accumulate finished tasks.
_INVESTIGATIONS: set[asyncio.Task[None]] = set()


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    A module-level ``structlog.get_logger(__name__)`` proxy caches its bound
    methods on first use under the production ``cache_logger_on_first_use=True``
    config; a later :func:`structlog.testing.capture_logs` in the same worker
    then cannot reach the orphaned closure, so log assertions flake under
    pytest-xdist ``loadscope``. Resolving per call reads the live config each
    time (the discipline #2505's runner adopted).
    """
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


@dataclass(frozen=True, slots=True)
class _MemberSnapshot:
    """A detached view of one non-green member Sensor the briefing needs.

    Captured while the member row is fresh in the transition session, so the
    backgrounded investigation (which opens its own sessions) never touches an
    expired ORM object.
    """

    sensor_id: uuid.UUID
    name: str
    connector_id: str
    op_id: str
    target: dict[str, object] | None
    severity: str
    raw_state: str
    effective_state: str
    last_value: object
    last_evidence: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class _DashboardSnapshot:
    """A detached view of the transitioning Dashboard the briefing / work_ref need."""

    dashboard_id: uuid.UUID
    name: str
    slug: str
    previous_state: str
    current_state: str


@dataclass(frozen=True, slots=True)
class _CauseGroup:
    """One correlated cause: the Sensors a single investigation covers.

    :attr:`key` is the deterministic group key (the deepest shared topology
    node, else the lone Sensor's slug); it is both the ``work_ref`` group
    segment and the noise-suppression slug suffix, so the two cannot drift.
    """

    key: str
    members: tuple[_MemberSnapshot, ...]


async def investigate_on_transition(
    *,
    sensor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    """Detect Dashboard green->non-green transitions off one Sensor persist.

    The public entry point #2505's runner calls immediately after every
    ``record_sensor_result``. **Never raises** -- the runner's result-persist
    path must not fail because of this hook. The in-band work (memo
    compare-and-set) is cheap; the expensive investigation is spawned as a
    fire-and-forget task so this call returns promptly.

    Args:
        sensor_id: The Sensor whose evaluation was just persisted.
        tenant_id: The Sensor's tenant (every read/write here is scoped to it).
        now: Injection point for the rollup's hysteresis clock; defaults to the
            current instant (the read-path default #2506 uses).
    """
    try:
        await _process_transition(sensor_id=sensor_id, tenant_id=tenant_id, now=now)
    except Exception:
        # Contract: never propagate to the runner's persist path.
        _log().warning(
            "checks_investigation_failed",
            sensor_id=str(sensor_id),
            tenant_id=str(tenant_id),
            exc_info=True,
        )


async def _process_transition(
    *,
    sensor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    now: datetime | None,
) -> None:
    """Recompute affected Dashboards, maintain the memo, spawn investigations.

    For every Dashboard containing *sensor_id*: fold its members through #2506's
    pure rollup against *now*, compare the result to the persisted
    ``last_rollup_state`` (NULL treated as ``ok``), and write the memo when it
    changed. A **worsening** transition into ``degraded`` / ``critical`` with at
    least one actively-failing member schedules a background investigation.
    """
    now = now or datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    pending: list[tuple[_DashboardSnapshot, list[_MemberSnapshot]]] = []
    async with sessionmaker() as session:
        dashboards = await _dashboards_for_sensor(session, tenant_id=tenant_id, sensor_id=sensor_id)
        if not dashboards:
            return
        grouped = await members_by_dashboard(session, dashboard_ids=[d.id for d in dashboards])
        for dashboard in dashboards:
            members = grouped.get(dashboard.id, [])
            evaluations = [(s, evaluate_member(_member_state(s), now)) for s in members]
            current = fold([e for _, e in evaluations])
            previous = dashboard.last_rollup_state or "ok"
            if dashboard.last_rollup_state != current:
                # Memo compare-and-set: the memo tracks the freshly computed
                # state so a still-non-green Dashboard fires exactly once (on
                # the edge), not every tick.
                dashboard.last_rollup_state = current
            if _is_worsening(previous, current):
                non_green = [
                    _member_snapshot(s, ev)
                    for s, ev in evaluations
                    if ev.effective_state not in ("ok", "skip")
                ]
                if non_green:
                    pending.append((_dashboard_snapshot(dashboard, previous, current), non_green))
        await session.commit()

    # Memo writes committed; spawn the expensive work off the persist path.
    for dashboard_snapshot, non_green in pending:
        _schedule_investigation(
            tenant_id=tenant_id, dashboard=dashboard_snapshot, members=non_green
        )


async def _dashboards_for_sensor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
) -> list[CheckDashboard]:
    """Return the tenant's Dashboards that reference *sensor_id*.

    Reverse membership lookup over the association table, riding
    ``check_dashboard_sensors_sensor_idx``. Tenant-scoped so a cross-tenant
    membership can never surface another tenant's Dashboard.
    """
    stmt = (
        select(CheckDashboard)
        .join(CheckDashboardSensor, CheckDashboardSensor.dashboard_id == CheckDashboard.id)
        .where(
            CheckDashboardSensor.sensor_id == sensor_id,
            CheckDashboard.tenant_id == tenant_id,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _member_state(sensor: Sensor) -> MemberState:
    """Project a ``sensor`` row into the fold-relevant :class:`MemberState`.

    Mirrors ``dashboard_service._member_state`` -- replicated here rather than
    imported so this module does not couple to a sibling's private helper.
    ``last_state`` is CHECK-constrained to the five-state vocabulary at the DB,
    so the narrowing cast is honest.
    """
    return MemberState(
        last_state=cast(CheckState, sensor.last_state),
        status=sensor.status,
        severity=sensor.severity,
        for_seconds=sensor.for_seconds,
        last_evaluated_at=sensor.last_evaluated_at,
        next_fire_at=sensor.next_fire_at,
        state_since=sensor.state_since,
    )


def _member_snapshot(sensor: Sensor, evaluation: MemberEvaluation) -> _MemberSnapshot:
    """Detach a non-green member Sensor into the briefing snapshot."""
    return _MemberSnapshot(
        sensor_id=sensor.id,
        name=sensor.name,
        connector_id=sensor.connector_id,
        op_id=sensor.op_id,
        target=sensor.target,
        severity=sensor.severity,
        raw_state=evaluation.raw_state,
        effective_state=evaluation.effective_state,
        last_value=sensor.last_value,
        last_evidence=sensor.last_evidence,
    )


def _dashboard_snapshot(
    dashboard: CheckDashboard, previous: str, current: str
) -> _DashboardSnapshot:
    """Detach the transitioning Dashboard into the briefing / work_ref snapshot."""
    return _DashboardSnapshot(
        dashboard_id=dashboard.id,
        name=dashboard.name,
        slug=_slugify(dashboard.name),
        previous_state=previous,
        current_state=current,
    )


def _is_worsening(previous: str, current: str) -> bool:
    """Return ``True`` iff *current* is a strictly worse non-green state.

    Fires on ``ok``/``skip`` -> ``degraded``/``critical`` and ``degraded`` ->
    ``critical``; never on an improving edge (``critical`` -> ``degraded``), an
    unchanged state, or a drop to ``unknown`` / ``skip``.
    """
    return _FIRE_RANK.get(current, 0) > _FIRE_RANK.get(previous, 0) and current in (
        "degraded",
        "critical",
    )


def _schedule_investigation(
    *,
    tenant_id: uuid.UUID,
    dashboard: _DashboardSnapshot,
    members: list[_MemberSnapshot],
) -> None:
    """Spawn the correlated investigation as a tracked fire-and-forget task."""
    task = asyncio.create_task(
        _guarded_investigation(tenant_id=tenant_id, dashboard=dashboard, members=members),
        name=f"checks-investigate-{dashboard.dashboard_id}",
    )
    _INVESTIGATIONS.add(task)
    task.add_done_callback(_INVESTIGATIONS.discard)


async def _guarded_investigation(
    *,
    tenant_id: uuid.UUID,
    dashboard: _DashboardSnapshot,
    members: list[_MemberSnapshot],
) -> None:
    """Run one investigation with a top-level guard so a crash cannot leak.

    ``CancelledError`` propagates so lifespan shutdown tears the task down
    cleanly; any other exception is logged and swallowed (the containment
    contract). Per-group errors are already contained inside
    :func:`_investigate_group`; this is the belt-and-braces backstop.
    """
    try:
        await run_investigation(tenant_id=tenant_id, dashboard=dashboard, members=members)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log().warning(
            "checks_investigation_failed",
            dashboard_id=str(dashboard.dashboard_id),
            tenant_id=str(tenant_id),
            exc_info=True,
        )


async def run_investigation(
    *,
    tenant_id: uuid.UUID,
    dashboard: _DashboardSnapshot,
    members: list[_MemberSnapshot],
) -> None:
    """Correlate the non-green members and investigate each cause group once.

    Public so the runner-scheduled task and tests share one entry. Correlation
    (topology forward-closure + union-find) collapses members sharing a common
    cause into one group; each group is investigated at most once (suppression +
    in-flight dedupe gate the actual agent fire).
    """
    operator = _investigator_operator(tenant_id)
    groups = await _correlate(operator, members)
    for group in groups:
        await _investigate_group(operator, tenant_id, dashboard, group)


async def _correlate(operator: Operator, members: list[_MemberSnapshot]) -> list[_CauseGroup]:
    """Group members by shared topology closure; one group per common cause.

    Each member maps to its topology anchor via its registered target's name
    (``kind="target"``); :func:`find_dependencies` returns the forward closure.
    Two members share a group iff their closures intersect (union-find handles
    transitivity). A member whose anchor is unresolvable -- no ``name`` in the
    target dict, or an untracked / ambiguous node (:class:`NodeNotFoundError` /
    :class:`AmbiguousNodeError`, the expected state for every non-k8s target
    today) -- has no closure and forms its own singleton, so correlation
    degrades gracefully to per-Sensor investigations when topology has no data.
    """
    closures: dict[uuid.UUID, dict[tuple[str, str], int] | None] = {}
    for member in members:
        closures[member.sensor_id] = await _closure_for(operator, member)

    ids = [m.sensor_id for m in members]
    parent: dict[uuid.UUID, uuid.UUID] = {sid: sid for sid in ids}

    def find(node: uuid.UUID) -> uuid.UUID:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a: uuid.UUID, b: uuid.UUID) -> None:
        parent[find(a)] = find(b)

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            ci, cj = closures[ids[i]], closures[ids[j]]
            if ci is not None and cj is not None and (ci.keys() & cj.keys()):
                union(ids[i], ids[j])

    by_member = {m.sensor_id: m for m in members}
    grouped: dict[uuid.UUID, list[_MemberSnapshot]] = {}
    for sid in ids:
        grouped.setdefault(find(sid), []).append(by_member[sid])

    groups = [
        _CauseGroup(key=_group_key(gmembers, closures), members=tuple(gmembers))
        for gmembers in grouped.values()
    ]
    # Deterministic order (the determinism the AC asserts).
    groups.sort(key=lambda g: g.key)
    return groups


async def _closure_for(
    operator: Operator, member: _MemberSnapshot
) -> dict[tuple[str, str], int] | None:
    """Return the member's forward topology closure, or ``None`` when unresolved.

    Keyed ``(kind, name) -> minimum depth``. ``None`` means "no anchor" -- the
    member forms a singleton group. Every failure is a graceful ``None``: an
    untracked / ambiguous anchor, or an unexpected topology error (logged).
    """
    anchor = _anchor_name(member.target)
    if anchor is None:
        return None
    try:
        nodes = await find_dependencies(operator, anchor, kind=_TARGET_NODE_KIND)
    except (NodeNotFoundError, AmbiguousNodeError):
        # The expected miss for every non-k8s / alias / target-less Sensor.
        return None
    except Exception:
        _log().warning(
            "checks_investigation_topology_error",
            anchor=anchor,
            tenant_id=str(operator.tenant_id),
            exc_info=True,
        )
        return None
    closure: dict[tuple[str, str], int] = {}
    for node in nodes:
        key = (node.kind, node.name)
        if key not in closure or node.depth < closure[key]:
            closure[key] = node.depth
    return closure


def _anchor_name(target: dict[str, object] | None) -> str | None:
    """Extract the topology anchor name from a Sensor's dispatch target.

    A Sensor references a registered target by name-or-alias under the ``name``
    key (the canonical dispatch-target shape). Only a non-empty string ``name``
    is a usable anchor; anything else (``None`` target, ``{"target_id": ...}``,
    a blank name) yields ``None`` -> singleton path.
    """
    if isinstance(target, dict):
        name = target.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _group_key(
    members: list[_MemberSnapshot],
    closures: dict[uuid.UUID, dict[tuple[str, str], int] | None],
) -> str:
    """Derive the deterministic key for one correlated group.

    When the group's resolved closures share a common node, the key is that
    shared node with the **maximal minimum depth** (the deepest common cause),
    ties broken lexicographically by ``(kind, name)`` -- slugified. Otherwise
    (a lone anchorless Sensor, or a transitively-linked group with no all-common
    node) the key falls back to the smallest member Sensor slug, so a singleton
    is keyed on its own Sensor (the AC's NodeNotFoundError case).
    """
    resolved = [closure for m in members if (closure := closures[m.sensor_id]) is not None]
    if resolved:
        common: set[tuple[str, str]] = set(resolved[0])
        for closure in resolved[1:]:
            common &= set(closure)
        if common:

            def _rank(node: tuple[str, str]) -> tuple[int, str, str]:
                min_depth = min(c[node] for c in resolved if node in c)
                # Largest min-depth first (negate), then (kind, name) ascending.
                return (-min_depth, node[0], node[1])

            best = min(common, key=_rank)
            # Append a digest of the original (kind, name) so two distinct
            # topology causes that slugify to the same string do not share a
            # suppression key (which would suppress one when the other is muted).
            return f"{_slugify(f'{best[0]}-{best[1]}')}-{_identity_digest(best[0], best[1])}"
    return min(_sensor_group_key(m) for m in members)


def _sensor_group_key(member: _MemberSnapshot) -> str:
    """Deterministic singleton key for an anchorless / uncorrelated Sensor."""
    return f"{_slugify(member.name)}-{member.sensor_id.hex[:8]}"


def _slugify(value: str) -> str:
    """Collapse *value* to the memory-slug alphabet (``[a-z0-9_.-]``).

    Lower-cases, replaces runs of out-of-alphabet characters with ``-``, and
    trims separators. Non-empty by construction so ``checks-noise-<key>`` always
    satisfies ``memory.schemas.SLUG_PATTERN``.
    """
    slug = _SLUGIFY_RE.sub("-", value.strip().lower()).strip("-.")
    return slug or "node"


def _identity_digest(*parts: str) -> str:
    """Short stable digest disambiguating a slug from its source identity.

    ``_slugify`` is lossy (``prod/db`` and ``prod db`` collapse to the same
    slug; a separator-only name becomes ``node``), so two distinct identities
    can otherwise share a suppression key / ``work_ref``. Appending this digest
    of the *original* parts makes the key collision-resistant while staying in
    the ``[a-z0-9_.-]`` slug alphabet.
    """
    raw = "\x00".join(parts).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:8]


async def _investigate_group(
    operator: Operator,
    tenant_id: uuid.UUID,
    dashboard: _DashboardSnapshot,
    group: _CauseGroup,
) -> None:
    """Suppress / dedupe / invoke / persist for one correlated cause group.

    Contains every failure so one group's problem never kills its siblings or
    the task. Order matters: suppression and in-flight dedupe both short-circuit
    **before** any agent fire, so a known-noise or already-running group records
    zero invocations.
    """
    settings = get_settings()
    group_key = group.key
    # Include the dashboard id so two dashboards whose names slugify to the same
    # value do not collide on ``work_ref`` (which would coalesce their unrelated
    # in-flight investigations); the readable slug is kept for operators.
    work_ref = f"{_WORK_REF_PREFIX}:{dashboard.dashboard_id.hex[:8]}:{dashboard.slug}:{group_key}"
    memory = MemoryService()
    try:
        if await _is_suppressed(memory, operator, group_key):
            _log().info(
                "checks_investigation_suppressed",
                work_ref=work_ref,
                group_key=group_key,
                tenant_id=str(tenant_id),
            )
            return
        if await _has_in_flight_run(tenant_id, work_ref):
            _log().info(
                "checks_investigation_skipped_in_flight",
                work_ref=work_ref,
                tenant_id=str(tenant_id),
            )
            return

        definition = await _load_investigator_definition(
            tenant_id, settings.checks_investigator_agent
        )
        if definition is None:
            _log().info(
                "checks_investigator_unconfigured",
                agent=settings.checks_investigator_agent,
                tenant_id=str(tenant_id),
            )
            return
        name, identity_ref = definition

        try:
            client_id, secret = await resolve_agent_credentials(identity_ref)
        except AgentCredentialsUnresolvedError as exc:
            _log().warning(
                "checks_investigation_credentials_unresolved",
                agent=name,
                identity_ref=identity_ref,
                reason=str(exc),
                tenant_id=str(tenant_id),
            )
            return

        outcome = await _fire_investigation(
            name=name,
            client_id=client_id,
            secret=secret,
            work_ref=work_ref,
            briefing=_build_briefing(dashboard, group),
            group_key=group_key,
            tenant_id=tenant_id,
        )
        if outcome is None:
            return

        finding = await _await_finding(operator, outcome)
        if finding is None:
            return
        await _persist_finding(memory, operator, group_key, finding, run_id=outcome.run_id)
        _log().info(
            "checks_investigation_recorded",
            run_id=str(outcome.run_id),
            group_key=group_key,
            verdict=finding.verdict,
            re_escalate=finding.re_escalate,
            tenant_id=str(tenant_id),
        )
    except Exception:
        _log().warning(
            "checks_investigation_failed",
            work_ref=work_ref,
            tenant_id=str(tenant_id),
            exc_info=True,
        )


async def _fire_investigation(
    *,
    name: str,
    client_id: str,
    secret: str,
    work_ref: str,
    briefing: str,
    group_key: str,
    tenant_id: uuid.UUID,
) -> AgentRunOutcome | None:
    """Invoke the investigator via ``run_scheduled``; ``None`` on a gated refusal.

    Every budget / kill-switch / unconfigured / token failure is caught and
    mapped to a structured log + a clean ``None`` (skip), so a refused fire never
    crash-loops the runner. The budget gate refuses **before** any ``agent_run``
    row is created (``budget_enforcement`` REFUSE-before-row contract).
    """
    invoker = get_agent_invoker()
    try:
        outcome = await invoker.run_scheduled(
            name,
            briefing,
            agent_client_id=client_id,
            agent_client_secret=SecretStr(secret),
            work_ref=work_ref,
        )
    except BudgetExceededError as exc:
        _log().warning(
            "checks_investigation_budget_refused",
            work_ref=work_ref,
            reason=exc.reason,
            tenant_id=str(tenant_id),
        )
        return None
    except (AgentNotFoundError, AgentDisabledError) as exc:
        # The definition vanished / was disabled between load and fire.
        _log().info(
            "checks_investigator_unconfigured",
            agent=name,
            reason=type(exc).__name__,
            tenant_id=str(tenant_id),
        )
        return None
    except (AgentInvocationError, AgentTokenError) as exc:
        _log().warning(
            "checks_investigation_failed",
            work_ref=work_ref,
            reason=type(exc).__name__,
            tenant_id=str(tenant_id),
        )
        return None
    _log().info(
        "checks_investigation_started",
        run_id=str(outcome.run_id),
        work_ref=work_ref,
        group_key=group_key,
        tenant_id=str(tenant_id),
    )
    return outcome


async def _is_suppressed(memory: MemoryService, operator: Operator, group_key: str) -> bool:
    """Return ``True`` iff a known-noise policy suppresses re-escalation.

    Reads the ``checks-noise-<group-key>`` tenant memory entry (if any) and
    suppresses iff its metadata ``re_escalate`` is falsey. A missing entry (novel
    red) or an ``re_escalate=true`` entry (still actionable) does not suppress.
    Decided on metadata, never body prose, so no LLM call is needed to decide.
    """
    slug = f"{_NOISE_SLUG_PREFIX}{group_key}"
    # Exact (scope, slug) lookup. A ``list_memories(slug_pattern=...)`` query is
    # a *substring* filter capped at a limit, so an exact suppression entry can
    # be pushed past the cap by unrelated slugs that merely contain this one --
    # silently failing to suppress a known-noise cause (a needless agent run).
    # ``recall`` resolves the single ``(TENANT, slug)`` entry the closed loop
    # writes via ``remember`` (same scope + slug), with no cap to hide behind.
    entry = await memory.recall(operator, MemoryScope.TENANT, slug)
    if entry is None:
        return False
    return _is_falsey(entry.metadata.get("re_escalate"))


def _is_falsey(value: object) -> bool:
    """Interpret a metadata ``re_escalate`` value as an explicit false.

    Robust to the bool round-trip through the memory index and to a stringified
    ``"false"`` an operator hand-edit might leave.
    """
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() == "false"


async def _has_in_flight_run(tenant_id: uuid.UUID, work_ref: str) -> bool:
    """Return ``True`` iff a non-terminal run already covers this work_ref.

    Rides ``agent_run_tenant_work_ref_idx`` on ``(tenant_id, work_ref)`` so a
    second transition for the same correlated group does not duplicate an
    in-flight investigation.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = (
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.work_ref == work_ref,
                AgentRun.status.in_([s.value for s in _NON_TERMINAL_STATUSES]),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).first() is not None


async def _load_investigator_definition(tenant_id: uuid.UUID, name: str) -> tuple[str, str] | None:
    """Return ``(name, identity_ref)`` for the tenant's enabled investigator agent.

    ``None`` when no **enabled** definition with the well-known name exists in
    the tenant -- the opt-in switch. A tenant enables the wiring by creating an
    enabled ``AgentDefinition`` named ``checks-investigator`` (default); an
    absent or disabled definition logs and skips.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AgentDefinition.name, AgentDefinition.identity_ref).where(
            AgentDefinition.tenant_id == tenant_id,
            AgentDefinition.name == name,
            AgentDefinition.enabled.is_(True),
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            return None
        return row.name, row.identity_ref


def _build_briefing(dashboard: _DashboardSnapshot, group: _CauseGroup) -> str:
    """Assemble the deterministic investigator briefing.

    Three labelled sections (the briefing-builder mould from the r1 harness):
    the transitioning Dashboard, the correlated non-green Sensors with their
    evidence, and the correlation context. Instructs a JSON-only
    :class:`ChecksFinding` answer so the caller-side parse is deterministic.
    """
    parts: list[str] = ["## Dashboard", ""]
    parts.append(f"name: {dashboard.name}")
    parts.append(f"transition: {dashboard.previous_state} -> {dashboard.current_state}")
    parts.append(f"correlation_group: {group.key}")
    parts.append("")
    parts.append("## Correlated sensors")
    parts.append("")
    parts.append(
        "These sensors are non-green and share a common topology cause; "
        "investigate the ONE underlying cause, not each symptom."
    )
    for member in group.members:
        parts.append("")
        parts.append(f"- name: {member.name}")
        parts.append(f"  state: {member.effective_state} (raw {member.raw_state})")
        parts.append(f"  op: {member.connector_id} {member.op_id}")
        parts.append(f"  last_value: {member.last_value!r}")
        if member.last_evidence:
            parts.append(f"  evidence: {member.last_evidence!r}")
    parts.append("")
    parts.append("## Output")
    parts.append("")
    parts.append(
        "Investigate read-only, then answer with a single JSON object matching "
        "ChecksFinding: {verdict: benign|acknowledged|actionable, re_escalate: "
        "bool, summary: str, evidence: [str], recommended_action: str|null}. "
        "Set re_escalate=false for benign/acknowledged so this correlated red is "
        "suppressed until it recurs. recommended_action is advisory text only -- "
        "any change op you attempt parks for operator approval and is never "
        "executed by this wiring."
    )
    return "\n".join(parts)


async def _await_finding(operator: Operator, outcome: AgentRunOutcome) -> ChecksFinding | None:
    """Resolve *outcome* to a parsed :class:`ChecksFinding`, or ``None``.

    A run terminal on return is parsed immediately; a run that converted to
    async (outlived the sync timeout) is polled on a fixed cadence up to
    ``checks_investigation_poll_timeout_seconds``. A non-``succeeded`` terminal,
    a poll-deadline miss, or an unparseable answer yields ``None`` (the finding
    is still durable on the run row; only the memory write-back is skipped).
    """
    status = outcome.status
    output = outcome.output
    if outcome.converted_to_async or status not in _TERMINAL_STATUSES:
        resolved = await _poll_to_terminal(operator, outcome.run_id)
        if resolved is None:
            return None
        status, output = resolved.status, resolved.output
    if status is not AgentRunStatus.SUCCEEDED:
        _log().info(
            "checks_investigation_run_not_successful",
            run_id=str(outcome.run_id),
            status=status.value,
        )
        return None
    return _parse_finding(output, run_id=outcome.run_id)


async def _poll_to_terminal(operator: Operator, run_id: uuid.UUID) -> AgentRunStatusView | None:
    """Bounded-poll a run until terminal; ``None`` on deadline.

    Deep investigations routinely outlive the sync timeout, so
    ``run_scheduled`` hands back a still-running handle. Poll the durable row on
    a fixed cadence up to the configured deadline.
    """
    invoker = get_agent_invoker()
    deadline = time.monotonic() + get_settings().checks_investigation_poll_timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        view = await invoker.poll(operator, run_id)
        if view.status in _TERMINAL_STATUSES:
            return view
    _log().warning("checks_investigation_poll_deadline", run_id=str(run_id))
    return None


def _parse_finding(output: dict[str, object] | None, *, run_id: uuid.UUID) -> ChecksFinding | None:
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


async def _persist_finding(
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
        _render_finding_body(finding),
        slug=f"{_NOISE_SLUG_PREFIX}{group_key}",
        metadata={
            "source": _MEMORY_SOURCE,
            "verdict": finding.verdict,
            "re_escalate": finding.re_escalate,
            "run_id": str(run_id),
        },
    )


def _render_finding_body(finding: ChecksFinding) -> str:
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


def _investigator_operator(tenant_id: uuid.UUID) -> Operator:
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


async def _await_pending_investigations() -> None:
    """Await all in-flight investigation tasks -- a deterministic test seam.

    Production spawns investigations fire-and-forget; tests call this to drain
    them before asserting on the invoker / memory. Mirrors the runner's
    in-flight-drain discipline.
    """
    pending = [task for task in _INVESTIGATIONS if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
