# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Five-state worst-of Dashboard rollup -- a pure, DB-free fold (#2506).

Task #2506 under Initiative #2416 (parent goal #221). A Dashboard composes
Sensors (#2503) and answers one question -- "is everything OK?" -- by
folding each member Sensor's latest-state projection into a single
:data:`~meho_backplane.checks.assertions.CheckState`. The fold is
**evaluated on read** (decision recorded on #2506): the only readers are
the ``/ui/checks`` console and the REST GET, both human-frequency; the
hysteresis rules depend on ``now``, so a materialised state would go stale
between ticks and would need its own writer loop -- a second recurring
writer the Initiative's substrate-minimalism avoids.

This module is dependency-pure -- stdlib + :data:`CheckState` only, no
SQLAlchemy / session imports -- so the fold unit-tests as a table of
``(last_state, status, last_evaluated_at, next_fire_at, state_since,
severity, for_seconds, now) -> dashboard state`` with no DB fixture. The
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`
builds :class:`MemberState` values from the join rows and calls
:func:`evaluate_member` / :func:`fold` here.

Fold rules (all binding, decided on #2506)
==========================================

**Raw member-state derivation (before the fold):**

* ``status='paused'`` -> raw ``skip`` (the v1 producer of the first-class
  SKIP -- "unreachable-by-design", deliberately-not-evaluated).
* never evaluated (``last_evaluated_at IS NULL``) or overdue
  (``now > next_fire_at + _STALE_GRACE_SECONDS``) -> raw ``unknown`` (the
  "stale / read-failed" arm; a crashed #2505 runner leaves a dead
  ``next_fire_at`` visible here).
* otherwise raw = the projection's ``last_state``.

**Per-member contribution:**

* ``skip`` -> excluded from the fold (first-class in display, never
  degrades).
* ``unknown`` -> contributes as ``degraded`` (the Initiative's
  ``UNKNOWN -> degraded``).
* ``ok`` / ``degraded`` / ``critical`` -> contribute as themselves.

**Severity cap:** each contribution is clamped to the Sensor's
``severity`` (``min(contribution, severity)`` under ``ok < degraded <
critical``) -- a ``severity=degraded`` Sensor can never drive a dashboard
to ``critical``.

**Hysteresis (`for:` hold-time):** a non-``ok``, non-``skip`` raw state
contributes only once it has held continuously for ``>= for_seconds``
(``now - state_since >= for_seconds``); until then the member contributes
``ok`` and is displayed *pending*. Recovery to ``ok`` takes effect
immediately (Prometheus ``for:`` semantics -- the hold delays firing,
never resolution). A member with no ``state_since`` (never transitioned)
is not held.

**Fold:** worst-of over contributions (``ok < degraded < critical``). Zero
members -> ``unknown``; ``>= 1`` member but all ``skip`` -> ``skip``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from meho_backplane.checks.assertions import CheckState

__all__ = [
    "MemberEvaluation",
    "MemberState",
    "evaluate_member",
    "fold",
    "rollup",
]

#: Grace window added to a Sensor's ``next_fire_at`` before it counts as
#: overdue. A Sensor evaluated a little late (runner jitter, a slow op) is
#: not yet stale; only once it is more than this many seconds past its due
#: instant does the rollup derive ``unknown`` for it.
_STALE_GRACE_SECONDS = 60

#: Ordering over the three *foldable* states. ``skip`` / ``unknown`` are
#: never fold operands (``skip`` is excluded; ``unknown`` maps to
#: ``degraded`` before folding), so they carry no rank here. Used for both
#: the worst-of fold and the per-Sensor severity cap.
_FOLD_ORDER: dict[str, int] = {"ok": 0, "degraded": 1, "critical": 2}


@dataclass(frozen=True)
class MemberState:
    """The rollup-relevant projection of one member Sensor.

    Built by the service from a ``sensor`` join row; carries only what the
    fold reads (no display fields, no id) so this module stays DB-free.
    """

    #: The projection's persisted ``last_state`` (#2503). Only consulted
    #: when the derivation below does not override it (paused / stale).
    last_state: CheckState
    #: Sensor lifecycle status (``active`` / ``paused``). ``paused`` derives
    #: ``skip``.
    status: str
    #: Per-Sensor severity cap (``degraded`` / ``critical``).
    severity: str
    #: ``for:`` hold-time in seconds (hysteresis; ``>= 0``).
    for_seconds: int
    #: When the Sensor was last evaluated; ``None`` -> never evaluated ->
    #: derives ``unknown``.
    last_evaluated_at: datetime | None
    #: Materialised next-fire instant; drives the overdue -> ``unknown``
    #: derivation.
    next_fire_at: datetime | None
    #: When the current ``last_state`` began (bumped transition-only by
    #: ``record_sensor_result``); the hysteresis clock.
    state_since: datetime | None


@dataclass(frozen=True)
class MemberEvaluation:
    """The per-member verdict the fold consumes and the detail view renders.

    :attr:`raw_state` is the derived current state (after the paused ->
    ``skip`` / stale -> ``unknown`` derivation). :attr:`effective_state` is
    what the member *contributes* to the fold: ``skip`` (excluded), ``ok``
    (healthy or held-pending), or the severity-capped
    ``degraded`` / ``critical``. :attr:`pending` is ``True`` when a failing
    raw state is being held by the ``for:`` window (so it contributes
    ``ok`` for now).
    """

    raw_state: CheckState
    effective_state: CheckState
    pending: bool


def _as_utc(value: datetime) -> datetime:
    """Normalise a possibly-naive datetime to UTC-aware for arithmetic.

    ``DateTime(timezone=True)`` round-trips naive on aiosqlite (the
    unit-test path) and aware on PG; the runner always evaluates in UTC, so
    a naive stored value denotes a UTC instant. Attaching UTC lets the
    ``now`` arithmetic work regardless of which side is naive.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _cap(state: str, severity: str) -> CheckState:
    """Clamp *state* to *severity* under ``ok < degraded < critical``.

    A ``severity=degraded`` Sensor caps a ``critical`` contribution to
    ``degraded``; a ``severity=critical`` Sensor caps nothing below
    ``critical``. Both operands are foldable states.
    """
    if _FOLD_ORDER[state] <= _FOLD_ORDER[severity]:
        return state  # type: ignore[return-value]
    return severity  # type: ignore[return-value]


def _derive_raw(state: MemberState, now: datetime) -> CheckState:
    """Derive the member's current raw state (see the module rules)."""
    if state.status == "paused":
        return "skip"
    if state.last_evaluated_at is None:
        return "unknown"
    if state.next_fire_at is not None:
        overdue_after = _as_utc(state.next_fire_at) + timedelta(seconds=_STALE_GRACE_SECONDS)
        if _as_utc(now) > overdue_after:
            return "unknown"
    return state.last_state


def evaluate_member(state: MemberState, now: datetime) -> MemberEvaluation:
    """Evaluate one member into its ``(raw, effective, pending)`` verdict."""
    raw = _derive_raw(state, now)
    if raw == "skip":
        return MemberEvaluation(raw_state="skip", effective_state="skip", pending=False)
    if raw == "ok":
        # Recovery to ok takes effect immediately -- no hold.
        return MemberEvaluation(raw_state="ok", effective_state="ok", pending=False)

    # raw is one of degraded / critical / unknown -> a failing contribution.
    contribution: CheckState = "degraded" if raw == "unknown" else raw
    capped = _cap(contribution, state.severity)

    # Hysteresis: a failing state that has not yet held for ``for_seconds``
    # contributes ok and is displayed pending. A member with no
    # ``state_since`` (never transitioned) has no clock to hold against, so
    # it is not suppressed.
    if state.state_since is not None:
        held_for = _as_utc(now) - _as_utc(state.state_since)
        if held_for < timedelta(seconds=state.for_seconds):
            return MemberEvaluation(raw_state=raw, effective_state="ok", pending=True)
    return MemberEvaluation(raw_state=raw, effective_state=capped, pending=False)


def fold(evaluations: Sequence[MemberEvaluation]) -> CheckState:
    """Worst-of fold over already-evaluated members.

    Zero members -> ``unknown``. ``>= 1`` member but every one ``skip`` ->
    ``skip``. Otherwise the worst of the non-``skip`` contributions under
    ``ok < degraded < critical``.
    """
    if not evaluations:
        return "unknown"
    contributions = [e for e in evaluations if e.effective_state != "skip"]
    if not contributions:
        return "skip"
    return max(contributions, key=lambda e: _FOLD_ORDER[e.effective_state]).effective_state


def rollup(members: Sequence[MemberState], now: datetime) -> CheckState:
    """Fold member Sensors into one Dashboard state (the read-path entry)."""
    return fold([evaluate_member(m, now) for m in members])
