# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reflex-adoption KPIs aggregated from ``audit_log`` + the announce store.

Task #3134 (Initiative #3128). The reflex work — tool-group
descriptions, plugin hooks, dispatch advisory — is behavioural: its
whole point is to change what an agent does *first* in a session. This
module makes that falsifiable by reading the synchronous, append-only,
session-tagged ``audit_log`` (and the #2544 :class:`AgentAnnouncement`
store) and reporting four v1 metrics over a time window, per tenant,
split by surface. It reuses the #444 usage-telemetry seam (a
``compute_*`` service behind a REST route + CLI verb, aggregating
``audit_log`` in Python for cross-dialect portability) rather than a
parallel metrics pipeline — the canonical record is the same one the
G8 audit-trail surface queries, so the numbers can be re-derived at any
time from the same source.

Row shapes this module reads
----------------------------

Every audited operation lands in ``audit_log`` with a ``path``, a
``method``, a ``status_code``, an ``occurred_at``, a ``tenant_id``, and
a nullable ``agent_session_id`` (the MCP/agent-run session correlation
key, migration ``0014``). Two row shapes matter here:

* **MCP meta-tool envelope rows** — one per ``tools/call`` invocation,
  written by :func:`meho_backplane.mcp.audit.write_mcp_audit_row` with
  ``method="MCP"`` and ``path = f"{MCP_TOOL_PATH_PREFIX}{tool_name}"``.
  The reflex meta-tools ``call_operation`` / ``broadcast_recent`` /
  ``add_to_knowledge`` / ``add_to_memory`` are counted from these rows,
  the same path-prefix filter #444 uses for the retrieval search tools.
* **Dispatch rows** — one per connector operation, written by
  :func:`meho_backplane.operations._audit.write_audit_row` with
  ``method="DISPATCH"`` and ``path = descriptor.op_id`` (e.g.
  ``vsphere.vm.create``, ``GET:/api/v2.0/systeminfo``). A row's
  write-vs-read class is :func:`meho_backplane.broadcast.events.classify_op`
  applied to ``path``; the mutating classes are :data:`WRITE_OP_CLASSES`.

Only successful rows (``status_code == 200``) are counted, mirroring
#444: a 4xx/5xx attempt did not dispatch and is not "acting".

Surface split
-------------

Each metric is reported for two surfaces, partitioned by the presence
of ``agent_session_id`` on the row:

* ``agent`` — rows **with** ``agent_session_id`` (MCP/agent-run
  traffic, where client-side reflex levers such as plugin hooks apply).
* ``cli_rest`` — rows **without** ``agent_session_id`` (CLI/REST
  traffic, which can only ever receive server-side levers).

The split is the comparison the reflex work needs: read-before-act and
write-back are structurally computable only on the ``agent`` surface
(the meta-tool envelope rows always carry a session), so the
``cli_rest`` surface reports them as ``None`` — that absence *is* the
signal that a surface has no client-side reflex lever. Announce
coverage splits meaningfully across both, since dispatch rows occur on
either surface.

Metric definitions (v1)
-----------------------

Let a *session* be a distinct non-null ``agent_session_id``.

1. **read-before-act** (``read_before_act_pct``) — of the sessions with
   at least one ``call_operation``, the fraction whose **first**
   ``call_operation`` is strictly preceded, in the same session, by a
   ``broadcast_recent``. Denominator: sessions with ≥1
   ``call_operation``. ``None`` when that denominator is 0 (the
   ``cli_rest`` surface, and any window with no agent operations).

2. **announce-coverage** (``announce_coverage_pct``) — of the
   write-class dispatch rows (:data:`WRITE_OP_CLASSES` via
   :func:`classify_op`), the fraction executed with an announce claim
   earlier in the same session: an :class:`AgentAnnouncement` whose
   ``run_id`` equals the row's ``agent_session_id`` and whose
   ``created_at`` is ``<=`` the row's ``occurred_at``. (The "active
   claim" reading — a TTL-bounded window — is subsumed by "an announce
   earlier in the same session" for the boolean, so v1 correlates on
   session identity and leaves TTL as a future refinement.) A
   ``cli_rest`` write op has no session to correlate, so it is never
   covered. Denominator: write-class dispatch rows. ``None`` when 0.

3. **write-back rate** (``write_back_per_100_call_ops``) — the count of
   ``add_to_knowledge`` + ``add_to_memory`` calls per 100
   ``call_operation`` calls. ``None`` when there are 0
   ``call_operation`` calls on the surface.

4. **surface split** — every metric above, reported once per surface
   (``agent`` / ``cli_rest``).

