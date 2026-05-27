# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

# code-quality-allow: file-size — this module is the single coherent
# pre-execution budget gate (decision types + ladder + the
# evaluate_pre_run_budget pipeline + the helpers it factors into).
# Splitting it would either fragment the unit (decision types in one
# file, the pipeline in another, no readability gain) or recreate the
# same dense docstring discipline the sibling identity_budget.py
# module already follows. The body is ~280 lines of code; the rest is
# the per-decision rationale documentation Initiative #806 requires
# for an audit-row reason ("I refused because X").

"""Pre-execution budget enforcement + graceful-degradation policy + kill switch.

Initiative #806 (G11.5 Portability + cost), Task #1080 (G11.5-T6 /
C3-b). The companion to the **observational-only**
:mod:`~meho_backplane.operations.identity_budget` service (#1079):
where that module records consumption *after* a successful run, this
one decides *whether a run may start at all* — and, when the answer is
"yes but cheaper", which tier the runtime should resolve instead of
the operator-requested one.

Three knobs, one decision
-------------------------

A single :func:`evaluate_pre_run_budget` call rolls every gate into one
:class:`BudgetDecision` the
:class:`~meho_backplane.agent.invocation.AgentInvoker` consumes before
it commits the durable ``agent_run`` row:

1. **Kill switch.** A global flag
   (:attr:`Settings.agent_runs_disabled_global`) and a per-tenant list
   (:attr:`Settings.agent_runs_disabled_tenants`) refuse a run before
   any DB read happens. Per-identity kill is the existing
   ``identity_budget.request_limit = 0`` row written via
   :func:`~meho_backplane.operations.identity_budget.set_limits` — the
   read picks it up alongside every other cap, so the same single read
   serves both "configured cap reached" and "operator manually
   disabled this principal".
2. **Refusal at the cap.** When *any* of the three active windows
   (daily / weekly / monthly) shows ``consumed >= limit`` on *any*
   limited dimension (tokens / cost / requests),
   :func:`evaluate_pre_run_budget` returns
   :class:`BudgetDecision` ``REFUSE``. The invoker raises
   :class:`~meho_backplane.agent.run.BudgetExceededError`; no run row
   is created.
3. **Graceful degradation at the threshold.** When *any* window
   crosses :attr:`Settings.agent_budget_degrade_threshold` (default 0.8
   = 80%) on tokens or cost but no dimension is yet at the cap, the
   policy *downgrades the resolved tier one step* along the
   :data:`TIER_DOWNGRADE_LADDER` (INVESTIGATE → SUMMARIZE → TRIAGE).
   :class:`BudgetDecision` ``ALLOW`` carries the (possibly
   substituted) :class:`AgentTier`; the invoker hands that tier to the
   resolver instead of the original.

   TRIAGE has no cheaper step, so a TRIAGE request that crosses the
   threshold but not the cap is allowed to run unchanged — the hard
   cap is the only remaining gate.

Why not subtract "this run's predicted cost" before the call?
-------------------------------------------------------------

A multi-turn agent loop's token cost is not knowable ex-ante: it
depends on the model's tool-call shape, retrieval payload sizes, and
the conversation length the loop converges to. The v0.2 contract is
*"this run consumes one request (known) and an unknown number of
tokens"* — so the pre-check uses ``requests_consumed`` as the
guaranteed +1 increment but charges tokens / cost only against the
*already-recorded* state. A run that pushes a window over the cap
mid-flight is recorded faithfully by
:func:`~meho_backplane.operations.identity_budget.apply_consumption`
afterwards, and the *next* run hits the cap pre-flight. The window's
last spike is the inevitable cost of any per-window-bucket scheme
that doesn't know per-call token budgets ex-ante (a reservation
protocol is a v0.3 question, not v0.2).

Why three windows, all-of-them gate
-----------------------------------

The identity_budget table records one bucket per
:class:`BudgetWindowKind` per principal-window-start. A run that's
under-budget for the day might be over-budget for the month (mid-month
cumulative overrun even though today's bucket is fresh). The gate
fires on the worst-case across the three buckets — the conservative
read consistent with the C3-b intent "stays under the configured
budget".

What this module deliberately doesn't do
----------------------------------------

* **Persist enforcement state.** Every decision is computed from the
  current row state; no row mutations here. Consumption mutations
  happen in :mod:`~meho_backplane.operations.identity_budget`.
* **Hold a long-lived state across runs.** Stateless pure functions
  + a read of the current settings + a read of the three windows.
  Tests construct an :class:`EnforcementContext` directly to drive
  decisions deterministically without monkeypatching settings.
* **Resolve the tier persistence shape.** The M1 persistence-wiring
  + ``AgentModelTier`` ↔ :class:`AgentTier` enum unification is
  deferred to a follow-up (see the TODO in
  :meth:`~meho_backplane.agent.invocation.AgentInvoker._to_agent_definition`).
  Until then, the enforcement gate runs against the
  :attr:`AgentDefinition.tier` already on the definition value object
  — when the persistence wiring lands, that field will be populated
  from the row and the enforcement gate picks it up unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.models import AgentTier
from meho_backplane.db.models import BudgetWindowKind
from meho_backplane.operations.identity_budget import BudgetReading, get_remaining
from meho_backplane.settings import Settings

__all__ = [
    "TIER_DOWNGRADE_LADDER",
    "BudgetDecision",
    "BudgetDecisionKind",
    "BudgetEnforcementSnapshot",
    "EnforcementContext",
    "evaluate_pre_run_budget",
    "parse_disabled_tenants",
]


_log = structlog.get_logger(__name__)


#: One-step-cheaper successor for each tier the runtime resolves.
#:
#: Reading: INVESTIGATE downgrades to SUMMARIZE; SUMMARIZE downgrades to
#: TRIAGE; TRIAGE has no cheaper step (it is the always-on watcher tier
#: per :class:`AgentTier`). A tier missing from this map is treated as
#: "no cheaper step"; the policy then allows the run unchanged so the
#: hard cap is the only remaining gate.
#:
#: The ladder is intentionally a single step per evaluation — a run
#: that crosses the threshold once gets a one-rung drop, not a fall to
#: the floor. Multi-rung degradation would land surprising routing
#: (operator asked for INVESTIGATE; runtime ran TRIAGE) without giving
#: the operator a chance to notice the threshold trip in the logs.
TIER_DOWNGRADE_LADDER: Final[dict[AgentTier, AgentTier]] = {
    AgentTier.INVESTIGATE: AgentTier.SUMMARIZE,
    AgentTier.SUMMARIZE: AgentTier.TRIAGE,
}


class BudgetDecisionKind(StrEnum):
    """The two terminal verdicts a pre-execution check can produce.

    A closed enum so the consumer (the invocation surface) can switch
    exhaustively; expanding to a third verdict (e.g. ``DEFER`` for a
    future reservation protocol) is a deliberate code change.

    * :attr:`ALLOW` — the run may start. The accompanying
      :attr:`BudgetDecision.tier` is what the runtime should resolve
      (same as requested when no degradation fired; one rung cheaper
      when the threshold tripped).
    * :attr:`REFUSE` — the run must not start. The runtime raises
      :class:`~meho_backplane.agent.run.BudgetExceededError`; no
      durable row is created.
    """

    ALLOW = "allow"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class BudgetEnforcementSnapshot:
    """The window-by-window state one decision was made against.

    Threaded onto every :class:`BudgetDecision` so the invocation
    surface can log the exact (tokens / cost / requests) ratios that
    drove a refusal or a degradation — the C3-b "I refused because
    X" audit obligation. ``None`` ratios mean the corresponding limit
    was unset (NULL in the DB), i.e. the bucket has no cap on that
    dimension.

    The ratios are :class:`float` rather than :class:`Decimal` because
    the *limit* values these are read against carry far more precision
    than enforcement needs (two decimal places are plenty for a
    threshold compare), and turning :class:`Decimal` ratios into log
    fields invites JSON-encoder grief at the structlog edge. The
    underlying consumed / limit pairs stay :class:`Decimal` on the
    :class:`BudgetReading` returned by :func:`get_remaining`; only the
    derived ratios on this snapshot collapse to float.
    """

    window_kind: BudgetWindowKind
    tokens_ratio: float | None
    cost_ratio: float | None
    requests_ratio: float | None
    requests_remaining: int | None


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """The verdict one :func:`evaluate_pre_run_budget` call produced.

    ``kind`` is the terminal verdict; ``tier`` is the (possibly
    degraded) tier the runtime should resolve when ``kind == ALLOW``,
    and is the *original* requested tier when ``kind == REFUSE`` (kept
    on the value object so the audit row + the raised
    :class:`~meho_backplane.agent.run.BudgetExceededError` carry what
    the operator asked for, not a confusing ``None``).

    ``reason`` is a short human-readable string the invocation surface
    surfaces on the error / log (e.g. ``"daily cost_consumed >=
    cost_limit"``); ``snapshots`` records every window the gate
    inspected, so an operator reading the audit row sees daily +
    weekly + monthly state in one place.

    ``downgraded`` is ``True`` exactly when ``tier`` differs from the
    operator-requested tier the call was invoked with — explicit so a
    decision-reader does not have to compare against a separate
    "requested tier" reference.
    """

    kind: BudgetDecisionKind
    tier: AgentTier | None
    reason: str
    snapshots: tuple[BudgetEnforcementSnapshot, ...]
    downgraded: bool = False


@dataclass(frozen=True, slots=True)
class EnforcementContext:
    """The deterministic, settings-free decision input.

    Bundles the three knobs :func:`evaluate_pre_run_budget` reads —
    the degradation threshold, the global kill switch, and the
    per-tenant kill list — into one frozen value object. Built by
    :meth:`from_settings` for the live runtime; tests construct it
    directly so they can exercise the policy without going through the
    Settings env-var path.

    The ``threshold`` is a fraction in ``[0, 1)``; the consumer
    compares ``ratio >= threshold`` so a 0 threshold means "every run
    degrades" and a value approaching 1 means "only degrade when
    practically at the cap". A threshold of exactly 1 is rejected by
    :class:`Settings` (would defeat the policy — use the kill switch
    for "no agent runs" instead).
    """

    degrade_threshold: float
    global_kill_switch: bool
    disabled_tenants: frozenset[UUID]

    @classmethod
    def from_settings(cls, settings: Settings) -> EnforcementContext:
        """Build an :class:`EnforcementContext` from the live settings.

        The :attr:`Settings.agent_runs_disabled_tenants` raw string is
        parsed once here (rather than on every decision) so the
        runtime's per-run overhead is one settings read + one
        frozenset membership check.
        """
        return cls(
            degrade_threshold=settings.agent_budget_degrade_threshold,
            global_kill_switch=settings.agent_runs_disabled_global,
            disabled_tenants=parse_disabled_tenants(settings.agent_runs_disabled_tenants),
        )


def parse_disabled_tenants(raw: str) -> frozenset[UUID]:
    """Turn the comma-separated tenant list into a :class:`frozenset` of UUIDs.

    Tolerant of whitespace and case so an operator pasting from a
    docs example does not have to hand-normalise. A malformed UUID is
    a *configuration* error and surfaces as :class:`ValueError`
    (caller decides whether to fail-fast at startup or per-call); an
    empty / whitespace-only input is the documented "no tenants
    disabled" state and returns an empty frozenset.

    The function is pure / synchronous so :class:`Settings`'s
    validators can adopt it later without restructuring.
    """
    if not raw or not raw.strip():
        return frozenset()
    parts = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    return frozenset(UUID(part) for part in parts)


async def evaluate_pre_run_budget(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_sub: str,
    requested_tier: AgentTier | None,
    context: EnforcementContext,
) -> BudgetDecision:
    """Decide whether a run may start and, if so, against which tier.

    The single pre-execution gate the
    :class:`~meho_backplane.agent.invocation.AgentInvoker` calls before
    it creates the durable ``agent_run`` row. Phases:

    1. :func:`_kill_switch_decision` — global flag + per-tenant list.
    2. :func:`_read_all_windows` — daily / weekly / monthly buckets.
    3. :func:`_cap_breach_reason` → REFUSE on any cap fired.
    4. :func:`_threshold_breach_reason` → ALLOW with degraded tier.
    5. Otherwise → ALLOW unchanged.

    Why tokens + cost trigger degradation but requests don't: a
    request is a unit cost (one per run); halfway through the request
    budget means N more runs are available, each consuming one
    request regardless of tier. Tokens + cost, by contrast, do depend
    on the tier — a cheaper tier produces fewer / cheaper tokens — so
    those are the dimensions where degradation actually buys headroom.

    Args:
        session: Open :class:`AsyncSession`; not committed by this call.
        tenant_id: The tenant the principal belongs to.
        principal_sub: The JWT ``sub`` of the principal whose budget
            is being checked.
        requested_tier: The :class:`AgentTier` the definition asked
            for, or ``None`` when the definition has no tier (the
            legacy single-tenant path; no degradation possible, only
            REFUSE on hard caps).
        context: The :class:`EnforcementContext` carrying the knobs.

    Returns:
        A :class:`BudgetDecision`; the caller acts on
        :attr:`BudgetDecision.kind` and (on ALLOW) uses
        :attr:`BudgetDecision.tier` as the resolver input.
    """
    # Phase 1 — kill switches (fire before any DB read so an
    # operator's emergency-stop env push takes effect immediately on
    # the next invocation regardless of budget state).
    kill = _kill_switch_decision(tenant_id, principal_sub, requested_tier, context)
    if kill is not None:
        return kill

    # Phase 2 — the three windows. Compute the worst-case across the
    # three for each dimension so the gate is the conservative
    # all-of-them read.
    readings = await _read_all_windows(session, tenant_id, principal_sub)
    snapshots = tuple(_snapshot_for(kind, reading) for kind, reading in readings)

    # Phase 3 — cap-breach: REFUSE wins over degrade (cap is the
    # invariant, threshold is the heuristic).
    refuse_reason = _cap_breach_reason(readings)
    if refuse_reason is not None:
        _log.warning(
            "agent_run_refused_budget_exceeded",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
            reason=refuse_reason,
        )
        return BudgetDecision(
            kind=BudgetDecisionKind.REFUSE,
            tier=requested_tier,
            reason=refuse_reason,
            snapshots=snapshots,
        )

    # Phase 4 — threshold + one-rung downgrade.
    threshold_reason = _threshold_breach_reason(
        readings,
        threshold=context.degrade_threshold,
    )
    if threshold_reason is not None and requested_tier is not None:
        downgraded = _maybe_downgrade(
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            requested_tier=requested_tier,
            threshold_reason=threshold_reason,
            snapshots=snapshots,
        )
        if downgraded is not None:
            return downgraded

    return BudgetDecision(
        kind=BudgetDecisionKind.ALLOW,
        tier=requested_tier,
        reason="within budget",
        snapshots=snapshots,
    )


def _kill_switch_decision(
    tenant_id: UUID,
    principal_sub: str,
    requested_tier: AgentTier | None,
    context: EnforcementContext,
) -> BudgetDecision | None:
    """Run the kill-switch phases; return a REFUSE decision or ``None``.

    Returns ``None`` when no kill switch is engaged so the caller
    proceeds to the cap/threshold checks. Two switches checked here:

    * Global (``AGENT_RUNS_DISABLED_GLOBAL``) — cheapest possible
      response shape (no DB read, no per-tenant lookup).
    * Per-tenant (``AGENT_RUNS_DISABLED_TENANTS``) — still pre-DB.

    The per-identity kill switch (``request_limit = 0`` on the row)
    lives further down the pipeline in :func:`_cap_breach_reason`
    because reading it requires the DB session.
    """
    if context.global_kill_switch:
        _log.warning(
            "agent_run_refused_global_kill_switch",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
        )
        return BudgetDecision(
            kind=BudgetDecisionKind.REFUSE,
            tier=requested_tier,
            reason="global kill switch enabled (AGENT_RUNS_DISABLED_GLOBAL)",
            snapshots=(),
        )

    if tenant_id in context.disabled_tenants:
        _log.warning(
            "agent_run_refused_tenant_kill_switch",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
        )
        return BudgetDecision(
            kind=BudgetDecisionKind.REFUSE,
            tier=requested_tier,
            reason=(
                f"tenant {tenant_id} is in AGENT_RUNS_DISABLED_TENANTS; "
                f"agent runs are kill-switched for this tenant"
            ),
            snapshots=(),
        )

    return None


async def _read_all_windows(
    session: AsyncSession,
    tenant_id: UUID,
    principal_sub: str,
) -> list[tuple[BudgetWindowKind, BudgetReading]]:
    """Read every active budget window for (tenant, principal).

    Three reads in fixed order (daily / weekly / monthly) so the
    downstream cap/threshold checks see deterministic precedence —
    the same dimension on the daily window beats the weekly one in
    the reason string.
    """
    readings: list[tuple[BudgetWindowKind, BudgetReading]] = []
    for kind in (
        BudgetWindowKind.DAILY,
        BudgetWindowKind.WEEKLY,
        BudgetWindowKind.MONTHLY,
    ):
        reading = await get_remaining(
            session,
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            window_kind=kind,
        )
        readings.append((kind, reading))
    return readings


def _maybe_downgrade(
    *,
    tenant_id: UUID,
    principal_sub: str,
    requested_tier: AgentTier,
    threshold_reason: str,
    snapshots: tuple[BudgetEnforcementSnapshot, ...],
) -> BudgetDecision | None:
    """Try the one-rung tier downgrade; return ``None`` if no cheaper rung.

    Reading: walks :data:`TIER_DOWNGRADE_LADDER` once. A cheaper
    rung exists → return ALLOW with the substituted tier; no rung
    (TRIAGE) → log the "would-degrade-but-already-cheapest" line
    and return ``None`` so the caller emits the plain ALLOW.
    """
    cheaper = TIER_DOWNGRADE_LADDER.get(requested_tier)
    if cheaper is not None:
        _log.info(
            "agent_run_tier_downgraded",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
            requested_tier=requested_tier.value,
            resolved_tier=cheaper.value,
            reason=threshold_reason,
        )
        return BudgetDecision(
            kind=BudgetDecisionKind.ALLOW,
            tier=cheaper,
            reason=f"tier downgraded: {threshold_reason}",
            snapshots=snapshots,
            downgraded=True,
        )
    # TRIAGE (or any tier missing from the ladder): nothing
    # cheaper to fall to. Allow unchanged; the hard cap is the
    # only remaining gate (and it didn't fire here, since we
    # would have refused above).
    _log.info(
        "agent_run_threshold_no_cheaper_tier",
        tenant_id=str(tenant_id),
        principal_sub=principal_sub,
        requested_tier=requested_tier.value,
        reason=threshold_reason,
    )
    return None


def _snapshot_for(
    kind: BudgetWindowKind,
    reading: BudgetReading,
) -> BudgetEnforcementSnapshot:
    """Project a :class:`BudgetReading` onto a :class:`BudgetEnforcementSnapshot`.

    Converts the :class:`Decimal` consumed/limit pairs into ``float``
    ratios for logging + audit-row friendliness; ratios are ``None``
    when the corresponding limit is ``None`` (no cap on that
    dimension).
    """
    return BudgetEnforcementSnapshot(
        window_kind=kind,
        tokens_ratio=_ratio(reading.tokens_consumed, reading.token_limit),
        cost_ratio=_ratio(reading.cost_consumed, reading.cost_limit),
        requests_ratio=_ratio_int(reading.requests_consumed, reading.request_limit),
        requests_remaining=reading.requests_remaining,
    )


def _ratio(consumed: Decimal, limit: Decimal | None) -> float | None:
    """Return ``consumed / limit`` as a float, or ``None`` if unbounded.

    A zero limit is *not* division-by-zero territory: it means the
    operator set a hard "no consumption allowed" cap, so the ratio is
    infinite — encoded as ``float("inf")`` so the threshold compare
    fires (cap is breached).
    """
    if limit is None:
        return None
    if limit == 0:
        # consumed >= 0 ≥ limit (=0) always; surface as +inf so the
        # threshold compare and the cap-breach compare both fire.
        return float("inf") if consumed > 0 else 1.0
    return float(consumed) / float(limit)


def _ratio_int(consumed: int, limit: int | None) -> float | None:
    """Integer-flavour :func:`_ratio` (requests are :class:`int`, not Decimal)."""
    if limit is None:
        return None
    if limit == 0:
        return float("inf") if consumed > 0 else 1.0
    return consumed / limit


def _cap_breach_reason(
    readings: list[tuple[BudgetWindowKind, BudgetReading]],
) -> str | None:
    """Return a short human-readable reason if any window is at-or-over a cap.

    Walks the three readings and checks tokens / cost / requests in
    that order; the first hit wins (so the reason string names the
    dimension that fired). ``None`` means "no cap breached on any
    window" — the threshold check fires next.

    Per-identity kill switch (``request_limit = 0`` on the row) lands
    here too: a zero request_limit with consumed >= 0 trips on the
    requests dimension. The reason string distinguishes it from a
    "budget filled by use" refusal because the limit value is 0.
    """
    for kind, reading in readings:
        if reading.token_limit is not None and reading.tokens_consumed >= reading.token_limit:
            return (
                f"{kind.value} tokens_consumed ({reading.tokens_consumed}) "
                f">= token_limit ({reading.token_limit})"
            )
        if reading.cost_limit is not None and reading.cost_consumed >= reading.cost_limit:
            return (
                f"{kind.value} cost_consumed ({reading.cost_consumed}) "
                f">= cost_limit ({reading.cost_limit})"
            )
        if reading.request_limit is not None and reading.requests_consumed >= reading.request_limit:
            # Distinguish the per-identity kill switch (limit == 0)
            # from "budget filled by use" so an operator reading the
            # audit row knows which it is.
            if reading.request_limit == 0:
                return f"{kind.value} request_limit is 0 (per-identity kill switch)"
            return (
                f"{kind.value} requests_consumed ({reading.requests_consumed}) "
                f">= request_limit ({reading.request_limit})"
            )
    return None


def _threshold_breach_reason(
    readings: list[tuple[BudgetWindowKind, BudgetReading]],
    *,
    threshold: float,
) -> str | None:
    """Return a short reason if any window's tokens/cost ratio hits *threshold*.

    Requests are deliberately *not* checked here — see
    :func:`evaluate_pre_run_budget`'s docstring for why (cheaper tier
    doesn't help if the limit is on count, not per-call magnitude).
    """
    for kind, reading in readings:
        if reading.token_limit is not None and reading.token_limit > 0:
            ratio = float(reading.tokens_consumed) / float(reading.token_limit)
            if ratio >= threshold:
                return f"{kind.value} tokens at {ratio:.2%} of limit (threshold {threshold:.0%})"
        if reading.cost_limit is not None and reading.cost_limit > 0:
            ratio = float(reading.cost_consumed) / float(reading.cost_limit)
            if ratio >= threshold:
                return f"{kind.value} cost at {ratio:.2%} of limit (threshold {threshold:.0%})"
    return None
