# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator email for the checks layer: transitions (#2719) and findings (#2721).

Task #2719 under Initiative #2716 (parent goal #221). #2507's transition
detector (:mod:`meho_backplane.checks.investigate`) already compare-and-swaps
``check_dashboards.last_rollup_state`` exactly once per rollup edge, replica-
safe; until now that claim only ever reached the diagnose-only investigator.
This module is the second, independent consumer of the same claim: it mails
the Dashboard's configured recipient(s). Since #2764 ``notify_email`` may
carry more than one comma-separated address; the identical mail fans out to
every recipient in one send, each screened individually against the
deployment allowlist (:func:`_allowed_recipients`) so one refused entry
drops only itself.

Two notice kinds, one delivery seam
===================================

#2721 added the second message this module renders: the investigator's
:class:`~meho_backplane.checks.investigate.ChecksFinding` once the run is
terminal and its noise-suppression policy is persisted
(:class:`FindingNotice` / :func:`schedule_finding_notification`). Both kinds
share the same background-task set, the same never-raise posture, and the
same header-safety fold, so the delivery contract is stated once.

The **wrapper** sends the finding mail; the investigator agent has no mail
tool and is never given one. That is what keeps the diagnose-only contract
(``investigate.py`` imports no operations dispatcher) intact while still
putting the finding in front of a human. An agent that should send ad-hoc
mail does so in its own run via ``call_operation`` on the ``mail.*``
connector under normal policy -- a different seam entirely.

Both notice kinds project their inputs into module-local dataclasses
(:class:`NotifyMember`, :class:`FindingNotice`) rather than importing the
investigator's types. The import must stay one-directional --
``investigate`` imports *this* module -- so a shared type would be a cycle.

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