Tenant boundary
---------------

The router gate resolves the effective tenant (own tenant, or a
``platform_admin``-supplied filter) and passes it here; this helper
never inspects the :class:`Operator`. A non-null ``tenant_id`` scopes
every query — ``audit_log`` rows *and* :class:`AgentAnnouncement` rows
— so cross-tenant rows cannot leak into another tenant's numbers.

References
----------

* Parent Initiative #3128; Task #3134.
* #444 — the ``audit_log`` aggregation + REST + CLI precedent reused here.
* #2544 — the structured announce-claim store (:class:`AgentAnnouncement`).
* Delineation from evoila-bosnia/meho-internal#200: taint metrics
  measure meho-vs-local-fallback *adoption*; these reflex KPIs measure
  *in-session discipline*. Related, not merged.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast.events import classify_op
from meho_backplane.db.models import AgentAnnouncement, AuditLog

# Re-exported from the #444 seam so the reflex ``--since`` grammar is
# identical to ``meho retrieval usage`` by construction, not by a
# duplicated parser that could drift.
from meho_backplane.retrieval.usage import (
    MCP_TOOL_PATH_PREFIX,
    SinceValueError,
    parse_since,
)

__all__ = [
    "ADD_TOOLS",
    "BROADCAST_RECENT_TOOL",
    "CALL_OPERATION_TOOL",
    "DEFAULT_SINCE",
    "DISPATCH_METHOD",
    "MCP_TOOL_PATH_PREFIX",
    "SUCCESS_STATUS",
    "WRITE_OP_CLASSES",
    "ReflexReport",
    "SinceValueError",
    "SurfaceMetrics",
    "compute_reflex_report",
    "parse_since",
]

#: The reflex meta-tools, keyed by the audited MCP tool name. Counted
#: from the envelope rows written with
#: ``path = f"{MCP_TOOL_PATH_PREFIX}{tool_name}"``.
CALL_OPERATION_TOOL: Final[str] = "call_operation"
BROADCAST_RECENT_TOOL: Final[str] = "broadcast_recent"

#: The write-back meta-tools (knowledge + memory add). Their combined
#: successful-call count is the write-back numerator.
ADD_TOOLS: Final[tuple[str, ...]] = ("add_to_knowledge", "add_to_memory")

#: ``method`` value on dispatcher-written rows (one per connector
#: operation). ``path`` on these rows is the descriptor's ``op_id``.
DISPATCH_METHOD: Final[str] = "DISPATCH"

#: The :func:`classify_op` classes that count as a *write* (a mutating
#: operation) for announce coverage. The three mutating classes the
#: classifier emits; read / audit_query / approval / checks / other are
#: excluded.
WRITE_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"write", "credential_write", "credential_mint"},
)

#: Only successful rows count (mirrors #444): a 4xx/5xx attempt did not
#: dispatch, so it is not an act, a read, or a write-back.
SUCCESS_STATUS: Final[int] = 200

#: Default ``--since`` window. Reflex discipline is a recent-behaviour
#: signal, so the default is a short week rather than #444's 30-day
#: retire-threshold window.
DEFAULT_SINCE: Final[str] = "7d"

#: The two surfaces the report is split by.
_SURFACES: Final[tuple[Literal["agent", "cli_rest"], ...]] = ("agent", "cli_rest")


class SurfaceMetrics(BaseModel):
    """The four reflex metrics for one surface (``agent`` / ``cli_rest``).

    Each ratio is reported as a rounded percentage/rate together with
    the raw numerator and denominator, so a consumer can re-derive the
    ratio and a zero-denominator surface reads as ``None`` (N/A) rather
    than a misleading ``0.0``. Frozen so report instances can be passed
    around without accidental mutation.
    """

    model_config = ConfigDict(frozen=True)

    surface: Literal["agent", "cli_rest"]

    #: read-before-act.
    read_before_act_pct: float | None
    read_before_act_sessions: int
    read_before_act_read_first: int

    #: announce-coverage.
    announce_coverage_pct: float | None
    announce_coverage_write_ops: int
    announce_coverage_announced: int

    #: write-back rate (per 100 ``call_operation``).
    write_back_per_100_call_ops: float | None
    write_back_add_calls: int
    write_back_call_operations: int


