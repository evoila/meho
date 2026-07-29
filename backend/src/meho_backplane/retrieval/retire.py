# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retire-decision checklist service for the retrieval surfaces (G4.3-T6, #445).

The load-bearing decision-support output of Initiative #373: combines
T2's eval results (precision@5 + MEHO-vs-baseline) with T5's audit-log-
backed usage telemetry (daily-use date + operator breadth) and an
externally-supplied count of open ``retrieval-migration-blocker``
issues (T7 #446 ships the GitHub label automation; the CLI runs
``gh issue list`` locally and passes the count in via the request
body) to compute the five-criterion checklist locked by Goal #215
decision #2 and the Initiative-body acceptance criteria.

Verdict bands
-------------

Per-criterion green / yellow / red follows the same threshold-contract
shape that ``retrieval.eval.metrics`` established (yellow floor =
:data:`YELLOW_FLOOR_RATIO` x green). The contract is encoded in this
module so the CI gate is reproducible from the same numbers operators
see in the CLI table:

==============================  ========  ===============  =======
Criterion                       Green     Yellow           Red
==============================  ========  ===============  =======
1. days since first daily use   >= 30     [21, 30)         < 21
2. qualified operators          >= 3      == 2             <= 1
3. eval precision@5             >= 0.80   [0.56, 0.80)     < 0.56
4. MEHO vs baseline             all >=    baseline absent  any worse
5. open blocker issues          == 0      None (not run)   >= 1
==============================  ========  ===============  =======

A "qualified operator" (criterion 2) is one with ≥1 search per ISO
week for ≥ :data:`MIN_OPERATOR_STREAK_WEEKS` consecutive weeks. The
streak is computed in :func:`_qualified_operator_count` from a single
audit_log scan; the same surface→path mapping that
:mod:`meho_backplane.retrieval.usage` ships drives the filter.

Overall verdict
---------------

* **READY TO RETIRE** — every criterion green.
* **REVIEW MANUALLY** — at least one yellow, no red.
* **NOT YET** — any criterion red.

The overall report verdict is the worst across all surfaces in scope
(matches the operator intuition "we retire the slowest surface last";
mirrors :func:`meho_backplane.retrieval.eval.runner._worst_verdict`).

Why a service module instead of inlining in the router
------------------------------------------------------

The retire-decision call is the third composer of two audit-trail-
backed signals (usage + eval). Encoding the threshold contract +
verdict math in a service module keeps:

* The router thin (request-body validation + tenant-filter gating +
  audit overrides) and surface-agnostic.
* The verdict math unit-testable against synthetic data without
  spinning up FastAPI.
* The criterion shape stable for v0.2.next consumers (a future
  retire-checklist alert / dashboard reads the same shape).

References
----------

* Parent Initiative #373 (G4.3 retrieval migration tooling).
* Parent Task #445 (this Task).
* :mod:`meho_backplane.retrieval.usage` — audit-log aggregation reused
  for the streak math.
* :mod:`meho_backplane.retrieval.eval.runner` — eval pipeline whose
  ``EvalResult`` feeds criteria 3 + 4.
* Decision #2: ``docs/decisions/locked-decisions.md`` L39-L43.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog
from meho_backplane.retrieval.eval.runner import (
    DEFAULT_K,
    RetrieveCallable,
    eval_all,
)
from meho_backplane.retrieval.usage import (
    COUNTED_SEARCH_SURFACES,
    MCP_TOOL_PATH_PREFIX,
    REST_RETRIEVE_EXCLUDED,
    SEARCH_OPS,
    SUPPORTED_SURFACES,
)

__all__ = [
    "EVAL_PRECISION_GREEN",
    "MIN_DAYS_SINCE_FIRST_USE",
    "MIN_OPERATOR_STREAK_WEEKS",
    "MIN_QUALIFIED_OPERATORS",
    "RETIRE_LOOKBACK",
    "SURFACE_VERDICT_ORDER",
    "YELLOW_FLOOR_RATIO",
    "BaselineMetricsOverride",
    "ChecklistSurface",
    "ChecklistVerdict",
    "CriterionName",
    "CriterionResult",
    "RetireChecklistReport",
    "SurfaceChecklist",
    "compute_retire_checklist",
]

