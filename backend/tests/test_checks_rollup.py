# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the five-state Dashboard rollup (#2506).

Initiative #2416 (parent goal #221), Task #2506. The rollup
(:mod:`meho_backplane.checks.rollup`) is a pure, DB-free fold, so it tests as
a table of ``(last_state, status, last_evaluated_at, next_fire_at,
state_since, severity, for_seconds, now) -> dashboard state`` with no fixture.

Coverage (by name, per the acceptance criteria): worst-of ordering,
``unknown -> degraded``, ``skip`` excluded, paused -> ``skip``, never-evaluated
-> ``unknown``, overdue -> ``unknown``, severity cap, hysteresis pending +
expiry, immediate recovery, zero-member -> ``unknown``, all-``skip`` ->
``skip``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meho_backplane.checks.rollup import (
    MemberState,
    evaluate_member,
    fold,
    rollup,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _member(
    *,
    last_state: str = "ok",
    status: str = "active",
    severity: str = "critical",
    for_seconds: int = 0,
    last_evaluated_at: datetime | None = _NOW - timedelta(seconds=30),
    next_fire_at: datetime | None = _NOW + timedelta(seconds=60),
    state_since: datetime | None = _NOW - timedelta(hours=1),
) -> MemberState:
    """Build a member with healthy defaults; override one axis per test."""
    return MemberState(
        last_state=last_state,  # type: ignore[arg-type]
        status=status,
        severity=severity,
        for_seconds=for_seconds,
        last_evaluated_at=last_evaluated_at,
        next_fire_at=next_fire_at,
        state_since=state_since,
    )


# ---------------------------------------------------------------------------
# Worst-of ordering
# ---------------------------------------------------------------------------


def test_worst_of_ordering() -> None:
    """The dashboard state is the worst of its members' contributions."""
    assert rollup([_member(last_state="ok"), _member(last_state="ok")], _NOW) == "ok"
    assert rollup([_member(last_state="ok"), _member(last_state="degraded")], _NOW) == "degraded"
    assert (
        rollup([_member(last_state="degraded"), _member(last_state="critical")], _NOW) == "critical"
    )
    # A single critical member drives the whole dashboard critical.
    assert rollup([_member(last_state="ok"), _member(last_state="critical")], _NOW) == "critical"


# ---------------------------------------------------------------------------
# unknown -> degraded
# ---------------------------------------------------------------------------


def test_unknown_contributes_degraded() -> None:
    """A member whose raw state is unknown contributes degraded to the fold."""
    # Never-evaluated member (raw unknown) alongside an ok member -> degraded.
    unknown = _member(last_evaluated_at=None, state_since=None)
    assert evaluate_member(unknown, _NOW).raw_state == "unknown"
    assert evaluate_member(unknown, _NOW).effective_state == "degraded"
    assert rollup([_member(last_state="ok"), unknown], _NOW) == "degraded"


# ---------------------------------------------------------------------------
# skip excluded from the fold
# ---------------------------------------------------------------------------


def test_skip_excluded_from_fold() -> None:
    """A skip member never degrades the dashboard (excluded from the fold)."""
    paused = _member(status="paused")
    # skip + ok -> ok (skip does not count).
    assert rollup([paused, _member(last_state="ok")], _NOW) == "ok"
    # skip + critical -> critical (skip neither raises nor lowers).
    assert rollup([paused, _member(last_state="critical")], _NOW) == "critical"


# ---------------------------------------------------------------------------
# Raw-state derivation
# ---------------------------------------------------------------------------


def test_paused_member_derives_skip() -> None:
    """A paused sensor derives raw skip (unreachable-by-design)."""
    ev = evaluate_member(_member(status="paused", last_state="critical"), _NOW)
    assert ev.raw_state == "skip"
    assert ev.effective_state == "skip"


def test_never_evaluated_member_derives_unknown() -> None:
    """A sensor never evaluated (last_evaluated_at is None) derives unknown."""
    ev = evaluate_member(_member(last_evaluated_at=None, state_since=None), _NOW)
    assert ev.raw_state == "unknown"
    assert ev.effective_state == "degraded"


def test_overdue_member_derives_unknown() -> None:
    """A sensor overdue by more than the grace window derives unknown."""
    # next_fire_at is 120 s in the past -> beyond the 60 s grace -> unknown.
    overdue = _member(
        last_state="ok",
        last_evaluated_at=_NOW - timedelta(minutes=10),
        next_fire_at=_NOW - timedelta(seconds=120),
    )
    assert evaluate_member(overdue, _NOW).raw_state == "unknown"
    # Just inside the grace window (30 s overdue) is NOT stale -> keeps ok.
    fresh = _member(
        last_state="ok",
        last_evaluated_at=_NOW - timedelta(minutes=1),
        next_fire_at=_NOW - timedelta(seconds=30),
    )
    assert evaluate_member(fresh, _NOW).raw_state == "ok"


# ---------------------------------------------------------------------------
# Severity cap
# ---------------------------------------------------------------------------


def test_severity_cap_clamps_critical_to_degraded() -> None:
    """A degraded-severity sensor caps a critical contribution to degraded."""
    capped = _member(last_state="critical", severity="degraded")
    assert evaluate_member(capped, _NOW).effective_state == "degraded"
    assert rollup([capped], _NOW) == "degraded"
    # A critical-severity sensor is not capped below critical.
    uncapped = _member(last_state="critical", severity="critical")
    assert evaluate_member(uncapped, _NOW).effective_state == "critical"


# ---------------------------------------------------------------------------
# Hysteresis (for: hold-time)
# ---------------------------------------------------------------------------


def test_hysteresis_pending_contributes_ok() -> None:
    """A failing state held for less than for_seconds contributes ok (pending)."""
    pending = _member(
        last_state="critical",
        for_seconds=300,
        state_since=_NOW - timedelta(seconds=100),  # < 300 -> not yet firing
    )
    ev = evaluate_member(pending, _NOW)
    assert ev.pending is True
    assert ev.effective_state == "ok"
    assert rollup([pending], _NOW) == "ok"


def test_hysteresis_expiry_contributes_failing() -> None:
    """A failing state held for at least for_seconds contributes its state."""
    fired = _member(
        last_state="critical",
        for_seconds=300,
        state_since=_NOW - timedelta(seconds=400),  # >= 300 -> fires
    )
    ev = evaluate_member(fired, _NOW)
    assert ev.pending is False
    assert ev.effective_state == "critical"
    assert rollup([fired], _NOW) == "critical"


def test_immediate_recovery_takes_effect_at_once() -> None:
    """Recovery to ok is immediate -- the for: hold never delays resolution."""
    recovered = _member(
        last_state="ok",
        for_seconds=300,
        state_since=_NOW - timedelta(seconds=1),  # just recovered
    )
    ev = evaluate_member(recovered, _NOW)
    assert ev.pending is False
    assert ev.effective_state == "ok"
    assert rollup([recovered], _NOW) == "ok"


# ---------------------------------------------------------------------------
# Edge folds
# ---------------------------------------------------------------------------


def test_zero_members_rolls_up_unknown() -> None:
    """A dashboard with no members rolls up to unknown."""
    assert rollup([], _NOW) == "unknown"
    assert fold([]) == "unknown"


def test_all_skip_rolls_up_skip() -> None:
    """A dashboard whose every member is skip rolls up to skip."""
    assert rollup([_member(status="paused"), _member(status="paused")], _NOW) == "skip"