A new problem on a Dashboard that is already red (#5068)
=======================================================

The edge rule above only sees the Dashboard's overall state. A Dashboard
that is already ``critical`` because of one Sensor therefore hid every
later problem on it: a second Sensor turning ``critical`` did not move the
overall state, so no mail went out. Since #5068 the claim also
compare-and-swaps each member's counted state (the link row's
``last_member_state``) and hands a :class:`DashboardNotice` carrying a
:class:`MemberChange` to this module when one member got worse while the
overall state stayed the same. The notice is delivered through the same
gates as a Dashboard edge, with the member's own edge standing in for the
Dashboard's: the ``notify_min_state`` floor applies to the member's new
state, and the flap window is keyed per ``(tenant, dashboard, sensor,
state)`` so a flapping member is bounded like a flapping Dashboard. The
subject names the member and says the Dashboard is still red; the body
lists every non-green member and marks the new one. A member recovering
while the Dashboard stays red is not mailed (the #5068 boundary), and a
member change never reaches the investigator.

Exactly-once, and the flap window
=================================

One mail per *claimed* edge. The claim is the compare-and-swap, so two
replicas racing the same edge produce exactly one send with no coordination
here. What that alone does not bound is a genuinely flapping Dashboard: a
member Sensor going stale and back re-crosses ``critical <-> unknown``, and
each crossing is a real edge. Since #2732 a per-``(tenant, dashboard,
state)`` suppression window bounds *delivery*: the first crossing into a
non-green state claims a Valkey ``SET NX EX`` key
(:func:`_claim_suppression`, the #2718 advisory's idiom) and mails; repeat
crossings into the **same** state inside the window lose the claim and are
suppressed (``checks_notify_suppressed``). The window is
``CHECKS_NOTIFY_SUPPRESSION_WINDOW_MINUTES`` (default 30); ``0`` disables
suppression entirely and short-circuits before any Valkey call, restoring
the pre-#2732 one-mail-per-edge behaviour.

Three deliberate asymmetries keep the window from ever costing a page:

* **Escalation is never suppressed.** A different state is a different
  key, so ``degraded -> critical`` mails immediately after a recent
  ``degraded`` notice.
* **Recovery is never suppressed, and it resets the windows.** A crossing
  into a rank-0 state (``ok`` / ``skip``) is exempt from the claim, and
  it deletes the Dashboard's suppression keys (:func:`_clear_suppression`)
  -- even when the recovery edge itself sits below the floor -- so the
  incident *after* an all-clear is a new incident and its first crossing
  mails, never inherits a half-spent window.
* **Fail-open.** A Valkey error on claim or clear warn-logs
  (``checks_notify_suppression_failed``) and the notification is sent --
  a missed alert is worse than a duplicate.

The window bounds **attempts**, not confirmed deliveries: the key is
claimed before the send (atomic check-and-claim; claiming after would race
a slow SMTP session against the next flap edge), so a send that fails
inside a window is not retried on the next same-state edge. That matches
the #2719 contract -- at most one notification attempt per claimed
transition -- and the failure is warn-logged either way. Digests and
batching remain out of scope (#2716); findings are not flap-suppressed --
their volume control is the investigator's own fire gate.

Failure posture
===============

Fire-and-forget, in two senses. The send runs as a tracked background task
(:func:`schedule_dashboard_notification` /
:func:`schedule_finding_notification`) so a 30-second SMTP session cannot
sit on the caller's path -- the check-runner's result-persist path for a
transition, and the investigator's serial cause-group loop for a finding,
where an inline send would delay the *next* group's diagnosis by a whole
SMTP session. And neither :func:`notify_dashboard_transition` nor
:func:`notify_finding` raises: a refused, unconfigured, or failed
:func:`~meho_backplane.connectors.mail.transport.send_email` is logged
(``checks_notify_failed`` / ``checks_finding_email_failed``) and swallowed,
contract parity with ``investigate_on_transition``'s never-raise posture. A
broken MTA must not convert a committed transition claim into a persist-path
failure, nor a persisted finding into a lost noise-suppression policy.

Header safety
=============

A Dashboard name is operator-authored free text with no newline constraint
at the database, and the transport's in-process entry point raises
:class:`ValueError` on a subject carrying a line break (only the dispatched
``mail.send`` path gets the single-line parameter screen). Every subject
built here therefore folds every control character out of the interpolated
fragments, so neither a crafted Dashboard name nor a model-authored verdict
can inject an SMTP header. Bodies are MIME payloads, not headers, so their
untrusted fragments (member names, last values, evidence, and the finding's
model-written summary) need no such fold -- only bounding, which
:data:`_MAX_MEMBER_LINES`, :data:`_MAX_FIELD_CHARS`,
:data:`_MAX_EVIDENCE_LINES` and :data:`_MAX_FINDING_CHARS` provide.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Final, cast

import structlog

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.connectors.mail.allowlist import (
    RecipientNotAllowedError,
    assert_recipient_allowed,
)
from meho_backplane.connectors.mail.transport import send_email
from meho_backplane.settings import get_settings

__all__ = [
    "DashboardNotice",
    "FindingNotice",
    "MemberChange",
    "NotifyMember",
    "notify_dashboard_transition",
    "notify_finding",
    "schedule_dashboard_notification",
    "schedule_finding_notification",
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

#: The states a flap-suppression window applies to -- exactly the rank>=1
#: states of :data:`_NOTIFY_RANK`. A crossing into a rank-0 state (``ok`` /
#: ``skip``) is a recovery for suppression purposes: it is never claimed
#: (the all-clear always delivers) and it clears these keys so the next
#: incident's first crossing mails. Enumerated as a tuple (not derived from
#: the dict at call time) because :func:`_clear_suppression` deletes one key
#: per entry and the vocabulary is CHECK-constrained at the database.
_SUPPRESSIBLE_STATES: Final[tuple[str, ...]] = ("unknown", "degraded", "critical")

#: How many non-green members one mail enumerates. A correlated failure can
#: redden a whole Dashboard; the recipient needs the shape of the problem,
#: not a 200-row dump (``dashboard_schemas._MAX_MEMBERS``).
_MAX_MEMBER_LINES: Final[int] = 20

#: Per-field character bound on the untrusted member fragments (last value,
#: evidence). Bounds one adversarial or merely verbose Sensor payload from
#: dominating the mail.
_MAX_FIELD_CHARS: Final[int] = 200

#: How many evidence lines one finding mail enumerates (#2721). The finding's
#: ``evidence`` list is model-authored and carries no schema bound, so the
#: mail takes the leading lines and says how many it dropped.
_MAX_EVIDENCE_LINES: Final[int] = 20

#: Character bound on each free-text finding field (summary, recommended
#: action). Far larger than :data:`_MAX_FIELD_CHARS` because the summary *is*
#: the message -- clipping a diagnosis to 200 characters would defeat the
#: mail -- but still bounded: ``ChecksFinding.summary`` is model output with
#: no upper length, and an SMTP message is not the place to discover that.
_MAX_FINDING_CHARS: Final[int] = 4000

#: Per-process strong references to in-flight notification tasks -- both
#: transition and finding sends. ``asyncio`` holds only weak references to
#: bare tasks, so without this set a fire-and-forget send could be
#: garbage-collected mid-flight (the ``_INVESTIGATIONS`` rationale in
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
class MemberChange:
    """One member Sensor that got worse while its Dashboard stayed red (#5068).

    :attr:`previous_state` -> :attr:`current_state` is the member's edge as
    the Dashboard counts it (severity-capped, hold-time applied) -- the edge
    the floor and the flap window are judged on when this rides along on a
    :class:`DashboardNotice`. :attr:`sensor_id` keys the member's own flap
    window.
    """

    sensor_id: uuid.UUID
    name: str
    previous_state: str
    current_state: str


@dataclass(frozen=True, slots=True)
class DashboardNotice:
    """One claimed transition, detached for the background send.

    Built while the ``check_dashboards`` row and its member Sensors are live
    inside the claim transaction, so the background task never touches an
    expired ORM object.

    :attr:`tenant_id` exists for the #2732 suppression key -- the same
    per-tenant prefix discipline the #2718 advisory key follows, and what
    keeps the Valkey namespace greppable per tenant.

    :attr:`member_change` set (#5068) makes this a "new problem" notice:
    :attr:`previous_state` and :attr:`current_state` are then both the
    Dashboard's unchanged state, and the member's own edge is what the
    gates judge (:func:`_edge`).
    """

    tenant_id: uuid.UUID
    dashboard_id: uuid.UUID
    name: str
    previous_state: str
    current_state: str
    notify_email: str | None
    notify_min_state: str
    members: tuple[NotifyMember, ...]
    member_change: MemberChange | None = None


def _edge(notice: DashboardNotice) -> tuple[str, str]:
    """The ``(previous, current)`` edge the gates judge this notice on.

    The member's edge for a "new problem" notice, the Dashboard's otherwise.
    """
    if notice.member_change is not None:
        return notice.member_change.previous_state, notice.member_change.current_state
    return notice.previous_state, notice.current_state


def _log_fields(notice: DashboardNotice) -> dict[str, object]:
    """The structured fields every outcome log for *notice* carries."""
    previous, current = _edge(notice)
    fields: dict[str, object] = {
        "dashboard_id": str(notice.dashboard_id),
        "previous_state": previous,
        "current_state": current,
    }
    if notice.member_change is not None:
        fields["member_name"] = notice.member_change.name
        fields["member_sensor_id"] = str(notice.member_change.sensor_id)
    return fields


@dataclass(frozen=True, slots=True)
class FindingNotice:
    """One investigator finding, detached for the background send (#2721).

    A notifier-owned projection of
    :class:`~meho_backplane.checks.investigate.ChecksFinding` plus the
    Dashboard context the mail needs, for the same reason
    :class:`NotifyMember` is one: this module must not import
    :mod:`meho_backplane.checks.investigate`, which imports *it*.

    :attr:`recipient` is the Dashboard's ``notify_email`` (one or more
    comma-separated addresses since #2764) resolved by the investigator, so an
    unconfigured Dashboard arrives here as ``None`` and short-circuits in
    :func:`notify_finding` rather than being filtered at the call site -- the
    skip is then logged in one place. :func:`notify_finding` fans the identical
    mail out to every allowlisted entry, the same seam the transition mail uses.
    """

    dashboard_id: uuid.UUID
    dashboard_name: str
    run_id: uuid.UUID
    verdict: str
    summary: str
    evidence: tuple[str, ...]
    recommended_action: str | None
    recipient: str | None


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


def _suppression_key(
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    state: str,
    *,
    sensor_id: uuid.UUID | None = None,
) -> str:
    """Valkey key bounding one ``(tenant, dashboard, state)`` per window (#2732).

    Unlike the #2718 advisory's key there is no caller segment: the
    notification's audience is the Dashboard's configured recipient(s), not
    whoever happens to dispatch. Nor is there a recipient segment: the claim
    is per edge, so one claim covers every recipient of a multi-recipient
    Dashboard (#2764) -- it cannot burn N windows. Every segment is a UUID or
    a CHECK-constrained vocabulary word, so the ``:`` delimiter cannot alias
    two keys.

    With *sensor_id* the key bounds one ``(tenant, dashboard, sensor,
    state)`` -- the "new problem" window (#5068). The literal ``member``
    segment keeps it apart from the Dashboard-level keys, which
    :func:`_clear_suppression` deletes on recovery; member keys are left to
    expire on their own.
    """
    if sensor_id is not None:
        return f"meho:checks:notify:{tenant_id}:{dashboard_id}:member:{sensor_id}:{state}"
    return f"meho:checks:notify:{tenant_id}:{dashboard_id}:{state}"


async def _claim_suppression(notice: DashboardNotice, window_seconds: int) -> bool:
    """Claim this edge's suppression window; ``True`` iff the mail should send.

    One atomic ``SET key 1 NX EX <window-seconds>`` (the #2718 advisory's
    claim idiom, single key so no pipeline): ``True`` from redis-py 8.0.1's
    ``parse_set_result`` means the key was absent and this notification owns
    the window; ``None`` means a same-state notification already claimed it.
    Claimed **before** the send -- an atomic check-and-claim -- because
    claiming after would race a slow SMTP session against the next flap
    edge and let both mail. The window therefore bounds attempts, not
    confirmed deliveries (see the module docstring).

    Fail-open: any Valkey error warn-logs ``checks_notify_suppression_failed``
    and returns ``True`` -- a missed alert is worse than a duplicate.
    """
    _, current = _edge(notice)
    change = notice.member_change
    key = _suppression_key(
        notice.tenant_id,
        notice.dashboard_id,
        current,
        sensor_id=change.sensor_id if change is not None else None,
    )
    try:
        claimed = await get_broadcast_client().set(key, "1", nx=True, ex=window_seconds)
    except Exception:
        _log().warning(
            "checks_notify_suppression_failed",
            dashboard_id=str(notice.dashboard_id),
            phase="claim",
            exc_info=True,
        )
        return True
    return bool(claimed)


async def _clear_suppression(notice: DashboardNotice) -> None:
    """Drop the Dashboard's suppression keys on a recovery crossing (#2732).

    One ``DEL`` covering every suppressible state, so the incident after an
    all-clear starts with fresh windows and its first non-green crossing
    always mails. Runs on any rank-0 crossing -- before the floor gate --
    because a below-floor recovery (``degraded -> ok`` at the default
    ``critical`` floor) still ends the incident even though it sends no
    mail of its own.

    Fail-open like the claim: a Valkey error warn-logs and the notification
    path continues -- worst case a stale window suppresses one repeat, it
    never drops the current mail.
    """
    keys = [
        _suppression_key(notice.tenant_id, notice.dashboard_id, state)
        for state in _SUPPRESSIBLE_STATES
    ]
    try:
        await get_broadcast_client().delete(*keys)
    except Exception:
        _log().warning(
            "checks_notify_suppression_failed",
            dashboard_id=str(notice.dashboard_id),
            phase="clear",
            exc_info=True,
        )


def _single_line(value: str) -> str:
    """Fold every control character out of *value* (subject-header safety)."""
    return "".join(" " if ch.isspace() or not ch.isprintable() else ch for ch in value).strip()


def _clip(value: object) -> str:
    """Render *value* as a bounded single-line fragment for the mail body."""
    text = " ".join(str(value).split())
    if len(text) > _MAX_FIELD_CHARS:
        return text[:_MAX_FIELD_CHARS] + "..."
    return text


def _truncate(text: str, limit: int) -> str:
    """Bound *text* to *limit* characters, preserving its line structure.

    Unlike :func:`_clip` this keeps newlines: a finding summary is prose the
    model laid out in paragraphs, and collapsing it to one line would make
    the mail harder to read than the memory entry it mirrors.
    """
    stripped = text.rstrip()
    if len(stripped) > limit:
        return stripped[:limit] + "..."
    return stripped


def _subject(notice: DashboardNotice) -> str:
    """Build the single-line subject.

    An "all clear" prefix keys on *no member being non-green* rather than on
    the edge improving: ``critical -> degraded`` improves but still has
    failures to name, and the recipient's filter rule cares about whether
    anything is still broken. A "new problem" notice (#5068) names the
    member and its edge, and says the Dashboard is still in its state --
    the Sensor name is operator-authored text, so it is folded like the
    Dashboard name.
    """
    name = _single_line(notice.name)
    change = notice.member_change
    if change is not None:
        member = _single_line(change.name)
        return (
            f"[MEHO] {name}: new problem {member} "
            f"{change.previous_state} -> {change.current_state} "
            f"(dashboard still {notice.current_state})"
        )
    prefix = "" if notice.members else "all clear - "
    return f"[MEHO] {prefix}{name}: {notice.previous_state} -> {notice.current_state}"


def _body(notice: DashboardNotice) -> str:
    """Build the plain-text body: the edge, then the non-green members.

    A "new problem" notice (#5068) states the unchanged Dashboard state and
    the member's edge up front, then marks that member ``(new)`` in the
    list so the recipient finds it without reading every block.
    """
    change = notice.member_change
    lines = [f"Dashboard: {notice.name}"]
    if change is not None:
        lines.append(f"State: {notice.current_state} (unchanged)")
        lines.append(
            f"New problem: {_clip(change.name)} {change.previous_state} -> {change.current_state}"
        )
    else:
        lines.append(f"State: {notice.previous_state} -> {notice.current_state}")
    lines.append("")
    if not notice.members:
        lines.append("All clear - no member sensor is in a failing state.")
        return "\n".join(lines) + "\n"

    shown = notice.members[:_MAX_MEMBER_LINES]
    lines.append(f"Non-green members ({len(notice.members)}):")
    lines.append("")
    for member in shown:
        marker = " (new)" if change is not None and member.name == change.name else ""
        lines.append(f"- {_clip(member.name)} [{member.effective_state}]{marker}")
        lines.append(f"    last value: {_clip(member.last_value)}")
        lines.append(f"    evidence:   {_clip(member.last_evidence)}")
    if len(notice.members) > len(shown):
        lines.append("")
        lines.append(f"({len(notice.members) - len(shown)} further member(s) not shown.)")
    return "\n".join(lines) + "\n"


def _allowed_recipients(raw: str, **log_fields: object) -> list[str]:
    """Split a comma-joined ``notify_email`` and keep the allowlisted entries.

    The wire schema stores one or more recipients comma-joined (#2764). Each
    entry is screened **individually** against the deployment recipient
    allowlist -- the same
    :func:`~meho_backplane.connectors.mail.allowlist.assert_recipient_allowed`
    floor the transport enforces, reused here so one refused entry drops only
    itself. A refused entry warn-logs ``checks_notify_recipient_refused`` and
    is skipped; the survivors are returned for a single
    :func:`~meho_backplane.connectors.mail.transport.send_email` fan-out. A
    refused recipient must never silence delivery to the allowlisted rest --
    the missed-alert-is-worse posture the module holds elsewhere.

    ``send_email``'s own all-or-nothing screen is deliberately left untouched
    (its audit-honesty rationale stands for the dispatched ``mail.send`` path);
    it re-screens this already-clean survivor set and passes it as a whole.
    """
    survivors: list[str] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            assert_recipient_allowed(entry)
        except RecipientNotAllowedError:
            _log().warning("checks_notify_recipient_refused", recipient=entry, **log_fields)
            continue
        survivors.append(entry)
    return survivors


async def _deliver(notice: DashboardNotice, recipients: list[str]) -> None:
    """Fan the identical mail out to *recipients*; swallow every failure.

    Split out of :func:`notify_dashboard_transition` so that function reads
    as the gates it is, and so the never-raise guard wraps exactly the I/O
    rather than the gates too. One
    :func:`~meho_backplane.connectors.mail.transport.send_email` call delivers
    the same message to every recipient (#2764); *recipients* is the
    already-allowlisted survivor set from :func:`_allowed_recipients`, so the
    transport's own all-or-nothing floor passes them as a whole.
    """
    try:
        result = await send_email(
            to=recipients,
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
        member_count=len(notice.members),
        recipient_count=len(recipients),
        **_log_fields(notice),
    )


async def notify_dashboard_transition(notice: DashboardNotice) -> None:
    """Mail the Dashboard's recipient(s) about one claimed transition.

    Never raises. Short-circuits, in order:

    * ``notify_email`` unset -- notifications are off for this Dashboard
      (the state every pre-#2719 row backfills to);
    * the edge does not reach ``notify_min_state`` (see
      :func:`_crosses_threshold`);
    * a same-state notification already claimed this edge's flap window
      (#2732, see :func:`_claim_suppression`; skipped entirely when the
      window is ``0`` or the crossing is into a rank-0 state);
    * every configured recipient is refused by the allowlist, so there is
      nothing to send (``checks_notify_no_allowed_recipients``).

    A rank-0 crossing (recovery) additionally clears the Dashboard's
    suppression keys before the floor gate -- the incident is over whether
    or not the recovery edge itself mails.

    Otherwise the configured recipients are screened individually
    (:func:`_allowed_recipients`) and :func:`_deliver` fans one
    :func:`~meho_backplane.connectors.mail.transport.send_email` call out to
    the allowlisted survivors (#2764).

    A "new problem" notice (#5068) runs the same gates on the member's own
    edge (:func:`_edge`): the floor is judged on the member's new state and
    the flap window is the member's. Its new state is always non-green, so
    it never clears the Dashboard's windows.
    """
    if not notice.notify_email:
        _log().info(
            "checks_notify_skipped_unconfigured",
            dashboard_id=str(notice.dashboard_id),
        )
        return
    previous, current = _edge(notice)
    window_minutes = get_settings().checks_notify_suppression_window_minutes
    suppressible = current in _SUPPRESSIBLE_STATES
    if window_minutes > 0 and not suppressible:
        await _clear_suppression(notice)
    if not _crosses_threshold(previous, current, notice.notify_min_state):
        _log().info(
            "checks_notify_skipped_below_threshold",
            notify_min_state=notice.notify_min_state,
            **_log_fields(notice),
        )
        return
    if (
        window_minutes > 0
        and suppressible
        and not await _claim_suppression(notice, window_minutes * 60)
    ):
        _log().info(
            "checks_notify_suppressed",
            window_minutes=window_minutes,
            **_log_fields(notice),
        )
        return
    recipients = _allowed_recipients(notice.notify_email, dashboard_id=str(notice.dashboard_id))
    if not recipients:
        _log().warning(
            "checks_notify_no_allowed_recipients",
            dashboard_id=str(notice.dashboard_id),
        )
        return
    await _deliver(notice, recipients)


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


def _finding_subject(notice: FindingNotice) -> str:
    """Build the single-line subject for a finding mail.

    Leads with the verdict because that is the triage bit: an
    ``actionable`` finding needs a human now, a ``benign`` one is an FYI the
    recipient's filter rule can route away.
    """
    name = _single_line(notice.dashboard_name)
    return f"[MEHO] investigation {_single_line(notice.verdict)}: {name}"


def _finding_body(notice: FindingNotice) -> str:
    """Build the plain-text body: verdict header, summary, evidence, action.

    Deliberately the same section order as
    :func:`~meho_backplane.checks.investigate._render_finding_body` (the
    memory entry the same finding is written to), so an operator reading the
    mail and an operator reading the memory entry see one shape. The
    ``run_id`` is carried so the recipient can pull the full durable run
    (``meho agent runs get <run_id>``) -- the mail is a summary, the run row
    is the record.
    """
    lines = [
        f"Dashboard: {notice.dashboard_name}",
        f"Verdict: {notice.verdict}",
        f"Run: {notice.run_id}",
        "",
        "## Summary",
        "",
        _truncate(notice.summary, _MAX_FINDING_CHARS),
    ]
    if notice.evidence:
        shown = notice.evidence[:_MAX_EVIDENCE_LINES]
        lines.append("")
        lines.append("## Evidence")
        lines.append("")
        lines.extend(f"- {_clip(line)}" for line in shown)
        if len(notice.evidence) > len(shown):
            lines.append(f"({len(notice.evidence) - len(shown)} further line(s) not shown.)")
    if notice.recommended_action:
        lines.append("")
        lines.append("## Recommended action")
        lines.append("")
        lines.append(_truncate(notice.recommended_action, _MAX_FINDING_CHARS))
        lines.append("")
        lines.append(
            "Advisory text only -- the investigator is diagnose-only and executed "
            "nothing. Any change op it attempted is parked for operator approval."
        )
    return "\n".join(lines) + "\n"


async def notify_finding(notice: FindingNotice) -> None:
    """Mail one persisted investigator finding to the Dashboard's recipient(s).

    Never raises. Short-circuits when the Dashboard has no ``notify_email``
    (the state every pre-#2719 row backfills to) or when every configured
    recipient is refused by the allowlist
    (``checks_finding_email_no_allowed_recipients``). Since #2764 the identical
    finding mail fans out to every allowlisted recipient in one send, each
    screened individually via :func:`_allowed_recipients` -- the same seam the
    transition mail uses.

    There is deliberately **no** ``notify_min_state`` gate here.
    ``notify_min_state`` is a floor on a transition *edge* --
    ``max(rank(previous), rank(current))``, see :func:`_crosses_threshold` --
    and a finding is not an edge, so applying it would be a category error.
    The volume control for findings is the investigator's own fire gate,
    which is already far narrower than any state floor: a finding exists only
    when the edge worsened into a non-green state with an actively-failing
    member, the correlated cause was novel (not noise-suppressed), no run for
    it was already in flight, and the budget gate admitted it. #2732 re-put
    the question and settled on keeping this shape -- the operator-surprise
    tradeoff (finding mail about a ``degraded`` Dashboard reaching someone on
    the default ``critical`` floor) is documented in
    ``docs/codebase/checks-notifications.md``. The same fire gate is why the
    #2732 flap-suppression window does not apply here either.
    """
    if not notice.recipient:
        _log().info(
            "checks_finding_email_skipped_unconfigured",
            dashboard_id=str(notice.dashboard_id),
            run_id=str(notice.run_id),
        )
        return
    recipients = _allowed_recipients(
        notice.recipient,
        dashboard_id=str(notice.dashboard_id),
        run_id=str(notice.run_id),
    )
    if not recipients:
        _log().warning(
            "checks_finding_email_no_allowed_recipients",
            dashboard_id=str(notice.dashboard_id),
            run_id=str(notice.run_id),
        )
        return
    try:
        result = await send_email(
            to=recipients,
            subject=_finding_subject(notice),
            body=_finding_body(notice),
        )
    except Exception:
        # Contract: a mail failure never reaches the investigation seam.
        _log().warning(
            "checks_finding_email_failed",
            dashboard_id=str(notice.dashboard_id),
            run_id=str(notice.run_id),
            reason="unexpected_error",
            exc_info=True,
        )
        return

    if not result.sent:
        _log().warning(
            "checks_finding_email_failed",
            dashboard_id=str(notice.dashboard_id),
            run_id=str(notice.run_id),
            reason=result.reason,
        )
        return
    _log().info(
        "checks_finding_email_sent",
        dashboard_id=str(notice.dashboard_id),
        run_id=str(notice.run_id),
        verdict=notice.verdict,
        recipient_count=len(recipients),
    )


def schedule_finding_notification(notice: FindingNotice) -> None:
    """Spawn the finding send as a tracked fire-and-forget task.

    Keeps the SMTP session off the investigator's serial cause-group loop:
    ``run_investigation`` awaits each group to a terminal outcome before the
    next fires (the budget-gate discipline, #2576), so an inline 30-second
    session would delay the next group's *diagnosis*, not just its mail.
    :func:`notify_finding` swallows every exception, so the task cannot end
    in an unretrieved exception; :class:`asyncio.CancelledError` still
    propagates so lifespan shutdown tears the task down cleanly.
    """
    task = asyncio.create_task(
        notify_finding(notice),
        name=f"checks-finding-mail-{notice.run_id}",
    )
    _NOTIFICATIONS.add(task)
    task.add_done_callback(_NOTIFICATIONS.discard)


async def _await_pending_notifications() -> None:
    """Await all in-flight notification tasks -- a deterministic test seam.

    Production spawns sends fire-and-forget; tests drain them before
    asserting on the transport. Covers both transition and finding sends
    (one task set). Mirrors
    :func:`meho_backplane.checks.investigate._await_pending_investigations`.
    """
    pending = [task for task in _NOTIFICATIONS if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