#: Per-criterion green-band thresholds (criteria 1 + 2 + 3).
MIN_DAYS_SINCE_FIRST_USE: Final[int] = 30
MIN_QUALIFIED_OPERATORS: Final[int] = 3
MIN_OPERATOR_STREAK_WEEKS: Final[int] = 4
EVAL_PRECISION_GREEN: Final[float] = 0.80

#: Yellow floor as a ratio of green — matches
#: :data:`meho_backplane.retrieval.eval.metrics.YELLOW_FLOOR_RATIO` so
#: the retire-checklist + eval gates use the same operator-facing
#: contract. Centralising the ratio means a future re-tune touches one
#: place across both verbs.
YELLOW_FLOOR_RATIO: Final[float] = 0.70

#: Audit-log lookback window for criterion 1 + 2. 90 days comfortably
#: covers the 30-day daily-use criterion and the 4-week-streak window
#: with a safety margin: an operator with usage from day -90 to day -10
#: still shows green for criterion 1 (first use ≥ 30d ago) and the
#: streak check observes every week that could contribute.
RETIRE_LOOKBACK: Final[timedelta] = timedelta(days=90)

#: Surface labels honoured by the checklist. Aligned with
#: :data:`meho_backplane.retrieval.usage.SUPPORTED_SURFACES`.
ChecklistSurface = Literal["kb", "memory", "operations"]

#: Per-surface + overall verdict tokens. Three-state to preserve the
#: yellow "look again" signal alongside the binary retire / hold.
ChecklistVerdict = Literal["READY TO RETIRE", "REVIEW MANUALLY", "NOT YET"]

#: Stable canonical criterion identifiers — the CLI table renderer and
#: the JSON consumers key off these. Order locked here so renderings
#: stay consistent across runs.
CriterionName = Literal[
    "daily_use_duration",
    "operator_breadth",
    "eval_precision",
    "meho_vs_baseline",
    "open_blockers",
]

#: Verdict ordering for ``_worst_band`` and the overall composition.
#: Index 0 is the best band.
_BAND_ORDER: Final[tuple[str, ...]] = ("green", "yellow", "red")

#: Display ordering for surfaces in the response + CLI table. Matches
#: the parent Initiative's narrative order (kb first because it's the
#: closest-to-retire surface in v0.2).
SURFACE_VERDICT_ORDER: Final[tuple[ChecklistSurface, ...]] = (
    "kb",
    "memory",
    "operations",
)