class ReflexReport(BaseModel):
    """Top-level shape returned by :func:`compute_reflex_report` + the route.

    ``surfaces`` is always ordered ``[agent, cli_rest]`` so table
    renderers and ``--json`` consumers see a stable shape even for an
    empty window (both surfaces present with zero-valued / ``None``
    metrics). ``tenant_id`` is the scope the report was computed for
    (the operator's own tenant, or a ``platform_admin`` filter).
    """

    model_config = ConfigDict(frozen=True)

    since: datetime
    until: datetime
    tenant_id: UUID | None
    surfaces: list[SurfaceMetrics]


def _tool_path(tool_name: str) -> str:
    """Return the audited MCP envelope path for *tool_name*."""
    return f"{MCP_TOOL_PATH_PREFIX}{tool_name}"


def _is_agent_surface(agent_session_id: UUID | None) -> Literal["agent", "cli_rest"]:
    """Map a row's ``agent_session_id`` onto its surface label."""
    return "agent" if agent_session_id is not None else "cli_rest"


async def _fetch_meta_tool_rows(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: UUID | None,
) -> list[Any]:
    """Pull successful reflex meta-tool envelope rows in the window.

    Returns ``(occurred_at, agent_session_id, path)`` for the four
    reflex meta-tools, ordered by ``(agent_session_id, occurred_at)`` so
    the per-session read-before-act walk sees each session's timeline in
    order.
    """
    paths = [
        _tool_path(CALL_OPERATION_TOOL),
        _tool_path(BROADCAST_RECENT_TOOL),
        *(_tool_path(tool) for tool in ADD_TOOLS),
    ]
    stmt = (
        select(
            AuditLog.occurred_at,
            AuditLog.agent_session_id,
            AuditLog.path,
        )
        .where(AuditLog.occurred_at >= since)
        .where(AuditLog.occurred_at <= until)
        .where(AuditLog.path.in_(paths))
        .where(AuditLog.status_code == SUCCESS_STATUS)
        .order_by(AuditLog.agent_session_id, AuditLog.occurred_at)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return list((await session.execute(stmt)).all())


async def _fetch_dispatch_rows(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: UUID | None,
) -> list[Any]:
    """Pull successful dispatcher rows in the window (write-class filtered later).

    Returns ``(occurred_at, agent_session_id, path)``. ``path`` is the
    descriptor ``op_id``; the write-class filter is applied in Python via
    :func:`classify_op` because the classifier is match-order-significant
    Python, not expressible in portable SQL.
    """
    stmt = (
        select(
            AuditLog.occurred_at,
            AuditLog.agent_session_id,
            AuditLog.path,
        )
        .where(AuditLog.occurred_at >= since)
        .where(AuditLog.occurred_at <= until)
        .where(AuditLog.method == DISPATCH_METHOD)
        .where(AuditLog.status_code == SUCCESS_STATUS)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return list((await session.execute(stmt)).all())


async def _fetch_announce_first_seen(
    session: AsyncSession,
    *,
    until: datetime,
    tenant_id: UUID | None,
) -> dict[UUID, datetime]:
    """Return ``run_id -> earliest created_at`` for announce claims.

    An op is announce-covered iff its session has an announcement whose
    ``created_at`` is at or before the op — so the earliest announcement
    per session (``run_id``) is the only fact the coverage check needs.
    No lower time bound: an announcement made before the report window
    can still cover an in-window op. Rows without a ``run_id`` cannot be
    session-correlated and are skipped.
    """
    stmt = select(
        AgentAnnouncement.run_id,
        AgentAnnouncement.created_at,
    ).where(AgentAnnouncement.created_at <= until)
    if tenant_id is not None:
        stmt = stmt.where(AgentAnnouncement.tenant_id == tenant_id)

    first_seen: dict[UUID, datetime] = {}
    for run_id, created_at in (await session.execute(stmt)).all():
        if run_id is None:
            continue
        current = first_seen.get(run_id)
        if current is None or created_at < current:
            first_seen[run_id] = created_at
    return first_seen


def _read_before_act(
    meta_rows: Iterable[Any],
    surface: str,
) -> tuple[int, int]:
    """Return ``(read_first_sessions, sessions_with_call_op)`` for *surface*.

    A session (distinct ``agent_session_id``) counts once it has ≥1
    ``call_operation``; it is "read-first" when a ``broadcast_recent``
    strictly precedes the session's first ``call_operation``.
    """
    call_path = _tool_path(CALL_OPERATION_TOOL)
    recent_path = _tool_path(BROADCAST_RECENT_TOOL)
    first_call: dict[UUID, datetime] = {}
    first_recent: dict[UUID, datetime] = {}
    for row in meta_rows:
        sid = row.agent_session_id
        if sid is None or _is_agent_surface(sid) != surface:
            continue
        if row.path == call_path:
            current = first_call.get(sid)
            if current is None or row.occurred_at < current:
                first_call[sid] = row.occurred_at
        elif row.path == recent_path:
            current = first_recent.get(sid)
            if current is None or row.occurred_at < current:
                first_recent[sid] = row.occurred_at

    sessions = len(first_call)
    read_first = sum(
        1
        for sid, call_ts in first_call.items()
        if sid in first_recent and first_recent[sid] < call_ts
    )
    return read_first, sessions


def _announce_coverage(
    dispatch_rows: Iterable[Any],
    announce_first_seen: dict[UUID, datetime],
    surface: str,
) -> tuple[int, int]:
    """Return ``(announced_write_ops, write_ops)`` for *surface*.

    A write-class dispatch row is covered when its session has an
    announcement first seen at or before the op. ``cli_rest`` rows carry
    no session and so are never covered.
    """
    write_ops = 0
    announced = 0
    for row in dispatch_rows:
        if _is_agent_surface(row.agent_session_id) != surface:
            continue
        if classify_op(row.path) not in WRITE_OP_CLASSES:
            continue
        write_ops += 1
        sid = row.agent_session_id
        if sid is not None:
            seen = announce_first_seen.get(sid)
            if seen is not None and seen <= row.occurred_at:
                announced += 1
    return announced, write_ops


def _write_back(
    meta_rows: Iterable[Any],
    surface: str,
) -> tuple[int, int]:
    """Return ``(add_calls, call_operations)`` for *surface*."""
    call_path = _tool_path(CALL_OPERATION_TOOL)
    add_paths = {_tool_path(tool) for tool in ADD_TOOLS}
    call_ops = 0
    add_calls = 0
    for row in meta_rows:
        if _is_agent_surface(row.agent_session_id) != surface:
            continue
        if row.path == call_path:
            call_ops += 1
        elif row.path in add_paths:
            add_calls += 1
    return add_calls, call_ops


def _pct(numerator: int, denominator: int) -> float | None:
    """Return ``numerator/denominator`` as a 0-100 percentage, or ``None``.

    ``None`` when *denominator* is 0 (N/A), so a zero-activity surface is
    never mis-rendered as ``0.0``.
    """
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100.0, 2)


def _surface_metrics(
    surface: Literal["agent", "cli_rest"],
    *,
    meta_rows: list[Any],
    dispatch_rows: list[Any],
    announce_first_seen: dict[UUID, datetime],
) -> SurfaceMetrics:
    """Compute all four metrics for one surface."""
    read_first, rba_sessions = _read_before_act(meta_rows, surface)
    announced, write_ops = _announce_coverage(dispatch_rows, announce_first_seen, surface)
    add_calls, call_ops = _write_back(meta_rows, surface)
    return SurfaceMetrics(
        surface=surface,
        read_before_act_pct=_pct(read_first, rba_sessions),
        read_before_act_sessions=rba_sessions,
        read_before_act_read_first=read_first,
        announce_coverage_pct=_pct(announced, write_ops),
        announce_coverage_write_ops=write_ops,
        announce_coverage_announced=announced,
        write_back_per_100_call_ops=_pct(add_calls, call_ops),
        write_back_add_calls=add_calls,
        write_back_call_operations=call_ops,
    )


async def compute_reflex_report(
    *,
    session: AsyncSession,
    since: datetime,
    until: datetime,
    tenant_id: UUID | None,
) -> ReflexReport:
    """Aggregate ``audit_log`` + announce rows into a reflex-adoption report.

    Three tenant-scoped fetches (meta-tool envelope rows, dispatcher
    rows, per-session earliest announce), then per-surface Python
    correlation. Read-only: the helper neither commits nor closes the
    caller-owned *session*. An empty window yields both surfaces present
    with zero counts and ``None`` ratios.
    """
    meta_rows = await _fetch_meta_tool_rows(
        session,
        since=since,
        until=until,
        tenant_id=tenant_id,
    )
    dispatch_rows = await _fetch_dispatch_rows(
        session,
        since=since,
        until=until,
        tenant_id=tenant_id,
    )
    announce_first_seen = await _fetch_announce_first_seen(
        session,
        until=until,
        tenant_id=tenant_id,
    )

    surfaces = [
        _surface_metrics(
            surface,
            meta_rows=meta_rows,
            dispatch_rows=dispatch_rows,
            announce_first_seen=announce_first_seen,
        )
        for surface in _SURFACES
    ]
    return ReflexReport(
        since=since,
        until=until,
        tenant_id=tenant_id,
        surfaces=surfaces,
    )
