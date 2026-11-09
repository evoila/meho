# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time reflex advisory (#3133, Initiative #3128).

The **third** in-band ``extras`` fragment on successful dispatch
responses, beside the #2550 ``target_activity_advisory``
(:mod:`meho_backplane.broadcast.target_advisory`) and the #2718
``checks_alert_advisory`` (:mod:`meho_backplane.checks.advisory`). It
nudges an agent toward the coordination discipline the backplane already
exposes but does not otherwise enforce: read the feed before starting,
announce before mutating. When a heuristic fires the response carries one
compact ``extras["reflex_advisory"]`` one-liner; otherwise the key is
absent.

Heuristics (v1):

* **read-before-act** -- the calling MCP session has no prior
  ``meho_broadcast_recent`` call recorded in ``audit_log`` (correlated by
  ``agent_session_id``). A one-liner recommends reading the feed first.
  A service login with a ``work_ref`` runs its dispatch under the session
  of its orchestration run, but reads the feed under its MCP session, so
  the probe looks under both (#5489). A hosted agent run counts only its
  own run session.
* **announce-before-mutate** -- the op is write-class (the class the
  dispatcher stores on its audit row,
  :func:`~meho_backplane.operations._audit.descriptor_op_class`, is in
  :data:`~meho_backplane.broadcast.history.WRITE_OP_CLASSES`) and the
  caller holds no active announce claim covering it
  (:func:`~meho_backplane.broadcast.announce_claim_lookup.announce_claim_gap`). A
  one-liner names ``meho_broadcast_announce`` and says why no claim
  covers the op: none at all, expired, another target, or another
  planned op class (#4091). Only a caller that may publish an
  announcement gets it: a live hosted agent run or a registered agent
  principal (:func:`~meho_backplane.auth.agent_admission.resolve_agent_admission`).
  Anyone else would be refused, so MEHO does not tell them to announce
  (#5489).

Design contract (mirrors the #2550 / #2718 mould):

* **Advisory only.** Never gates, blocks, or fails a dispatch. The
  fragment rides an already-successful response, built after the
  synchronous audit commit -- never on a denied / error /
  awaiting-approval envelope.
* **Session-scoped.** The nudge targets an *agent session*, so it is
  keyed on :func:`~meho_backplane.operations._audit.resolve_agent_session_id`.
  A dispatch with no session id -- a CLI / REST operator call, a system
  sweep -- is not an agent session and gets no nudge (returns ``{}``
  before any I/O). This is a data-driven no-op on the shared dispatch
  path, not a surface-specific branch: an MCP ``call_operation`` carries
  a session and an operator ``meho operation call`` does not, so the
  same code nudges the former and stays silent for the latter.
* **One line per response.** The fragment is a single string. When more
  than one heuristic qualifies the builder returns the first (in
  priority order: read-before-act, then announce-before-mutate) whose
  per-``(session, heuristic)`` dedupe claim it wins, and leaves the
  other heuristic's claim untouched so a later response can still carry
  it.
* **Deduped.** At most one nudge per ``(agent_session_id, heuristic)``
  per window, via an atomic Valkey ``SET NX EX`` -- the #2718 dedupe
  primitive, one key per heuristic.
* **Fail-open.** Any error -- a DB teardown, a Valkey teardown, a parse
  bug -- is swallowed by a broad guard, warn-logged as
  ``reflex_advisory_failed``, and yields ``{}`` (mirroring the
  ``dispatch_audit_failed`` posture). The nudge never converts a
  successful dispatch into a failure.
* **``0`` disables.** ``REFLEX_ADVISORY_WINDOW_MINUTES`` (default 30)
  short-circuits before any session resolution or I/O when ``0``.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from sqlalchemy import exists, select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast.announce_claim_lookup import ClaimGap, announce_claim_gap
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.history import WRITE_OP_CLASSES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, RequestUsageEvent
from meho_backplane.settings import get_settings

__all__ = [
    "REFLEX_ADVISORY_EXTRAS_KEY",
    "build_reflex_advisory",
]

_log = structlog.get_logger(__name__)

#: The ``extras`` key the advisory rides on the ``OperationResult`` --
#: sibling of ``target_activity_advisory`` (#2550) and
#: ``checks_alert_advisory`` (#2718).
REFLEX_ADVISORY_EXTRAS_KEY: Final[str] = "reflex_advisory"

#: The heuristic names -- also the ``SET NX EX`` dedupe-key segment, so a
#: session is reminded of each discipline at most once per window.
_HEURISTIC_READ: Final[str] = "read_before_act"
_HEURISTIC_ANNOUNCE: Final[str] = "announce_before_mutate"

#: The ``audit_log.path`` a ``meho_broadcast_recent`` MCP tool call lands
#: as -- ``write_mcp_audit_row`` writes ``/mcp/tools/call/{tool_name}``
#: (:mod:`meho_backplane.mcp.audit`). The read-before-act heuristic keys
#: on this stable, dialect-portable string column rather than the
#: JSON ``payload.op_id`` (whose value is the same ``meho_broadcast_recent``
#: but needs a per-dialect JSON accessor). ``meho_broadcast_watch`` is
#: deliberately not counted here -- the discipline's read step the issue
#: names is the recent-feed pull.
_BROADCAST_RECENT_PATH: Final[str] = "/mcp/tools/call/meho_broadcast_recent"

#: The request usage route of the same call (#3734). After the wrapper row
#: cutover (#3757) a read tool call writes no ``audit_log`` row, only this
#: usage event, so the probe looks in both places.
_BROADCAST_RECENT_ROUTE: Final[str] = "tool:meho_broadcast_recent"

#: The nudges. One line each, imperative, naming the exact meta-tool.
_READ_NUDGE: Final[str] = (
    "This session has not read the broadcast feed yet. Call "
    "meho_broadcast_recent before acting so you see what other operators "
    "and agents are already doing in this tenant."
)

#: One announce nudge per reason (#4091), so the agent knows what to fix.
#: ``{op_class}`` is the class the dispatcher stores on the op's audit row.
_ANNOUNCE_NUDGES: Final[dict[ClaimGap, str]] = {
    "no_claim": (
        "You are running a write-class operation without an active announce "
        "claim. Call meho_broadcast_announce first so concurrent operators "
        "and agents can see your intent."
    ),
    "expired": (
        "You are running a write-class operation, but your announce claim for "
        "it has expired. Call meho_broadcast_announce again so concurrent "
        "operators and agents can see your intent."
    ),
    "other_target": (
        "You are running a write-class operation on a target that none of "
        "your active announce claims name. Call meho_broadcast_announce with "
        "this target so concurrent operators and agents can see your intent."
    ),
    "other_op_class": (
        "You are running a '{op_class}' operation, but your active announce "
        "claim declared another planned_op_class. Call "
        "meho_broadcast_announce with planned_op_class '{op_class}' so "
        "concurrent operators and agents can see your intent."
    ),
}


def _dedupe_key(session_id: uuid.UUID, heuristic: str) -> str:
    """Valkey key deduping one ``(session, heuristic)`` per window."""
    return f"meho:reflex:advisory:{session_id}:{heuristic}"


def _read_probe_session_ids(session_id: uuid.UUID) -> list[uuid.UUID]:
    """The session ids under which this caller's feed read may be stored.

    The read is stored under the MCP session id. A service login with a
    ``work_ref`` dispatches with :data:`agent_session_id_var` set to the
    session of its orchestration run (#3446), so *session_id* is that run
    and not the MCP session. The MCP session id is added in that case.

    Inside a hosted agent run the run is the session, and a read by the
    outer MCP session does not count, so the run id stays the only one.
    """
    # Lazy import: ``meho_backplane.agent`` imports the broadcast package.
    from meho_backplane.agent.invoke import current_agent_run_id_var

    ids = [session_id]
    if current_agent_run_id_var.get() is not None:
        return ids
    raw = structlog.contextvars.get_contextvars().get("mcp_session_id")
    if isinstance(raw, str):
        try:
            mcp_session_id = uuid.UUID(raw)
        except ValueError:
            return ids
        if mcp_session_id != session_id:
            ids.append(mcp_session_id)
    return ids


async def _may_announce(operator: Operator) -> bool:
    """Whether MEHO would accept an announcement from *operator*.

    Uses the same check as ``meho_broadcast_announce``. A caller outside a
    hosted agent run whose token is not an agent token is refused there
    without any lookup, so it is answered here without one as well; this
    also keeps the refusal log of the announce path free of entries for
    announcements nobody sent.
    """
    # Lazy imports: ``meho_backplane.agent`` imports the broadcast package,
    # and ``auth.agent_admission`` imports ``broadcast.agent_events``.
    from meho_backplane.agent.invoke import current_agent_run_id_var
    from meho_backplane.auth.agent_admission import (
        AgentIdentityRequiredError,
        resolve_agent_admission,
    )

    if (
        current_agent_run_id_var.get() is None
        and operator.principal_kind is not PrincipalKind.AGENT
    ):
        return False
    try:
        await resolve_agent_admission(operator)
    except AgentIdentityRequiredError:
        return False
    return True


async def _session_read_broadcast(session_ids: list[uuid.UUID]) -> bool:
    """Whether this MCP session already called ``meho_broadcast_recent``.

    One indexed existence probe on ``audit_log`` -- the
    ``audit_log_agent_session_id_idx`` b-tree makes the
    ``agent_session_id`` predicate selective, and the query is
    ``LIMIT 1``-shaped via ``EXISTS``. The current dispatch's own audit
    row (a ``call_operation`` / inner-dispatch row) never matches the
    ``meho_broadcast_recent`` path, so there is no self-match to exclude.

    The probe also checks the ``request_finished`` usage events of the
    session (#3757): once the wrapper rows are retired, the read leaves
    only that event. Either record counts.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                exists().where(
                    AuditLog.agent_session_id.in_(session_ids),
                    AuditLog.path == _BROADCAST_RECENT_PATH,
                )
                | exists().where(
                    RequestUsageEvent.session_id.in_(session_ids),
                    RequestUsageEvent.kind == "request_finished",
                    RequestUsageEvent.route == _BROADCAST_RECENT_ROUTE,
                )
            )
        )
        return bool(result.scalar())


async def _claim(session_id: uuid.UUID, heuristic: str, window_minutes: int) -> bool:
    """Atomically claim the ``(session, heuristic)`` dedupe key.

    ``SET key 1 NX EX <window-seconds>`` -- returns ``True`` when the key
    was absent (this response wins the nudge) and ``None`` when it already
    existed (already nudged this window). One round-trip; at most one
    claim per response because the builder short-circuits on the first
    win.
    """
    client = get_broadcast_client()
    claimed = await client.set(
        _dedupe_key(session_id, heuristic), "1", nx=True, ex=window_minutes * 60
    )
    return bool(claimed)


async def build_reflex_advisory(
    operator: Operator,
    *,
    op_class: str,
    target_name: str | None,
) -> dict[str, Any]:
    """Build the ``extras`` fragment nudging toward coordination discipline.

    Returns ``{"reflex_advisory": "<one line>"}`` or an empty dict (no key
    added) when no heuristic both qualifies and wins its dedupe claim. The
    empty-dict short-circuits, in order:

    * the feature is disabled (``reflex_advisory_window_minutes == 0`` --
      checked before any session resolution or I/O);
    * the dispatch has no agent session
      (:func:`resolve_agent_session_id` returns ``None``);
    * read-before-act does not qualify (the session already read the
      feed) and announce-before-mutate does not qualify (the op is
      read-class, the caller may not publish an announcement, or the
      caller holds a covering active claim);
    * every qualifying heuristic was already nudged to this session in
      the current window (its ``SET NX`` found its key present).

    Fail-open: any error is swallowed by the broad guard, warn-logged as
    ``reflex_advisory_failed``, and yields ``{}``.
    """
    window_minutes = get_settings().reflex_advisory_window_minutes
    if window_minutes <= 0:
        return {}

    # Lazy import: ``operations._audit`` imports the ``broadcast`` package,
    # so a module-level import here would couple the two packages at load
    # time. The resolver is cheap and this keeps the import graph acyclic.
    from meho_backplane.operations._audit import resolve_agent_session_id

    session_id = resolve_agent_session_id()
    if session_id is None:
        return {}

    try:
        # read-before-act -- the more fundamental discipline, checked
        # first. The ``and`` short-circuits so the (write) claim is only
        # issued when the session has not read the feed; a qualified-but-
        # already-nudged read falls through to announce with its key
        # untouched.
        if not await _session_read_broadcast(_read_probe_session_ids(session_id)) and await _claim(
            session_id, _HEURISTIC_READ, window_minutes
        ):
            return {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}

        # announce-before-mutate -- only for write-class ops by a caller
        # that may publish; the claim is issued only when the op qualifies
        # and holds no covering claim.
        if op_class not in WRITE_OP_CLASSES or not await _may_announce(operator):
            return {}
        gap = await announce_claim_gap(operator, op_class=op_class, target_name=target_name)
        if gap is not None and await _claim(session_id, _HEURISTIC_ANNOUNCE, window_minutes):
            return {REFLEX_ADVISORY_EXTRAS_KEY: _ANNOUNCE_NUDGES[gap].format(op_class=op_class)}
    except Exception:
        # Advisory is best-effort; any failure (a DB or Valkey teardown, a
        # parse bug) must never convert a successful dispatch into a failure.
        _log.warning("reflex_advisory_failed", session_id=str(session_id))
        return {}

    return {}