class CriterionResult(BaseModel):
    """One row of the per-surface checklist.

    Frozen + ``extra="forbid"`` so the structured output is stable for
    the CLI table renderer and any future dashboard consumer. The
    ``observed_value`` + ``threshold_summary`` pair is what the
    human-readable table prints verbatim — keep both short (≤ 32
    chars each) so the table stays scannable in an 80-column terminal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: CriterionName
    verdict: Literal["green", "yellow", "red"]
    observed_value: str = Field(max_length=64)
    threshold_summary: str = Field(max_length=64)
    notes: str | None = Field(default=None, max_length=256)


class BaselineMetricsOverride(BaseModel):
    """Per-surface baseline metrics supplied by the caller.

    Criterion 4 (MEHO ≥ baseline) needs side-by-side numbers from a
    baseline retrieval (``grep -r kb/`` for kb; ``grep paths.txt +
    yq`` for operations). The v0.2 backplane has no checked-in
    corpus snapshot to evaluate the baseline against (see
    :mod:`meho_backplane.api.v1.retrieve_eval` — explicit
    ``baseline=grep`` on that route is rejected with 501); the CLI
    runs the baseline locally against the operator's kb/ checkout and
    can pass the resulting numbers here so the retire-checklist
    verdict honestly reflects criterion 4 instead of always reporting
    yellow ("baseline did not run") on the API path.

    Without an override, criterion 4 stays yellow for v0.2 production
    callers — the documented "READY TO RETIRE" path is unreachable
    via the bare API alone, which is the honest v0.2 posture. T7
    (#446) / v0.2.next is expected to wire a server-side corpus
    snapshot and remove the need for this override.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    precision_at_5: float = Field(ge=0.0, le=1.0)
    mrr: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    kind: str = Field(min_length=1, max_length=16, default="grep")


class SurfaceChecklist(BaseModel):
    """Per-surface checklist: the five criteria + the surface verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: ChecklistSurface
    verdict: ChecklistVerdict
    criteria: list[CriterionResult]


class RetireChecklistReport(BaseModel):
    """Top-level shape returned by :func:`compute_retire_checklist`.

    Frozen + ``extra="forbid"`` so the CLI's ``--json`` consumers can
    pin the shape; ``ran_at`` lets two saved reports be compared
    chronologically without external metadata.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ran_at: datetime
    tenant_id: uuid.UUID | None
    since: datetime
    until: datetime
    surfaces: list[SurfaceChecklist]
    overall_verdict: ChecklistVerdict

    #: The fully-qualified surface labels whose audit_log rows feed
    #: criterion 1 (days since first daily use) and criterion 2
    #: (operator breadth) — the same audited MCP search meta-tools
    #: :mod:`meho_backplane.retrieval.usage` counts. Surfaced here so an
    #: operator whose overlap clock is stuck (criteria 1 + 2 perpetually
    #: red) can see *why*: REST ``POST /api/v1/retrieve`` does not feed
    #: these criteria; only the audited ``/mcp`` search tools do.
    #: Defaulted from :data:`COUNTED_SEARCH_SURFACES` so the verdict
    #: surface cannot drift from the audit-log scan filter.
    counted_surfaces: list[str] = Field(
        default_factory=lambda: list(COUNTED_SEARCH_SURFACES),
    )

    #: ``True`` whenever REST ``POST /api/v1/retrieve`` is excluded from
    #: the audit-log scan that feeds the daily-use + operator-breadth
    #: criteria (always, in v0.2 — see
    #: :data:`meho_backplane.retrieval.usage.REST_RETRIEVE_EXCLUDED`).
    #: De-silences the "GREEN never arrives" trap for a REST-only
    #: dogfood: the retire decision is fed by the counted MCP surface,
    #: not REST ``/retrieve``.
    rest_excluded: bool = Field(default=REST_RETRIEVE_EXCLUDED)


# ---------------------------------------------------------------------------
# Verdict primitives — small pure helpers for unit testing each band
# ---------------------------------------------------------------------------


def _worst_band(
    bands: Iterable[Literal["green", "yellow", "red"]],
) -> Literal["green", "yellow", "red"]:
    """Return the worst (most-red) band; ``green`` for an empty input."""
    worst_idx = 0
    for band in bands:
        worst_idx = max(worst_idx, _BAND_ORDER.index(band))
    return _BAND_ORDER[worst_idx]  # type: ignore[return-value]


def _band_to_surface_verdict(
    band: Literal["green", "yellow", "red"],
) -> ChecklistVerdict:
    """Map the worst per-criterion band to the surface-level verdict token."""
    if band == "red":
        return "NOT YET"
    if band == "yellow":
        return "REVIEW MANUALLY"
    return "READY TO RETIRE"


# ---------------------------------------------------------------------------
# Criterion 1 — days since first daily use
# ---------------------------------------------------------------------------


def _evaluate_daily_use_duration(
    *,
    first_use: datetime | None,
    now: datetime,
) -> CriterionResult:
    """Build criterion 1 (>=1 month elapsed since first daily-use date).

    *first_use* is the earliest ``occurred_at`` for a successful
    search row on this surface inside the lookback window, or
    ``None`` if no search rows landed inside the window. ``None`` is
    red — without any audit-log evidence the surface hasn't entered
    daily-use territory at all.
    """
    if first_use is None:
        return CriterionResult(
            name="daily_use_duration",
            verdict="red",
            observed_value="no usage in window",
            threshold_summary=f">= {MIN_DAYS_SINCE_FIRST_USE} days",
            notes="no successful search rows found in the lookback window",
        )

    days = max(0, (now - first_use).days)
    yellow_floor = int(MIN_DAYS_SINCE_FIRST_USE * YELLOW_FLOOR_RATIO)
    if days >= MIN_DAYS_SINCE_FIRST_USE:
        band: Literal["green", "yellow", "red"] = "green"
    elif days >= yellow_floor:
        band = "yellow"
    else:
        band = "red"
    return CriterionResult(
        name="daily_use_duration",
        verdict=band,
        observed_value=f"{days} days since first use",
        threshold_summary=f">= {MIN_DAYS_SINCE_FIRST_USE} days",
    )


# ---------------------------------------------------------------------------
# Criterion 2 — operator breadth via 4-consecutive-week streaks
# ---------------------------------------------------------------------------


def _iso_week_key(occurred_at: datetime) -> tuple[int, int]:
    """Return the ``(iso_year, iso_week)`` tuple for *occurred_at*.

    Tuple keying gives ISO-correct ordering across year boundaries —
    week 53 of 2026 sorts before week 1 of 2027 because tuples compare
    lexicographically and 2026 < 2027. Bare-int week numbers would
    sort week 53 *after* week 1 of the next year and break the streak
    math.
    """
    iso = occurred_at.isocalendar()
    return (iso.year, iso.week)


def _longest_consecutive_streak(weeks: Iterable[tuple[int, int]]) -> int:
    """Return the longest run of consecutive ISO weeks in *weeks*.

    Weeks are walked in sort order; two adjacent weeks are *consecutive*
    if the second is the ISO calendar's immediate successor (same year
    + week+1, or next ISO year + week 1 after the prior ISO year's
    final week). Computing successor-week directly via
    :class:`datetime` arithmetic dodges the ISO-week-count edge cases
    (52- vs 53-week years).
    """
    sorted_weeks = sorted(set(weeks))
    if not sorted_weeks:
        return 0

    longest = 1
    current = 1
    for prev, cur in pairwise(sorted_weeks):
        # Pin a date in *prev*'s ISO week (Monday) and ask which ISO
        # week is 7 days later. That's the calendar-correct successor
        # without manually enumerating 52- vs 53-week ISO years.
        prev_monday = datetime.fromisocalendar(prev[0], prev[1], 1)
        successor_iso = (prev_monday + timedelta(days=7)).isocalendar()
        successor = (successor_iso.year, successor_iso.week)
        if cur == successor:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _qualified_operator_count(
    operator_weeks: Mapping[str, set[tuple[int, int]]],
) -> int:
    """Count operators whose longest ISO-week streak meets the threshold.

    A "qualified operator" has at least
    :data:`MIN_OPERATOR_STREAK_WEEKS` consecutive ISO weeks of activity
    on the surface. Each operator's streak is computed independently —
    the criterion is about *breadth* of daily users, not about a single
    operator carrying the surface.
    """
    qualified = 0
    for weeks in operator_weeks.values():
        if _longest_consecutive_streak(weeks) >= MIN_OPERATOR_STREAK_WEEKS:
            qualified += 1
    return qualified


def _evaluate_operator_breadth(
    *,
    operator_weeks: Mapping[str, set[tuple[int, int]]],
) -> CriterionResult:
    """Build criterion 2 (>=3 distinct operators with >=4-wk streaks)."""
    qualified = _qualified_operator_count(operator_weeks)
    yellow_floor = max(1, int(MIN_QUALIFIED_OPERATORS * YELLOW_FLOOR_RATIO))
    if qualified >= MIN_QUALIFIED_OPERATORS:
        band: Literal["green", "yellow", "red"] = "green"
    elif qualified >= yellow_floor:
        band = "yellow"
    else:
        band = "red"
    return CriterionResult(
        name="operator_breadth",
        verdict=band,
        observed_value=f"{qualified} qualified operators",
        threshold_summary=(
            f">= {MIN_QUALIFIED_OPERATORS} operators x "
            f">= {MIN_OPERATOR_STREAK_WEEKS} consecutive weeks"
        ),
    )


# ---------------------------------------------------------------------------
# Criterion 3 — eval precision@5
# ---------------------------------------------------------------------------


def _evaluate_eval_precision(
    *,
    precision_at_5: float | None,
    query_count: int,
) -> CriterionResult:
    """Build criterion 3 (eval precision@5 >= 0.80).

    *precision_at_5* is the corpus-aggregate precision number from the
    eval runner. ``None`` means the eval surface didn't run (e.g. the
    corpus hasn't shipped yet — memory in T4 #443, operations in T3
    #442). An absent corpus is red here (not green) — the verdict
    contract requires *evidence* of equivalence, and an empty corpus
    provides none.
    """
    if precision_at_5 is None or query_count == 0:
        return CriterionResult(
            name="eval_precision",
            verdict="red",
            observed_value="no corpus shipped",
            threshold_summary=f">= {EVAL_PRECISION_GREEN:.2f}",
            notes="eval corpus has zero queries; ship the surface corpus first",
        )

    yellow_floor = EVAL_PRECISION_GREEN * YELLOW_FLOOR_RATIO
    if precision_at_5 >= EVAL_PRECISION_GREEN:
        band: Literal["green", "yellow", "red"] = "green"
    elif precision_at_5 >= yellow_floor:
        band = "yellow"
    else:
        band = "red"
    return CriterionResult(
        name="eval_precision",
        verdict=band,
        observed_value=f"precision@5 = {precision_at_5:.3f}",
        threshold_summary=f">= {EVAL_PRECISION_GREEN:.2f}",
    )


# ---------------------------------------------------------------------------
# Criterion 4 — MEHO ranking vs baseline on every metric
# ---------------------------------------------------------------------------


def _evaluate_meho_vs_baseline(
    *,
    baseline_kind: str | None,
    meho_metrics: tuple[float, float, float] | None,
    baseline_metrics: tuple[float, float, float] | None,
) -> CriterionResult:
    """Build criterion 4 (MEHO ranking >= baseline on every metric).

    The eval runner's ``_apply_baseline_check`` already encodes "any
    metric below baseline ⇒ red"; we mirror the comparison here so the
    criterion result carries the per-metric observation rather than
    just inheriting the eval verdict. Yellow is reserved for
    "baseline did not run" (memory + operations surfaces lack a
    baseline in v0.2 per the eval runner's docstring); green for
    "every metric >= baseline" with the epsilon the runner uses.
    """
    if baseline_kind is None or meho_metrics is None or baseline_metrics is None:
        return CriterionResult(
            name="meho_vs_baseline",
            verdict="yellow",
            observed_value="baseline did not run",
            threshold_summary="every metric >= baseline",
            notes="no baseline corpus configured for this surface in v0.2",
        )

    # 1e-9 epsilon mirrors runner._apply_baseline_check to avoid
    # tripping on floating-point drift when MEHO and baseline are
    # mathematically equal but represented as very-close floats.
    losses = [
        (label, m, b)
        for label, m, b in zip(
            ("precision@5", "mrr", "coverage"),
            meho_metrics,
            baseline_metrics,
            strict=True,
        )
        if m < b - 1e-9
    ]
    if losses:
        label, m_val, b_val = losses[0]
        return CriterionResult(
            name="meho_vs_baseline",
            verdict="red",
            observed_value=f"{label} {m_val:.3f} < baseline {b_val:.3f}",
            threshold_summary="every metric >= baseline",
            notes=f"baseline kind: {baseline_kind}",
        )
    return CriterionResult(
        name="meho_vs_baseline",
        verdict="green",
        observed_value="every metric >= baseline",
        threshold_summary="every metric >= baseline",
        notes=f"baseline kind: {baseline_kind}",
    )


# ---------------------------------------------------------------------------
# Criterion 5 — open retrieval-migration-blocker issues
# ---------------------------------------------------------------------------


def _evaluate_open_blockers(
    *,
    blocker_count: int | None,
) -> CriterionResult:
    """Build criterion 5 (zero open ``retrieval-migration-blocker`` issues).

    *blocker_count* is supplied by the caller — the CLI runs
    ``gh issue list --label retrieval-migration-blocker --state open``
    locally and passes the surface-bucketed count in the request body.
    ``None`` is yellow rather than green / red — the verdict cannot be
    proven either way until T7 (#446) wires the label automation +
    operators have run the gh lookup at least once. T7 ships the
    automation; the v0.2 retire-checklist treats an unknown count as
    "review manually" so an operator's call is required.
    """
    if blocker_count is None:
        return CriterionResult(
            name="open_blockers",
            verdict="yellow",
            observed_value="unknown",
            threshold_summary="== 0 open blockers",
            notes=(
                "blocker count not provided; CLI did not run the gh lookup "
                "(label automation lands in T7 #446)"
            ),
        )
    if blocker_count == 0:
        band: Literal["green", "yellow", "red"] = "green"
    else:
        band = "red"
    return CriterionResult(
        name="open_blockers",
        verdict=band,
        observed_value=f"{blocker_count} open",
        threshold_summary="== 0 open blockers",
    )


# ---------------------------------------------------------------------------
# Audit-log scan — per-surface first-use date + operator weekly activity
# ---------------------------------------------------------------------------


def _surface_search_paths() -> dict[str, ChecklistSurface]:
    """Return the audit_log ``path`` → surface mapping for every surface.

    Mirrors :func:`meho_backplane.retrieval.usage._search_paths_for`
    but keyed on every surface unconditionally; the retire-checklist
    always inspects every surface in the audit-log scan, then narrows
    after aggregation. Centralising the mapping in
    :data:`SEARCH_OPS` (usage.py) keeps the surface→tool wiring in one
    place.
    """
    # ``SEARCH_OPS`` types its values as ``str``; cast to the narrower
    # ``ChecklistSurface`` literal at this single boundary so downstream
    # consumers stay surface-typed without re-validating on every call.
    return {
        f"{MCP_TOOL_PATH_PREFIX}{op_id}": cast("ChecklistSurface", surface)
        for op_id, surface in SEARCH_OPS.items()
    }


async def _fetch_surface_audit_scan(
    *,
    session: AsyncSession,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
) -> tuple[
    dict[ChecklistSurface, datetime],
    dict[ChecklistSurface, dict[str, set[tuple[int, int]]]],
]:
    """Single audit-log pass yielding the first-use date + operator weeks.

    Returns:

    * ``first_use``: ``{surface: earliest_occurred_at}`` for every
      surface that had at least one successful search row.
    * ``operator_weeks``: ``{surface: {operator_sub: {iso_week_tuple}}}``
      for the streak math.

    Surfaces with zero rows are absent from both dicts; the caller
    treats absence as "no signal" and the criteria evaluators report
    that as red (criterion 1) + red (criterion 2 — zero qualified
    operators).
    """
    path_to_surface = _surface_search_paths()
    paths = list(path_to_surface.keys())
    if not paths:
        # Defensive: SEARCH_OPS is non-empty, but a misconfiguration
        # there would otherwise produce ``IN ()`` which PG rejects.
        return {}, {}

    stmt = (
        select(
            AuditLog.occurred_at,
            AuditLog.operator_sub,
            AuditLog.path,
        )
        .where(AuditLog.occurred_at >= since)
        .where(AuditLog.occurred_at <= until)
        .where(AuditLog.path.in_(paths))
        .where(AuditLog.status_code == 200)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)

    first_use: dict[ChecklistSurface, datetime] = {}
    operator_weeks: dict[ChecklistSurface, dict[str, set[tuple[int, int]]]] = {}

    result = await session.execute(stmt)
    for occurred_at, operator_sub, path in result.all():
        # SQLite returns ``occurred_at`` as a naive datetime; PG returns
        # it timezone-aware. Normalise to UTC at the read boundary so
        # downstream arithmetic against the tz-aware ``now`` clock
        # (production callers pass ``datetime.now(UTC)``) doesn't trip
        # the "can't subtract offset-naive and offset-aware" TypeError.
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        surface = path_to_surface[path]
        prior = first_use.get(surface)
        if prior is None or occurred_at < prior:
            first_use[surface] = occurred_at
        week = _iso_week_key(occurred_at)
        operator_weeks.setdefault(surface, {}).setdefault(operator_sub, set()).add(week)

    return first_use, operator_weeks


# ---------------------------------------------------------------------------
# Top-level service entrypoint
# ---------------------------------------------------------------------------


def _compose_surface(
    *,
    surface: ChecklistSurface,
    criteria: list[CriterionResult],
) -> SurfaceChecklist:
    """Fold the five criteria into a per-surface verdict + checklist."""
    worst = _worst_band([c.verdict for c in criteria])
    return SurfaceChecklist(
        surface=surface,
        verdict=_band_to_surface_verdict(worst),
        criteria=criteria,
    )


def _compose_overall(surfaces: list[SurfaceChecklist]) -> ChecklistVerdict:
    """Worst-of every surface verdict — the report-level verdict token."""
    worst_band = _worst_band(
        [
            "red"
            if s.verdict == "NOT YET"
            else "yellow"
            if s.verdict == "REVIEW MANUALLY"
            else "green"
            for s in surfaces
        ]
    )
    return _band_to_surface_verdict(worst_band)


def _resolve_eval_lookup(
    eval_result_surfaces: Iterable[object],
) -> dict[
    ChecklistSurface,
    tuple[
        float,
        int,
        str | None,
        tuple[float, float, float] | None,
        tuple[float, float, float] | None,
    ],
]:
    """Index ``EvalResult.surfaces`` by surface label.

    Each value is ``(precision_at_5, query_count, baseline_kind,
    meho_metrics, baseline_metrics)``. Returning a flat tuple instead
    of the full :class:`SurfaceResult` keeps the criteria evaluators
    surface-agnostic — they take per-criterion primitives, not the
    eval runner's nested shape.
    """
    lookup: dict[
        ChecklistSurface,
        tuple[
            float,
            int,
            str | None,
            tuple[float, float, float] | None,
            tuple[float, float, float] | None,
        ],
    ] = {}
    for surface_result in eval_result_surfaces:
        # The eval runner ships a typed SurfaceResult; we accept any
        # object with the matching attribute shape so test fakes don't
        # have to construct the full Pydantic model.
        surface = getattr(surface_result, "surface", None)
        if surface not in SUPPORTED_SURFACES:
            continue
        precision = getattr(surface_result, "precision_at_5", 0.0)
        query_count = getattr(surface_result, "query_count", 0)
        baseline_kind = getattr(surface_result, "baseline_kind", None)
        meho_metrics: tuple[float, float, float] | None = (
            precision,
            getattr(surface_result, "mrr", 0.0),
            getattr(surface_result, "coverage", 0.0),
        )
        if baseline_kind is None:
            baseline_metrics: tuple[float, float, float] | None = None
        else:
            baseline_metrics = (
                getattr(surface_result, "baseline_precision_at_5", 0.0) or 0.0,
                getattr(surface_result, "baseline_mrr", 0.0) or 0.0,
                getattr(surface_result, "baseline_coverage", 0.0) or 0.0,
            )
        lookup[surface] = (
            precision,
            query_count,
            baseline_kind,
            meho_metrics,
            baseline_metrics,
        )
    return lookup


async def compute_retire_checklist(
    *,
    session: AsyncSession,
    surfaces: Iterable[ChecklistSurface],
    tenant_id: uuid.UUID | None,
    retrieve_fn: RetrieveCallable | None = None,
    blocker_counts: Mapping[ChecklistSurface, int] | None = None,
    baseline_overrides: Mapping[ChecklistSurface, BaselineMetricsOverride] | None = None,
    now: datetime | None = None,
    k: int = DEFAULT_K,
) -> RetireChecklistReport:
    """Build the five-criterion retire-decision checklist per surface.

    Algorithm:

    1. Run the audit-log scan once over the lookback window to derive
       per-surface first-use date + per-operator ISO-week sets.
    2. Run :func:`meho_backplane.retrieval.eval.runner.eval_all` once
       (narrowed to the *requested* surfaces, not every supported
       surface) to populate criteria 3 + 4.
    3. For each requested surface, compose the five criteria into a
       :class:`SurfaceChecklist`.
    4. Fold every surface into a :class:`RetireChecklistReport` with
       the worst-of overall verdict.

    *retrieve_fn* is passed through to ``eval_all`` so tests can inject
    a stub without standing up fastembed + PG. *blocker_counts* is
    the surface→count mapping the CLI provides from
    ``gh issue list``; ``None`` (or surface absence) maps to
    criterion 5 = yellow per :func:`_evaluate_open_blockers`.
    *baseline_overrides* is the optional per-surface baseline-metrics
    mapping the caller (CLI) supplies after running the grep baseline
    locally; without it, criterion 4 stays yellow for v0.2 production
    callers because the backplane has no server-side corpus snapshot
    to compute the baseline against (see
    :class:`BaselineMetricsOverride`). *now* defaults to
    ``datetime.now(UTC)`` and is exposed so tests can pin a frozen
    clock for the first-use-date math.
    """
    moment = now or datetime.now(UTC)
    since = moment - RETIRE_LOOKBACK
    surfaces_list = list(surfaces)

    first_use, operator_weeks = await _fetch_surface_audit_scan(
        session=session,
        since=since,
        until=moment,
        tenant_id=tenant_id,
    )

    # ``eval_all`` is tenant-scoped per its own contract; pass through
    # the resolved tenant_id (cross-tenant retire-checklists invoke
    # the eval against that other tenant's corpus, which is the
    # operator-intuitive shape: "is tenant_X ready to retire kb/?"
    # asks about tenant_X's eval numbers).
    eval_tenant = tenant_id if tenant_id is not None else uuid.UUID(int=0)
    # ``surfaces`` is typed as ``Iterable[ChecklistSurface]``; the eval
    # runner's ``surfaces`` parameter expects the matching narrow
    # literal. Cast at this single call boundary so single-surface
    # requests (``surfaces=["kb"]``) skip the eval cost on the other
    # two surfaces instead of always paying it.
    eval_surfaces = cast(
        "list[Literal['kb', 'memory', 'operations']]",
        list(surfaces_list),
    )
    eval_result = await eval_all(
        tenant_id=eval_tenant,
        retrieve_fn=retrieve_fn,
        surfaces=eval_surfaces,
        k=k,
    )
    eval_lookup = _resolve_eval_lookup(eval_result.surfaces)

    surface_reports: list[SurfaceChecklist] = []
    for surface in surfaces_list:
        # Criterion 1 — days since first use.
        c1 = _evaluate_daily_use_duration(
            first_use=first_use.get(surface),
            now=moment,
        )
        # Criterion 2 — operator breadth.
        c2 = _evaluate_operator_breadth(
            operator_weeks=operator_weeks.get(surface, {}),
        )
        # Criteria 3 + 4 — eval-driven.
        # The override resolution lives at this single point: a
        # caller-supplied ``BaselineMetricsOverride`` supersedes
        # whatever the in-process eval runner produced for the
        # surface's baseline triple. Without an override criterion 4
        # falls through to the eval runner's own ``baseline_kind`` /
        # ``baseline_metrics`` (which are ``None`` on the v0.2 API
        # path — no server-side corpus snapshot).
        override = baseline_overrides.get(surface) if baseline_overrides is not None else None
        eval_entry = eval_lookup.get(surface)
        if eval_entry is None:
            c3 = _evaluate_eval_precision(precision_at_5=None, query_count=0)
            meho_metrics_for_c4: tuple[float, float, float] | None = None
            baseline_kind_for_c4: str | None = None
            baseline_metrics_for_c4: tuple[float, float, float] | None = None
        else:
            (precision, query_count, baseline_kind, meho_metrics, baseline_metrics) = eval_entry
            c3 = _evaluate_eval_precision(
                precision_at_5=precision if query_count > 0 else None,
                query_count=query_count,
            )
            meho_metrics_for_c4 = meho_metrics
            baseline_kind_for_c4 = baseline_kind
            baseline_metrics_for_c4 = baseline_metrics

        if override is not None and meho_metrics_for_c4 is not None:
            # Caller supplied baseline metrics — use them to evaluate
            # criterion 4 against the MEHO numbers from the in-process
            # eval. This is the v0.2 path for reaching "green" on
            # criterion 4 (the API has no server-side corpus snapshot
            # to compute the baseline itself).
            baseline_kind_for_c4 = override.kind
            baseline_metrics_for_c4 = (
                override.precision_at_5,
                override.mrr,
                override.coverage,
            )

        c4 = _evaluate_meho_vs_baseline(
            baseline_kind=baseline_kind_for_c4,
            meho_metrics=meho_metrics_for_c4,
            baseline_metrics=baseline_metrics_for_c4,
        )
        # Criterion 5 — open blockers from the CLI-supplied count.
        blocker_count = blocker_counts.get(surface) if blocker_counts is not None else None
        c5 = _evaluate_open_blockers(blocker_count=blocker_count)

        surface_reports.append(
            _compose_surface(surface=surface, criteria=[c1, c2, c3, c4, c5]),
        )

    return RetireChecklistReport(
        ran_at=moment,
        tenant_id=tenant_id,
        since=since,
        until=moment,
        surfaces=surface_reports,
        overall_verdict=_compose_overall(surface_reports),
    )
