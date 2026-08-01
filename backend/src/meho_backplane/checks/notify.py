# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-Dashboard email notification on a claimed rollup transition (#2719).

Task #2719 under Initiative #2716 (parent goal #221). #2507's transition
detector (:mod:`meho_backplane.checks.investigate`) already compare-and-swaps
``check_dashboards.last_rollup_state`` exactly once per rollup edge, replica-
safe; until now that claim only ever reached the diagnose-only investigator.
This module is the second, independent consumer of the same claim: it mails
the Dashboard's configured recipient.

Both edge directions notify
===========================

The investigator fires on a **worsening** edge only -- a recovery needs no
diagnosis. Notification is the opposite: an operator who was paged for
``critical`` needs the all-clear as much as the page. So the rule here is
symmetric in the two states, keyed on the configured floor:

    notify  <=>  max(rank(previous), rank(current)) >= rank(notify_min_state)

under ``ok < degraded < critical``. With the default ``notify_min_state``
of ``critical`` that means ``critical -> ok`` mails the all-clear (the
``critical`` side clears the bar) while ``ok -> degraded`` stays silent
(neither side does). Prometheus Alertmanager takes the same stance with
``send_resolved``: the resolved notification is standard practice, not an
extra.

:data:`_NOTIFY_RANK` ranks ``unknown`` with ``degraded`` -- a Dashboard that
cannot be evaluated is a degradation, matching the Initiative's
``UNKNOWN -> degraded`` rollup cap -- and ranks ``skip`` with ``ok``, so a
``skip`` edge never clears the bar **on its own**; ``critical -> skip``
still mails, because the ``critical`` side does.

Exactly-once, and the flap boundary
===================================

One mail per *claimed* edge. The claim is the compare-and-swap, so two
replicas racing the same edge produce exactly one send with no coordination
here. What that does **not** bound is a genuinely flapping Dashboard: a
member Sensor going stale and back re-crosses ``critical <-> unknown``, and
each crossing is a real edge, so each mails. There is deliberately no
rate limit, digest, or repeat-suppression window in this Task (#2716
scopes those out); the floor knob is the only volume control today.

Failure posture
===============

Fire-and-forget, in two senses. The send runs as a tracked background task
(:func:`schedule_dashboard_notification`) so a 30-second SMTP session
cannot sit on the check-runner's result-persist path -- the same containment
shape the investigator uses. And :func:`notify_dashboard_transition` never
raises: a refused, unconfigured, or failed
:func:`~meho_backplane.connectors.mail.transport.send_email` is logged as
``checks_notify_failed`` and swallowed, contract parity with
``investigate_on_transition``'s never-raise posture. A broken MTA must not
convert a committed transition claim into a persist-path failure.

Header safety
=============

A Dashboard name is operator-authored free text with no newline constraint
at the database, and the transport's in-process entry point raises
:class:`ValueError` on a subject carrying a line break (only the dispatched
``mail.send`` path gets the single-line parameter screen). The subject
built here therefore folds every control character out of the name, so a
crafted name cannot inject an SMTP header. The body is a MIME payload, not
headers, so its untrusted fragments (member names, last values, evidence)
need no such fold -- only bounding, which :data:`_MAX_MEMBER_LINES` and
:data:`_MAX_FIELD_CHARS` provide.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Final, cast

import structlog

from meho_backplane.connectors.mail.transport import send_email

__all__ = [
    "DashboardNotice",
    "NotifyMember",
    "notify_dashboard_transition",
    "schedule_dashboard_notification",
]


#: Notification rank over the five-state check vocabulary. ``unknown`` ranks
#: with ``degraded`` (cannot-evaluate is a degradation, the Initiative's
#: rollup cap); ``skip`` ranks with ``ok`` so a deliberate opt-out never
#: clears the bar by itself. Distinct from
#: :data:`~meho_backplane.checks.investigate._FIRE_RANK`, which additionally
#: requires the *current* state to be a firing target -- that asymmetry is
#: exactly what keeps the investigator worsening-only while this notifier
#: acts on both directions.
_NOTIFY_RANK: Final[dict[str, int]] = {
    "ok": 0,
    "skip": 0,
    "unknown": 1,
    "degraded": 1,
    "critical": 2,
}

