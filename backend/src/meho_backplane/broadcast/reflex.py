# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time reflex advisory (#3133, Initiative #3128).

The **third** in-band ``extras`` fragment on successful dispatch
responses, beside the #2550 ``target_activity_advisory``
(:mod:`meho_backplane.broadcast.history`) and the #2718
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
* **announce-before-mutate** -- the op is write-class
  (:func:`~meho_backplane.broadcast.events.classify_op` in
  :data:`~meho_backplane.broadcast.history.WRITE_OP_CLASSES`) and the
  caller holds no active announce claim covering it
  (:func:`~meho_backplane.broadcast.history.caller_has_active_announce_claim`).
  A one-liner names ``meho_broadcast_announce``.

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

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import classify_op
from meho_backplane.broadcast.history import (
    WRITE_OP_CLASSES,
    caller_has_active_announce_claim,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
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

#: The nudges. One line each, imperative, naming the exact meta-tool.
_READ_NUDGE: Final[str] = (
    "This session has not read the broadcast feed yet. Call "
    "meho_broadcast_recent before acting so you see what other operators "
    "and agents are already doing in this tenant."
)
_ANNOUNCE_NUDGE: Final[str] = (
    "You are running a write-class operation without an active announce "
    "claim. Call meho_broadcast_announce first so concurrent operators "
    "and agents can see your intent."
)


def _dedupe_key(session_id: uuid.UUID, heuristic: str) -> str:
    """Valkey key deduping one ``(session, heuristic)`` per window."""
    return f"meho:reflex:advisory:{session_id}:{heuristic}"


async def _session_read_broadcast(session_id: uuid.UUID) -> bool:
    """Whether this MCP session already called ``meho_broadcast_recent``.

    One indexed existence probe on ``audit_log`` -- the
    ``audit_log_agent_session_id_idx`` b-tree makes the
    ``agent_session_id`` predicate selective, and the query is
    ``LIMIT 1``-shaped via ``EXISTS``. The current dispatch's own audit
    row (a ``call_operation`` / inner-dispatch row) never matches the
    ``meho_broadcast_recent`` path, so there is no self-match to exclude.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                exists().where(
                    AuditLog.agent_session_id == session_id,
                    AuditLog.path == _BROADCAST_RECENT_PATH,
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
    op_id: str,
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
      read-class, or the caller holds a covering active claim);
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
        if not await _session_read_broadcast(session_id) and await _claim(
            session_id, _HEURISTIC_READ, window_minutes
        ):
            return {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}

        # announce-before-mutate -- only for write-class ops; the claim is
        # issued only when the op qualifies and holds no covering claim.
        if (
            classify_op(op_id) in WRITE_OP_CLASSES
            and not await caller_has_active_announce_claim(
                operator, op_id=op_id, target_name=target_name
            )
            and await _claim(session_id, _HEURISTIC_ANNOUNCE, window_minutes)
        ):
            return {REFLEX_ADVISORY_EXTRAS_KEY: _ANNOUNCE_NUDGE}
    except Exception:
        # Advisory is best-effort; any failure (a DB or Valkey teardown, a
        # parse bug) must never convert a successful dispatch into a failure.
        _log.warning("reflex_advisory_failed", session_id=str(session_id))
        return {}

    return {}
