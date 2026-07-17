# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit-log-backed usage telemetry for the retrieval meta-tools (G4.3-T5).

The G4.3 Initiative (#373) makes the v0.1 "≥1 month daily use" retire
criterion for the consumer's ``kb/`` / ``docs/`` surfaces falsifiable.
This module is the daily-use side of that contract: it reads
``audit_log`` rows attributable to the five retrieval-class MCP
meta-tools — ``search_knowledge`` / ``search_memory`` /
``search_operations`` (read) plus ``add_to_knowledge`` /
``add_to_memory`` (write) — and reports per-day, per-surface,
per-tenant aggregates.

Surfaces
--------

The three retrieval **surfaces** (kb / memory / operations) each have
one mandatory search meta-tool and zero-or-one write meta-tool:

* ``kb`` — :func:`search_knowledge` (G4.1 #331) + :func:`add_to_knowledge`.
* ``memory`` — :func:`search_memory` (G5.1) + :func:`add_to_memory`.
* ``operations`` — :func:`search_operations` (G0.6-T8 #438; shipped).

The G4.1 + G5.1 search tools are not yet registered; usage telemetry
shipped today against an audit_log with zero matching rows returns
all-zero buckets gracefully. The verb is ready when those tools start
emitting events.

Why audit_log and not a separate metrics pipeline
-------------------------------------------------

Decision #2 of ``docs/decisions/locked-decisions.md`` (the retire-decision
criteria) and Goal #215's DoD line both hinge on **the same audited
operations every other meta-tool already emits**. A separate metrics
pipeline would drift from the canonical record on partial failures
(audit_log writes are synchronous + fail-closed; a Prometheus counter
would silently miss a row when the metrics endpoint is degraded). The
``meho retrieval usage`` verb queries the canonical record directly so
"≥1 month of daily use" can be re-verified at any time from the same
source the audit-trail surface (G8) queries.

Search-to-action conversion
---------------------------

For each search row this module asks: did the same operator (in the
same tenant) emit **any** subsequent audit_log row within the
:data:`CONVERSION_WINDOW` (5 minutes) of the search? If so, the search
counts as **converted** — it produced an observable downstream
action and is a positive signal that the operator found a useful
answer.

The "any subsequent action" definition is deliberately broad in v0.2:

* A connector-targeted resource read after ``search_knowledge``
  (e.g. ``meho://kb/<hit-slug>``).
* A ``call_operation`` after ``search_operations``.
* A ``meho://memory/<scope>/<slug>`` read after ``search_memory``.
* A pure operator follow-up (running ``meho status`` to confirm a
  hypothesis after a search) also classifies as conversion in v0.2.

A stricter per-surface action-shape match (``search_knowledge`` →
specifically a kb-resource-read) is v0.2.next refinement; the broad
definition is the right v0.2 baseline because the retire-checklist
(T6) only needs a directional signal ("operators do something after
searching, vs grep-and-fall-through"), not a perfect per-hit
attribution.

Cross-DB portability
--------------------

The aggregations run via SQLAlchemy 2.x against the same engine the
audit middleware writes to. Tests use SQLite (per the existing
``tests/conftest.py``); production runs against PostgreSQL.
:func:`sqlalchemy.func.date` compiles to ``date(col)`` on both dialects
— PG drops the time component of a ``timestamptz``; SQLite's stdlib
``date()`` does the same against an ISO-8601 string. The conversion
window check fetches all candidate-action rows in the extended window
and computes the per-search verdict in Python rather than expressing
``occurred_at + interval '5 minutes'`` (PG) versus
``datetime(occurred_at, '+5 minutes')`` (SQLite), because the v0.2
audit_log volume (≪ 1M rows in any reasonable retrieval-eval window)
makes the in-process cost negligible and the SQL stays portable.

Tenant boundary
---------------

The router gate enforces tenancy: ``operator``-role callers see only
their own tenant; ``tenant_admin`` may pass a ``tenant_filter`` for
cross-tenant aggregates (Goal #215 retire-decision requires a
team-of-4 cross-tenant view in v0.2.next; in v0.2 there is one
production tenant, but the gate ships now so the v0.2.next migration
is a no-op). The service-level helper accepts the resolved
``tenant_id | None`` and never inspects the :class:`Operator`
directly — RBAC is the router's responsibility, and the helper is
unit-testable against arbitrary scope shapes.

References
----------

* Parent Initiative #373 (G4.3 retrieval migration tooling).
* Parent Task #444 (this Task).
* :mod:`meho_backplane.audit` — the chassis writer this module reads
  back from.
* :mod:`meho_backplane.mcp.audit` — the MCP-specific writer that
  produces the rows this module aggregates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog

__all__ = [
    "ADD_OPS",
    "CONVERSION_WINDOW",
    "COUNTED_SEARCH_SURFACES",
    "DEFAULT_SINCE",
    "MCP_TOOL_PATH_PREFIX",
    "REST_RETRIEVE_EXCLUDED",
    "SEARCH_OPS",
    "SUPPORTED_SURFACES",
    "DailyUsageBucket",
    "SinceValueError",
    "UsageReport",
    "compute_usage",
    "parse_since",
]

#: MCP tool calls land in :class:`AuditLog` with
#: ``path = f"{MCP_TOOL_PATH_PREFIX}{tool_name}"`` (see
#: :mod:`meho_backplane.mcp.audit`). The prefix is the cross-DB-portable
#: filter we apply to ``audit_log.path`` — JSON-extraction on
#: ``payload->>'op_id'`` would work on PostgreSQL but compiles
#: differently on SQLite, and path filtering needs only a btree index
#: scan against the existing column.
MCP_TOOL_PATH_PREFIX: Final[str] = "/mcp/tools/call/"

#: Search-class retrieval meta-tools per surface. Aligned with the
#: parent Initiative's three-surface eval corpus (kb / memory /
#: operations) and the corresponding MCP tool registrations in G4.1 /
#: G5.1 / G0.6-T8.
SEARCH_OPS: Final[Mapping[str, str]] = {
    "search_knowledge": "kb",
    "search_memory": "memory",
    "search_operations": "operations",
}

#: Write-class retrieval meta-tools per surface. ``search_operations``
#: has no write counterpart in v0.2 — operation descriptors arrive via
#: spec ingestion (G0.7) or typed registration, never via an agent-
#: facing add tool. A future ``add_operation`` would extend this map.
ADD_OPS: Final[Mapping[str, str]] = {
    "add_to_knowledge": "kb",
    "add_to_memory": "memory",
}

#: The full set of surface labels callers may filter by. Used to
#: validate ``surface=`` query strings and to construct the bucket
#: cross-product when the surface had zero searches.
SUPPORTED_SURFACES: Final[tuple[str, ...]] = ("kb", "memory", "operations")

#: The fully-qualified labels of the surfaces this counter actually
#: counts. Derived from :data:`SEARCH_OPS` (the single source of truth
#: for which audit_log paths feed the aggregation) so the documented
#: contract cannot drift from the query filter. Only the audited MCP
#: search meta-tools tick the counter: a row lands in ``audit_log`` with
#: ``path = f"{MCP_TOOL_PATH_PREFIX}{tool}"`` only when the tool is
#: invoked over the ``/mcp`` surface. ``POST /api/v1/retrieve`` runs the
#: same retrieval substrate but audits under ``/api/v1/retrieve`` — not
#: a counted path — so it is deliberately excluded (see
#: :data:`REST_RETRIEVE_EXCLUDED`). Surfaced verbatim on
#: :class:`UsageReport` so a ``total_searches=0`` reads as "REST is not
#: counted", not "no usage".
COUNTED_SEARCH_SURFACES: Final[tuple[str, ...]] = tuple(f"mcp:{op_id}" for op_id in SEARCH_OPS)

#: Whether the operator-facing REST ``POST /api/v1/retrieve`` surface
#: is excluded from this counter. Always ``True`` in v0.2 by design: REST
#: ``/retrieve`` audits under its own path, not a counted MCP tool
#: path, so dogfooding exclusively through REST shows a permanent
#: silent zero. Counting REST would change audit volume and risk
#: double-counting against the MCP path — a separate scoped change if
#: ever wanted. This flag exists so the zero is self-explaining.
REST_RETRIEVE_EXCLUDED: Final[bool] = True

#: Conversion window — a search "converts" if the same operator emits
#: any subsequent audit_log row within this window. Five minutes is the
#: v0.2 default: long enough to absorb operator distractions (Slack
#: pings, context switches) yet short enough to reject coincidental
#: same-day activity.
CONVERSION_WINDOW: Final[timedelta] = timedelta(minutes=5)

#: Default ``--since`` window. Goal #215's decision #2 requires
#: ≥1-month overlap before retire is on the table; matching that as the
#: default makes ``meho retrieval usage`` a one-shot answer to "are we
#: at the retire threshold yet?".
DEFAULT_SINCE: Final[str] = "30d"

#: Internal — relative-window grammar ``\d+[dh]``. ISO-8601 dates fall
#: through to :meth:`datetime.fromisoformat` when this doesn't match.
_RELATIVE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?P<n>\d{1,4})(?P<unit>[dh])")


class SinceValueError(ValueError):
    """Raised by :func:`parse_since` when the input doesn't parse.

    Subclasses :class:`ValueError` so router-side ``except`` catches
    the standard exception; the dedicated subclass exists so test
    code can pin "this specific parser rejected the input" without
    matching on every ``ValueError`` the call stack might raise.
    """


def parse_since(value: str, *, now: datetime) -> datetime:
    """Resolve a ``--since`` query string to an absolute UTC datetime.

    Accepts two grammars:

    * **Relative window** — ``<N>d`` or ``<N>h`` (e.g. ``30d``,
      ``24h``). ``N`` is an unsigned integer ≤ 9999; values outside
      this band reject as :class:`SinceValueError` rather than
      compute-and-overflow. The result is ``now - timedelta``.
    * **Absolute ISO-8601 date** — ``YYYY-MM-DD`` or full
      ``YYYY-MM-DDTHH:MM:SS`` (the shape :meth:`datetime.fromisoformat`
      accepts). The result is the parsed datetime, with UTC attached
      when naive so the downstream ``occurred_at`` comparison
      (timezone-aware on PG, naive on SQLite) lands in a single
      timezone.

    *now* is passed explicitly so tests can pin a frozen clock without
    monkey-patching :func:`datetime.now`. Production callers pass
    ``datetime.now(UTC)``.

    Examples
    --------

    >>> from datetime import datetime, UTC
    >>> ref = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    >>> parse_since("30d", now=ref).isoformat()
    '2026-04-14T12:00:00+00:00'
    >>> parse_since("24h", now=ref).isoformat()
    '2026-05-13T12:00:00+00:00'
    >>> parse_since("2026-04-01", now=ref).isoformat()
    '2026-04-01T00:00:00+00:00'
    """
    if not value:
        raise SinceValueError("since must be non-empty")

    rel = _RELATIVE_PATTERN.fullmatch(value)
    if rel is not None:
        n = int(rel.group("n"))
        unit = rel.group("unit")
        delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
        return now - delta

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SinceValueError(
            f"unrecognised since value {value!r}: expected '<N>d', '<N>h', "
            "or an ISO-8601 date (YYYY-MM-DD)",
        ) from exc

    if parsed.tzinfo is None:
        # Naive datetimes from operators are interpreted as UTC. Any
        # other choice (server local, operator local) would be a
        # silent-correctness footgun: the ``occurred_at`` column is
        # ``timestamptz`` on PG and naive-UTC on SQLite; comparing a
        # naive non-UTC value against either would shift the window
        # by the local offset without the operator noticing.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class DailyUsageBucket(BaseModel):
    """One ``(date, surface)`` row of the usage report.

    Frozen so callers (CLI table renderer, ``--json`` consumers,
    Markdown report generators) can pass instances around without
    accidental mutation. ``action_conversion_pct`` is rounded to two
    decimals at construction time so JSON serialisation is stable
    across runs.
    """

    model_config = ConfigDict(frozen=True)

    date: date_type
    surface: Literal["kb", "memory", "operations"]
    search_count: int
    distinct_operators: int
    action_conversion_pct: float


class UsageReport(BaseModel):
    """Top-level shape returned by :func:`compute_usage` + the API route.

    ``buckets`` is sorted by ``(date, surface)`` ascending — the text
    table renderer relies on this for its grouping behaviour, and
    ``--json`` consumers (T6's retire-checklist, future dashboards)
    benefit from deterministic ordering even when nothing else
    pins it.

    ``tenant_id`` is the tenant the report is scoped to (the
    operator's own tenant by default, or the
    ``tenant_admin``-supplied filter). ``None`` is reserved for the
    cross-tenant case; v0.2 has one production tenant so this is
    effectively a v0.2.next placeholder, but the shape is locked
    today so consumers don't break when the cross-tenant verb lands.
    """

    model_config = ConfigDict(frozen=True)

    since: datetime
    until: datetime
    surfaces: list[str]
    tenant_id: UUID | None
    buckets: list[DailyUsageBucket]
    total_searches: int

    #: The fully-qualified surface labels that actually feed
    #: ``total_searches`` (``["mcp:search_knowledge", …]``). De-silences
    #: a zero: an operator who dogfoods exclusively via REST
    #: ``POST /api/v1/retrieve`` sees ``total_searches=0`` with this
    #: list present, so the zero reads as "REST is not counted" instead
    #: of "no retrieval activity". Defaulted from
    #: :data:`COUNTED_SEARCH_SURFACES` so the response cannot drift from
    #: the query filter.
    counted_surfaces: list[str] = Field(
        default_factory=lambda: list(COUNTED_SEARCH_SURFACES),
    )

    #: ``True`` whenever REST ``POST /api/v1/retrieve`` is excluded from
    #: the counter (always, in v0.2 — see :data:`REST_RETRIEVE_EXCLUDED`).
    #: The machine-readable companion to ``counted_surfaces`` for
    #: consumers (T6 retire-checklist, dashboards) that want a boolean
    #: rather than to diff the surface list.
    rest_excluded: bool = Field(default=REST_RETRIEVE_EXCLUDED)


def _search_paths_for(surfaces: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Return ``(paths, path_to_surface)`` for the given surface set.

    Splitting the lookup table out makes the query builder testable
    in isolation: pass any surface set and assert the
    ``AuditLog.path IN (...)`` list matches the expected MCP tool
    routing.
    """
    surface_set = set(surfaces)
    paths: list[str] = []
    mapping: dict[str, str] = {}
    for op_id, surface in SEARCH_OPS.items():
        if surface in surface_set:
            path = f"{MCP_TOOL_PATH_PREFIX}{op_id}"
            paths.append(path)
            mapping[path] = surface
    return paths, mapping


async def _fetch_search_rows(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    paths: list[str],
    tenant_id: UUID | None,
) -> list[Any]:
    """Pull successful search rows from ``audit_log`` in the time window.

    Successful rows only (``status_code = 200``) — a 4xx/5xx search
    attempt isn't "daily use". Ordering by ``(operator_sub,
    occurred_at)`` lets the downstream conversion loop walk the per-
    operator action timeline without re-sorting.
    """
    stmt = (
        select(
            AuditLog.id,
            AuditLog.occurred_at,
            AuditLog.operator_sub,
            AuditLog.tenant_id,
            AuditLog.path,
        )
        .where(AuditLog.occurred_at >= since)
        .where(AuditLog.occurred_at <= until)
        .where(AuditLog.path.in_(paths))
        .where(AuditLog.status_code == 200)
        .order_by(AuditLog.operator_sub, AuditLog.occurred_at)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return list((await session.execute(stmt)).all())


async def _fetch_action_universe(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: UUID | None,
) -> list[Any]:
    """Pull every audit_log row in the extended window (conversion universe).

    Over-fetches by :data:`CONVERSION_WINDOW` on the upper bound so a
    search at exactly ``until`` still sees a same-tenant follow-up
    action that lands just inside the window.
    """
    stmt = (
        select(
            AuditLog.id,
            AuditLog.operator_sub,
            AuditLog.tenant_id,
            AuditLog.occurred_at,
        )
        .where(AuditLog.occurred_at >= since)
        .where(AuditLog.occurred_at <= until + CONVERSION_WINDOW)
        .order_by(AuditLog.operator_sub, AuditLog.occurred_at)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return list((await session.execute(stmt)).all())


def _index_actions_by_principal(
    action_rows: Iterable[Any],
) -> dict[tuple[str, UUID | None], list[tuple[datetime, UUID]]]:
    """Group action rows into per-``(operator_sub, tenant_id)`` timelines.

    Each timeline stays sorted because :func:`_fetch_action_universe`
    issued ``ORDER BY operator_sub, occurred_at``. The conversion loop
    relies on that ordering to short-circuit on the first action past
    the window.
    """
    timelines: dict[tuple[str, UUID | None], list[tuple[datetime, UUID]]] = {}
    for action in action_rows:
        timelines.setdefault((action.operator_sub, action.tenant_id), []).append(
            (action.occurred_at, action.id),
        )
    return timelines


def _has_conversion(
    *,
    search_id: UUID,
    search_occurred_at: datetime,
    timeline: list[tuple[datetime, UUID]],
) -> bool:
    """Return True iff *timeline* has any non-self row in ``(search, search+window]``.

    The timeline is sorted ascending by ``occurred_at``; we early-exit
    on the first action past the window so the loop stays O(actions-
    inside-window) rather than O(timeline) per search.
    """
    deadline = search_occurred_at + CONVERSION_WINDOW
    for action_ts, action_id in timeline:
        if action_ts <= search_occurred_at:
            continue
        if action_id == search_id:
            continue
        return action_ts <= deadline
    return False


def _build_buckets(
    *,
    search_rows: Iterable[Any],
    timelines: dict[tuple[str, UUID | None], list[tuple[datetime, UUID]]],
    path_to_surface: dict[str, str],
) -> tuple[list[DailyUsageBucket], int]:
    """Bucket the search rows into ``(date, surface)`` aggregates.

    Returns ``(buckets, total_searches)``. Empty input → ``([], 0)``.
    """
    searches: dict[tuple[date_type, str], int] = {}
    converted: dict[tuple[date_type, str], int] = {}
    operators: dict[tuple[date_type, str], set[str]] = {}
    total = 0

    for row in search_rows:
        surface = path_to_surface[row.path]
        key = (row.occurred_at.date(), surface)
        searches[key] = searches.get(key, 0) + 1
        operators.setdefault(key, set()).add(row.operator_sub)
        total += 1
        if _has_conversion(
            search_id=row.id,
            search_occurred_at=row.occurred_at,
            timeline=timelines.get((row.operator_sub, row.tenant_id), []),
        ):
            converted[key] = converted.get(key, 0) + 1

    buckets: list[DailyUsageBucket] = []
    for day, surface in sorted(searches.keys()):
        count = searches[(day, surface)]
        conv = converted.get((day, surface), 0)
        ops = len(operators.get((day, surface), set()))
        pct = round((conv / count) * 100.0, 2) if count else 0.0
        buckets.append(
            DailyUsageBucket(
                date=day,
                surface=surface,  # type: ignore[arg-type]
                search_count=count,
                distinct_operators=ops,
                action_conversion_pct=pct,
            ),
        )
    return buckets, total


async def compute_usage(
    *,
    session: AsyncSession,
    since: datetime,
    until: datetime,
    surfaces: Iterable[str],
    tenant_id: UUID | None,
) -> UsageReport:
    """Aggregate ``audit_log`` rows into a per-day per-surface report.

    Algorithm (three phases, each in a dedicated helper):

    1. :func:`_fetch_search_rows` — successful search-class rows in the
       requested window + tenant scope.
    2. :func:`_fetch_action_universe` — every audit_log row in the
       extended window (used to score conversion).
    3. :func:`_build_buckets` — Python correlation per ``(date, surface)``.

    Result: a fully-populated :class:`UsageReport`. Empty audit_log →
    empty buckets + ``total_searches=0`` (no buckets are emitted for
    surface-day pairs that had zero searches; the router consumer can
    render a "no data" placeholder).

    *session* is the caller-owned async session. Tests pass an
    in-memory SQLite session; the API route uses the request-scoped
    session from :func:`~meho_backplane.db.engine.get_sessionmaker`.
    The helper does not commit (it's read-only) and does not close
    the session (lifetime is the caller's concern).
    """
    surfaces_list = list(surfaces)
    paths, path_to_surface = _search_paths_for(surfaces_list)
    if not paths:
        # No surfaces in scope → empty report. Avoids issuing an
        # ``IN ()`` clause which PG rejects outright and SQLite
        # optimises into a constant-false table scan.
        return UsageReport(
            since=since,
            until=until,
            surfaces=surfaces_list,
            tenant_id=tenant_id,
            buckets=[],
            total_searches=0,
        )

    search_rows = await _fetch_search_rows(
        session,
        since=since,
        until=until,
        paths=paths,
        tenant_id=tenant_id,
    )
    action_rows = await _fetch_action_universe(
        session,
        since=since,
        until=until,
        tenant_id=tenant_id,
    )
    timelines = _index_actions_by_principal(action_rows)
    buckets, total = _build_buckets(
        search_rows=search_rows,
        timelines=timelines,
        path_to_surface=path_to_surface,
    )

    return UsageReport(
        since=since,
        until=until,
        surfaces=surfaces_list,
        tenant_id=tenant_id,
        buckets=buckets,
        total_searches=total,
    )