#: How many non-green members one mail enumerates. A correlated failure can
#: redden a whole Dashboard; the recipient needs the shape of the problem,
#: not a 200-row dump (``dashboard_schemas._MAX_MEMBERS``).
_MAX_MEMBER_LINES: Final[int] = 20

#: Per-field character bound on the untrusted member fragments (last value,
#: evidence). Bounds one adversarial or merely verbose Sensor payload from
#: dominating the mail.
_MAX_FIELD_CHARS: Final[int] = 200

#: Per-process strong references to in-flight notification tasks. ``asyncio``
#: holds only weak references to bare tasks, so without this set a
#: fire-and-forget send could be garbage-collected mid-flight (the
#: ``_INVESTIGATIONS`` rationale in
#: :mod:`meho_backplane.checks.investigate`). The done-callback discards each
#: entry on completion so a long-lived worker does not accumulate them.
_NOTIFICATIONS: set[asyncio.Task[None]] = set()


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    A module-level proxy caches its bound methods on first use under the
    production ``cache_logger_on_first_use=True`` config, which orphans them
    from a later :func:`structlog.testing.capture_logs`. Resolving per call
    reads the live config each time -- the discipline the check runner and
    the investigator both follow.
    """
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


@dataclass(frozen=True, slots=True)
class NotifyMember:
    """One non-green member Sensor as the mail renders it.

    A narrow, notifier-owned projection rather than a reuse of the
    investigator's briefing snapshot: this module must not depend on
    :mod:`meho_backplane.checks.investigate` (which imports *it*), and the
    mail needs four fields where the briefing needs ten.
    """

    name: str
    effective_state: str
    last_value: object
    last_evidence: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class DashboardNotice:
    """One claimed transition, detached for the background send.

    Built while the ``check_dashboards`` row and its member Sensors are live
    inside the claim transaction, so the background task never touches an
    expired ORM object.
    """

    dashboard_id: uuid.UUID
    name: str
    previous_state: str
    current_state: str
    notify_email: str | None
    notify_min_state: str
    members: tuple[NotifyMember, ...]


def _crosses_threshold(previous: str, current: str, min_state: str) -> bool:
    """Return ``True`` iff this edge reaches the configured notification floor.

    ``max(rank(previous), rank(current)) >= rank(min_state)`` -- symmetric in
    the two states, which is what makes the recovery edge notify. An
    out-of-vocabulary state ranks 0 rather than raising: the memo column is
    CHECK-constrained, so this is unreachable defensive shaping, and a
    notifier is the wrong place to fail loudly.
    """
    floor = _NOTIFY_RANK.get(min_state, _NOTIFY_RANK["critical"])
    return max(_NOTIFY_RANK.get(previous, 0), _NOTIFY_RANK.get(current, 0)) >= floor


def _single_line(value: str) -> str:
    """Fold every control character out of *value* (subject-header safety)."""
    return "".join(" " if ch.isspace() or not ch.isprintable() else ch for ch in value).strip()


def _clip(value: object) -> str:
    """Render *value* as a bounded single-line fragment for the mail body."""
    text = " ".join(str(value).split())
    if len(text) > _MAX_FIELD_CHARS:
        return text[:_MAX_FIELD_CHARS] + "..."
    return text


def _subject(notice: DashboardNotice) -> str:
    """Build the single-line subject.

    An "all clear" prefix keys on *no member being non-green* rather than on
    the edge improving: ``critical -> degraded`` improves but still has
    failures to name, and the recipient's filter rule cares about whether
    anything is still broken.
    """
    prefix = "" if notice.members else "all clear - "
    name = _single_line(notice.name)
    return f"[MEHO] {prefix}{name}: {notice.previous_state} -> {notice.current_state}"


def _body(notice: DashboardNotice) -> str:
    """Build the plain-text body: the edge, then the non-green members."""
    lines = [
        f"Dashboard: {notice.name}",
        f"State: {notice.previous_state} -> {notice.current_state}",
        "",
    ]
    if not notice.members:
        lines.append("All clear - no member sensor is in a failing state.")
        return "\n".join(lines) + "\n"

    shown = notice.members[:_MAX_MEMBER_LINES]
    lines.append(f"Non-green members ({len(notice.members)}):")
    lines.append("")
    for member in shown:
        lines.append(f"- {_clip(member.name)} [{member.effective_state}]")
        lines.append(f"    last value: {_clip(member.last_value)}")
        lines.append(f"    evidence:   {_clip(member.last_evidence)}")
    if len(notice.members) > len(shown):
        lines.append("")
        lines.append(f"({len(notice.members) - len(shown)} further member(s) not shown.)")
    return "\n".join(lines) + "\n"


async def _deliver(notice: DashboardNotice, recipient: str) -> None:
    """Send one mail and record its outcome; swallow every failure.

    Split out of :func:`notify_dashboard_transition` so that function reads
    as the two gates it is, and so the never-raise guard wraps exactly the
    I/O rather than the gates too.
    """
    try:
        result = await send_email(
            to=[recipient],
            subject=_subject(notice),
            body=_body(notice),
        )
    except Exception:
        # Contract: a notification failure never reaches the persist seam.
        _log().warning(
            "checks_notify_failed",
            dashboard_id=str(notice.dashboard_id),
            reason="unexpected_error",
            exc_info=True,
        )
        return

    if not result.sent:
        _log().warning(
            "checks_notify_failed",
            dashboard_id=str(notice.dashboard_id),
            reason=result.reason,
        )
        return
    _log().info(
        "checks_notify_sent",
        dashboard_id=str(notice.dashboard_id),
        previous_state=notice.previous_state,
        current_state=notice.current_state,
        member_count=len(notice.members),
    )


async def notify_dashboard_transition(notice: DashboardNotice) -> None:
    """Mail the Dashboard's recipient about one claimed transition.

    Never raises. Short-circuits, in order:

    * ``notify_email`` unset -- notifications are off for this Dashboard
      (the state every pre-#2719 row backfills to);
    * the edge does not reach ``notify_min_state`` (see
      :func:`_crosses_threshold`).

    Otherwise :func:`_deliver` runs one
    :func:`~meho_backplane.connectors.mail.transport.send_email` call,
    screened by the deployment-level recipient allowlist floor that function
    owns.
    """
    if not notice.notify_email:
        _log().info(
            "checks_notify_skipped_unconfigured",
            dashboard_id=str(notice.dashboard_id),
        )
        return
    if not _crosses_threshold(notice.previous_state, notice.current_state, notice.notify_min_state):
        _log().info(
            "checks_notify_skipped_below_threshold",
            dashboard_id=str(notice.dashboard_id),
            previous_state=notice.previous_state,
            current_state=notice.current_state,
            notify_min_state=notice.notify_min_state,
        )
        return
    await _deliver(notice, notice.notify_email)


def schedule_dashboard_notification(notice: DashboardNotice) -> None:
    """Spawn the send as a tracked fire-and-forget task.

    Keeps the SMTP session off the check-runner's result-persist path: the
    transport bounds one session at 30 seconds, and the persist path awaits
    ``investigate_on_transition`` inline. ``notify_dashboard_transition``
    swallows every exception, so the task cannot end in an unretrieved
    exception; :class:`asyncio.CancelledError` still propagates so lifespan
    shutdown tears the task down cleanly.
    """
    task = asyncio.create_task(
        notify_dashboard_transition(notice),
        name=f"checks-notify-{notice.dashboard_id}",
    )
    _NOTIFICATIONS.add(task)
    task.add_done_callback(_NOTIFICATIONS.discard)


async def _await_pending_notifications() -> None:
    """Await all in-flight notification tasks -- a deterministic test seam.

    Production spawns sends fire-and-forget; tests drain them before
    asserting on the transport. Mirrors
    :func:`meho_backplane.checks.investigate._await_pending_investigations`.
    """
    pending = [task for task in _NOTIFICATIONS if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
