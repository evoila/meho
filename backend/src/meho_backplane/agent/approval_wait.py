# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-runtime resume substrate: wait for an approval decision (G11.1-T9 / #1117).

This module is the agent-side half of the **operator/agent split** that the
G11.2 approval substrate established:

* The operator decides — via REST ``/decide``, MCP ``meho_approvals_{approve,reject}``,
  CLI, or wall-monitor — and the decision row + ``approval.{approved,rejected}``
  broadcast event commit durably (see :mod:`meho_backplane.operations.approval_queue`).
* The agent run that bridged the ``requires_approval`` op is left awaiting that
  decision. Until #1117 the only resume path was the REST ``/approve`` endpoint
  with the original ``params``, which re-dispatches inline. Every other operator
  path captured the decision but left the agent run dead-on-arrival: the agent
  was paused in a ``call_operation`` frame and nothing told it the decision
  landed.

The helper here closes that gap. It subscribes to the per-tenant Valkey stream
(``meho:feed:{tenant_id}`` — the same stream the SSE feed and the
``meho_broadcast_watch`` tool read from) and blocks until it sees the
``approval.{approved,rejected,expired}`` event for *this* request, or the
stored approval row shows a decision, or the timeout elapses. The caller (the
agent's wrapped ``call_operation`` tool) then either re-dispatches with
``_approved=True`` (approved), reports the rejection or expiry back to the
model (no dispatch), or surfaces a timeout as a structured tool result the
agent can reason about.

Broadcast first, stored row as the fallback (#3732)
===================================================

The broadcast is the fast path: sub-second latency, and the same channel the
UI wall-monitor consumes, so the agent learns of decisions in lockstep with
human operators. But the broadcast is fail-open at the publish side (an
outage never blocks the durable decision), and an event can be missed. So
the stored :class:`~meho_backplane.db.models.ApprovalRequest` row, read
tenant-scoped through
:func:`~meho_backplane.operations.approval_queue.get_request`, is the
authority. The wait reads it:

* once before the first ``XREAD`` (a decision that landed before the wait
  started is not missed);
* after every notifier error, and at least every
  :data:`_DURABLE_RECHECK_SECONDS` while the row stays ``pending``;
* once more before it returns ``"timeout"``.

Each ``XREAD`` is also cut off locally (:func:`asyncio.timeout`) at the
moment the next stored-row read is due: the recheck boundary or the
deadline, whichever comes first. Its server-side ``BLOCK`` window ends
one reply grace before that moment, so a healthy reply lands in time. A
Valkey that accepts the command but never answers would otherwise hold
the wait for the client's full socket timeout, well past the next
stored-row read and the deadline. A cut-off ``XREAD`` counts as a
notifier error: the row is read at once.

So a ``"timeout"`` means the row was still ``pending`` (or could not be
read) at the deadline. This is a bounded fallback, not a polling service:
one primary-key read per interval, only while an agent waits.

An approval status is not an execution result. ``approved`` only allows the
caller to try the existing exactly-one-resumer claim and re-dispatch; the
dispatcher's envelope reports what actually happened.

Why ``XREAD BLOCK`` and not the high-level ``watch`` tool
=========================================================

The MCP ``meho_broadcast_watch`` tool wraps ``XREAD BLOCK`` plus filtering,
but it's bounded by ``_WATCH_MAX_TIMEOUT_MS`` (30s) — a sensible cap for
an MCP-call latency budget, the wrong shape for a human approval which can
take minutes to hours. This helper drives ``XREAD BLOCK`` directly with the
agent-runtime's own timeout (default 30 min, settings-driven), looping over
small block windows so cancellation (operator cancels the agent run) lands
promptly.

Cursor discipline
=================

``XREAD`` reads entries strictly past the cursor it's given (last-id-seen
exclusive). The wait starts the cursor at ``"$"`` (the "tail from this moment
forward" cursor); subsequent reads in the loop advance to the most recent
entry id seen so a busy tenant doesn't re-emit already-considered entries.
A decision published before the first ``XREAD`` is not on that tail; the
stored-row reads above catch it instead.

Fail-open semantics
===================

A recoverable broadcast failure (Valkey unreachable, malformed entry, parse
error) never raises. The wait re-reads the stored row and keeps waiting until
the deadline. A stored-row read that fails (database error, row not visible
to this tenant) is also logged and treated as "no decision seen yet". Only
when the deadline passes with no decision seen does the wait return
``"timeout"``, which the wrapper maps to a structured
awaiting-approval-timeout tool result. ``asyncio.CancelledError`` always
propagates; nothing retries after a cancel.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import time
import uuid
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from meho_backplane.broadcast.client import get_broadcast_blocking_client

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "AWAITING_APPROVAL_TIMEOUT_ERROR_CODE",
    "ApprovalDecision",
    "resume_or_surface_awaiting_approval",
    "wait_for_approval_decision",
]

_log = structlog.get_logger(__name__)

#: One :class:`Literal` return shape per outcome the caller switches on.
#: ``approved`` / ``rejected`` / ``expired`` mirror the terminal states of the
#: stored approval row; ``timeout`` means no decision was seen before the
#: deadline (the row was still ``pending``, or could not be read).
ApprovalDecision = Literal["approved", "rejected", "expired", "timeout"]

#: Longest gap between two reads of the stored approval row while the wait
#: runs. The broadcast is the fast path; this bounds how late a decision is
#: seen when its broadcast event is lost (#3732).
_DURABLE_RECHECK_SECONDS: Final[float] = 5.0

#: Per-iteration ``XREAD BLOCK`` window inside the wait loop. Smaller than
#: the overall timeout so an ``asyncio.CancelledError`` (operator cancels the
#: run, the agent loop tears down) lands within this window rather than at
#: the end of the full multi-minute timeout. 5s balances "responsive to
#: cancellation" against "syscall overhead from re-issuing ``XREAD`` too
#: often"; mirrors the ``meho_broadcast_watch`` per-call default with the
#: orders-of-magnitude longer overall wait wrapped around it.
_BLOCK_WINDOW_MS: Final[int] = 5_000

#: How long before the next stored-row read an ``XREAD``'s ``BLOCK`` window
#: ends, so the reply still lands before that read is due. Covers the
#: reply's network latency on a healthy server. The local cut-off sits at
#: the read itself, never later, so a server that accepted the command but
#: never answers cannot delay the read past :data:`_DURABLE_RECHECK_SECONDS`
#: or the overall deadline. Must stay well below the recheck interval.
_XREAD_REPLY_GRACE_SECONDS: Final[float] = 1.0

#: Upper bound on entries returned per ``XREAD`` invocation. The wait only
#: cares about the single matching ``approval.{approved,rejected}`` entry,
#: but a busy tenant may publish many unrelated events between iterations;
#: 100 keeps each batch bounded so the filter loop stays fast.
_XREAD_COUNT: Final[int] = 100

#: Op-id prefix every decision broadcast event carries. The publisher in
#: :func:`~meho_backplane.operations.approval_queue.publish_approval_event`
#: builds ``f"approval.{decision}"`` for ``decision`` in
#: ``{"pending", "approved", "rejected", "expired"}``; we filter for the
#: three terminal decisions on this request id only.
_TERMINAL_OP_IDS: Final[dict[str, ApprovalDecision]] = {
    "approval.approved": "approved",
    "approval.rejected": "rejected",
    "approval.expired": "expired",
}


def _stream_key(tenant_id: uuid.UUID) -> str:
    """Build the per-tenant Valkey Streams key.

    Mirrors :func:`meho_backplane.broadcast.publisher._stream_key` exactly so
    the wait reads from the same stream the publisher writes to. Duplicated
    rather than imported because the publisher's helper is private and the
    one-line shape isn't worth widening that module's surface.
    """
    return f"meho:feed:{tenant_id}"


def _entry_matches_request(
    fields: dict[str, str],
    *,
    target_request_id: str,
) -> ApprovalDecision | None:
    """Return ``"approved"``/``"rejected"``/``"expired"`` if *fields* is THIS
    request's terminal decision, ``None`` otherwise.

    Each stream entry carries a single ``event`` field whose value is the
    JSON-serialised :class:`~meho_backplane.broadcast.events.BroadcastEvent`.
    We parse it, gate on (a) ``op_id`` being one of the terminal decisions
    and (b) ``payload.approval_request_id`` matching the request the agent is
    waiting on. Anything else (other approvals' decisions, ``pending`` for
    this request, unrelated ops) is filtered out and the wait keeps looping.

    Parse failures log + return ``None`` (the wait skips the entry and
    continues) rather than raising — a single malformed entry must not
    poison the whole wait. The audit + DB row are the canonical record;
    the broadcast is a real-time view.
    """
    raw = fields.get("event")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _log.warning("approval_wait_entry_malformed", target_request_id=target_request_id)
        return None
    decision = _TERMINAL_OP_IDS.get(payload.get("op_id"))
    if decision is None:
        return None
    event_payload = payload.get("payload") or {}
    if event_payload.get("approval_request_id") != target_request_id:
        return None
    return decision


def _xread_items(raw_response: Any) -> list[tuple[str, dict[str, str]]]:
    """Unwrap a single-stream ``XREAD`` response into its entry list.

    Returns an empty list for the three "nothing for us" shapes ``XREAD``
    produces (``None`` on BLOCK timeout, empty outer list, inner list
    empty) so the caller has one ``if not items`` branch. Mirrors the
    same idiom :func:`meho_backplane.mcp.tools.broadcast._xread_items_or_none`
    uses; duplicated for the same reason :func:`_stream_key` is.
    """
    if not raw_response:
        return []
    entries = cast(
        "list[tuple[str, list[tuple[str, dict[str, str]]]]]",
        raw_response,
    )
    if not entries:
        return []
    _key, items = entries[0]
    return items if items else []


async def _xread_one_iteration(
    client: Any,
    *,
    stream_key: str,
    cursor: str,
    block_ms: int,
    target_request_id: str,
    tenant_id: uuid.UUID,
    reply_timeout_s: float,
) -> tuple[ApprovalDecision | None, str, bool]:
    """Run one ``XREAD BLOCK`` iteration, cut off locally if the reply stalls.

    Returns ``(decision-or-None, new-cursor, notifier_failed)``. The outcomes
    the caller folds into the surrounding while-loop:

    * ``(decision, _, False)`` — terminal decision observed; the loop
      returns.
    * ``(None, advanced_cursor, False)`` — entries arrived but none matched
      this request; the loop continues with the advanced cursor (so a busy
      tenant doesn't re-process the same entries).
    * ``(None, cursor, False)`` — BLOCK timed out empty; the loop re-issues
      with the same cursor.
    * ``(None, cursor, True)`` — a recoverable broadcast error fired, or the
      reply stalled past the local cut-off; the caller re-reads the stored
      row before it loops again.

    The call is cut off locally after *reply_timeout_s*; the caller sets
    that to the time left until the next stored-row read is due, and a
    *block_ms* that ends :data:`_XREAD_REPLY_GRACE_SECONDS` earlier. Without
    the cut-off a Valkey that accepts ``XREAD`` but never answers holds the
    call for the client's socket timeout (35 s), far past that read.
    A :class:`~redis.exceptions.RedisError` (Valkey unreachable, malformed
    response) or :class:`OSError` (socket-layer hiccup) is logged + folded
    into the "loop again" branch with a small back-off sleep — the same
    fail-open posture the publisher uses at write time.
    :class:`asyncio.CancelledError` propagates verbatim so the agent loop
    tears down cleanly when the run is cancelled.
    """
    try:
        async with asyncio.timeout(reply_timeout_s):
            raw_response = await client.xread(
                {stream_key: cursor},
                block=block_ms,
                count=_XREAD_COUNT,
            )
    except asyncio.CancelledError:
        _log.info(
            "approval_wait_cancelled",
            approval_request_id=target_request_id,
            tenant_id=str(tenant_id),
        )
        raise
    except TimeoutError:
        # Must precede the ``OSError`` branch: ``TimeoutError`` is an
        # ``OSError`` subclass. redis-py disconnects the connection on any
        # exception raised mid-read, so the next ``XREAD`` starts clean.
        _log.warning(
            "approval_wait_xread_stalled",
            approval_request_id=target_request_id,
            tenant_id=str(tenant_id),
            reply_timeout_seconds=reply_timeout_s,
        )
        return (None, cursor, True)
    except (RedisError, OSError) as exc:
        _log.warning(
            "approval_wait_xread_error",
            approval_request_id=target_request_id,
            tenant_id=str(tenant_id),
            error_class=type(exc).__name__,
        )
        await asyncio.sleep(min(1.0, max(0.0, reply_timeout_s)))
        return (None, cursor, True)

    items = _xread_items(raw_response)
    if not items:
        return (None, cursor, False)
    new_cursor = cursor
    for entry_id, fields in items:
        new_cursor = entry_id
        decision = _entry_matches_request(
            fields,
            target_request_id=target_request_id,
        )
        if decision is not None:
            _log.info(
                "approval_wait_decision_observed",
                approval_request_id=target_request_id,
                tenant_id=str(tenant_id),
                decision=decision,
            )
            return (decision, new_cursor, False)
    return (None, new_cursor, False)


async def _read_stored_decision(
    *,
    tenant_id: uuid.UUID,
    approval_request_id: uuid.UUID,
) -> ApprovalDecision | None:
    """Read the stored approval row; return its terminal state, or ``None``.

    ``None`` means "no decision seen": the row is still ``pending``, or it
    could not be read. A read that fails (database error, or no row visible
    to *tenant_id*) is logged and never raises, so a database hiccup does
    not end the wait early. :func:`get_request` scopes the read to the
    tenant, so a row of another tenant is never seen.
    ``asyncio.CancelledError`` is not caught and propagates.

    Local imports: ``approval_queue`` is imported lazily everywhere in this
    module (see :func:`_resume_approved_or_already_resumed`).
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.approval_queue import ApprovalNotFoundError, get_request

    try:
        async with get_sessionmaker()() as session:
            request = await get_request(
                session,
                tenant_id=tenant_id,
                request_id=approval_request_id,
            )
            status = request.status
    except (ApprovalNotFoundError, SQLAlchemyError, OSError) as exc:
        _log.warning(
            "approval_wait_durable_read_unavailable",
            approval_request_id=str(approval_request_id),
            tenant_id=str(tenant_id),
            error_class=type(exc).__name__,
        )
        return None
    if status in ("approved", "rejected", "expired"):
        _log.info(
            "approval_wait_durable_decision_observed",
            approval_request_id=str(approval_request_id),
            tenant_id=str(tenant_id),
            decision=status,
        )
        return cast("ApprovalDecision", status)
    return None


async def wait_for_approval_decision(
    *,
    tenant_id: uuid.UUID,
    approval_request_id: uuid.UUID,
    timeout_seconds: float,
) -> ApprovalDecision:
    """Block until a terminal decision is seen, or time out.

    Reads the stored approval row first. While it is ``pending``, loops
    ``XREAD BLOCK`` on ``meho:feed:{tenant_id}`` until an
    ``approval.{approved,rejected,expired}`` event for *approval_request_id*
    arrives, a re-read of the row shows a decision, or *timeout_seconds*
    passes. The row is re-read after every notifier error, at least every
    :data:`_DURABLE_RECHECK_SECONDS`, and once more at the deadline. Each
    ``XREAD`` is cut off locally at the moment the next re-read is due, so
    a Valkey that stops answering never delays a re-read past
    :data:`_DURABLE_RECHECK_SECONDS` or the deadline.

    Args:
        tenant_id: The operator's tenant. Scopes both the Valkey stream and
            the stored-row read, so another tenant's decision is never seen.
        approval_request_id: The ``approval_request.id`` the agent awaits
            (``extras["approval_request_id"]`` on the envelope).
        timeout_seconds: Overall wait cap (the caller passes
            ``Settings.agent_approval_wait_timeout_seconds``).

    Returns:
        ``"approved"`` (the caller tries the resume claim and re-dispatches),
        ``"rejected"`` or ``"expired"`` (the caller never dispatches), or
        ``"timeout"`` (the final re-read still found the row ``pending``, or
        could not read it).

    ``asyncio.CancelledError`` (the run is cancelled mid-wait) propagates
    verbatim; nothing retries after it.
    """
    deadline = time.monotonic() + timeout_seconds
    # Long-poll client; per-iteration BLOCK is _BLOCK_WINDOW_MS (5 s). The
    # fast client's 5 s socket_timeout races the BLOCK exit (either could
    # win at the boundary), producing spurious redis.TimeoutError raises
    # that the loop has to absorb into a back-off; the blocking client's
    # 35 s socket_timeout removes the race so a quiet stream returns
    # cleanly from XREAD with no entries. RDC #789 N1 / Initiative #1353.
    client = get_broadcast_blocking_client()
    stream_key = _stream_key(tenant_id)
    target_request_id = str(approval_request_id)
    _log.info(
        "approval_wait_started",
        approval_request_id=target_request_id,
        tenant_id=str(tenant_id),
        timeout_seconds=timeout_seconds,
    )
    read_stored = functools.partial(
        _read_stored_decision, tenant_id=tenant_id, approval_request_id=approval_request_id
    )
    # A decision committed before the wait started is on the stored row,
    # not on the ``"$"`` tail below.
    stored = await read_stored()
    if stored is not None:
        return stored
    next_recheck = time.monotonic() + _DURABLE_RECHECK_SECONDS
    # ``"$"`` is the Valkey "tail from the moment of this call" cursor.
    # Subsequent iterations advance the cursor to the latest entry id seen so
    # a busy tenant doesn't re-emit already-considered entries.
    cursor: str = "$"

    while (remaining := deadline - time.monotonic()) > 0:
        # The next stored-row read is due at the recheck boundary or the
        # deadline, whichever comes first. The XREAD is cut off locally at
        # that moment; its BLOCK window ends one reply grace earlier so a
        # healthy reply lands in time. A budget shorter than two graces
        # (the tail before the deadline) is split in half instead, so the
        # loop never spins on 1 ms reads. The ceiling keeps BLOCK from
        # ending a fraction of a millisecond before the read is due.
        read_budget_s = min(remaining, next_recheck - time.monotonic())
        block_s = max(read_budget_s - _XREAD_REPLY_GRACE_SECONDS, read_budget_s / 2)
        if block_s < 0.001:
            # Under 2 ms to the deadline: no room for one more round trip.
            break
        block_ms = min(_BLOCK_WINDOW_MS, math.ceil(block_s * 1000))
        decision, cursor, notifier_failed = await _xread_one_iteration(
            client,
            stream_key=stream_key,
            cursor=cursor,
            block_ms=block_ms,
            target_request_id=target_request_id,
            tenant_id=tenant_id,
            reply_timeout_s=read_budget_s,
        )
        if decision is not None:
            return decision
        # A quiet BLOCK ends one reply grace before the recheck boundary;
        # that counts as due, so the row is read now rather than after one
        # more tiny XREAD.
        if notifier_failed or time.monotonic() >= next_recheck - _XREAD_REPLY_GRACE_SECONDS:
            stored = await read_stored()
            if stored is not None:
                return stored
            next_recheck = time.monotonic() + _DURABLE_RECHECK_SECONDS

    stored = await read_stored()
    if stored is not None:
        return stored
    _log.info(
        "approval_wait_timeout", approval_request_id=target_request_id, tenant_id=str(tenant_id)
    )
    return "timeout"


#: ``extras["error_code"]`` value on a wrapped ``call_operation`` envelope that
#: signals "we waited for the approval decision and timed out". Distinct from
#: the plain ``awaiting_approval`` code (which means "decision still pending,
#: no wait was attempted") so the model can tell the two cases apart and the
#: caller's tests can pin on a stable string.
AWAITING_APPROVAL_TIMEOUT_ERROR_CODE: Final[str] = "awaiting_approval_timeout"


def _build_awaiting_approval_timeout_envelope(
    *,
    original_envelope: dict[str, Any],
    approval_request_id: uuid.UUID,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Annotate the original ``awaiting_approval`` envelope with a timeout marker.

    Returned to the model verbatim — preserves the original envelope's keys
    (so the model still sees the ``op_id``, the ``approval_request_id``, and
    the human-readable ``error`` prose) but overwrites the ``error_code`` with
    :data:`AWAITING_APPROVAL_TIMEOUT_ERROR_CODE` and the ``error`` message with
    a wait-context string. The audit row is untouched. Reached only when the
    final re-read still found the row ``pending`` or could not read it, so
    the decision may still come later.
    """
    extras = dict(original_envelope.get("extras") or {})
    extras["error_code"] = AWAITING_APPROVAL_TIMEOUT_ERROR_CODE
    extras["approval_request_id"] = str(approval_request_id)
    extras["wait_timeout_seconds"] = timeout_seconds
    annotated = dict(original_envelope)
    annotated["extras"] = extras
    annotated["error"] = (
        f"awaiting_approval_timeout: no approval decision observed for "
        f"request {approval_request_id} within {timeout_seconds:.0f}s. "
        "The decision row remains the durable source of truth — re-issue, "
        "or query the approval status before retrying."
    )
    return annotated


def _extract_approval_request_id(awaiting_envelope: dict[str, Any]) -> uuid.UUID:
    """Lift the request id off the envelope's ``extras`` or raise.

    The dispatcher's :func:`~meho_backplane.operations._errors.result_awaiting_approval`
    always populates ``extras["approval_request_id"]`` with the UUID. A
    missing or unparseable value is a dispatcher contract violation and
    surfaces as :class:`ValueError`, so the agent run fails cleanly rather
    than burning a 30-minute wait on an un-resumable request.
    """
    extras = awaiting_envelope.get("extras") or {}
    raw_request_id = extras.get("approval_request_id")
    if not raw_request_id:
        raise ValueError(
            "awaiting_approval envelope is missing extras['approval_request_id']; "
            "cannot resume — dispatcher contract broken",
        )
    try:
        return uuid.UUID(str(raw_request_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"awaiting_approval envelope has unparseable approval_request_id "
            f"{raw_request_id!r}; cannot resume",
        ) from exc


def _build_rejected_envelope(
    *,
    original_envelope: dict[str, Any],
    approval_request_id: uuid.UUID,
) -> dict[str, Any]:
    """Annotate the original envelope with the operator's rejection.

    Keeps ``status="awaiting_approval"`` (NOT ``denied`` — denied is the
    policy gate's verdict shape, not this layer's), rewrites ``error`` to
    the rejection-context string, and stamps ``extras["error_code"] =
    "approval_rejected"`` + ``extras["decision"] = "rejected"`` so the
    agent's model has a stable string to switch on. The decision row +
    the rejected broadcast event are the durable rejection evidence; this
    envelope is the model-side view.
    """
    annotated = dict(original_envelope)
    annotated["error"] = (
        f"awaiting_approval: operator rejected request {approval_request_id}. "
        "Try a different approach or stop."
    )
    extras = dict(original_envelope.get("extras") or {})
    extras["error_code"] = "approval_rejected"
    extras["decision"] = "rejected"
    annotated["extras"] = extras
    return annotated


def _build_expired_envelope(
    *,
    original_envelope: dict[str, Any],
    approval_request_id: uuid.UUID,
) -> dict[str, Any]:
    """Annotate the original envelope when the request expired undecided.

    Same shape as :func:`_build_rejected_envelope`: ``status`` stays
    ``"awaiting_approval"``, ``extras["error_code"] = "approval_expired"`` and
    ``extras["decision"] = "expired"``. The op was not run; an expired
    request is terminal and is never dispatched (#3732).
    """
    annotated = dict(original_envelope)
    annotated["error"] = (
        f"awaiting_approval: approval request {approval_request_id} expired "
        "before anyone decided. The operation was not run."
    )
    extras = dict(original_envelope.get("extras") or {})
    extras["error_code"] = "approval_expired"
    extras["decision"] = "expired"
    annotated["extras"] = extras
    return annotated


def _build_already_resumed_envelope(
    *,
    original_envelope: dict[str, Any],
    approval_request_id: uuid.UUID,
) -> dict[str, Any]:
    """Annotate the envelope when an operator surface already executed the op.

    Exactly-one-resumer claim (#2293): the request was approved, but the
    operator approval surface that granted it won the claim and
    re-dispatched the op, so this in-process waiter lost the race and must
    not dispatch again. Rewrites ``status`` to ``"already_resumed"`` (a
    benign terminal, not an error), stamps
    ``extras["error_code"] = "already_resumed"`` + ``extras["decision"] =
    "approved"`` so the agent's model has a stable string to switch on, and
    names the request in ``error``. The durable execution audit row written
    by the winning resumer is the evidence the op ran; this envelope is the
    model-side view telling the agent it need not (and must not) retry.
    """
    annotated = dict(original_envelope)
    annotated["status"] = "already_resumed"
    annotated["error"] = (
        f"approval request {approval_request_id} was approved and executed by "
        "an operator approval surface; not re-dispatched here."
    )
    extras = dict(original_envelope.get("extras") or {})
    extras["error_code"] = "already_resumed"
    extras["decision"] = "approved"
    annotated["extras"] = extras
    return annotated


async def _resume_approved_or_already_resumed(
    *,
    operator: Operator,
    call_arguments: dict[str, Any],
    awaiting_envelope: dict[str, Any],
    approval_request_id: uuid.UUID,
) -> dict[str, Any]:
    """Re-dispatch the approved op, or no-op if an operator surface beat us.

    Exactly-one-resumer claim (#2293): the operator surface that published
    this ``approval.approved`` event *also* re-dispatches the approved op
    via ``resume_dispatch_after_approval``. Both this in-process waiter and
    that surface must win the same atomic :func:`claim_resume` before
    dispatching, so the approved (possibly write) op executes exactly once.
    Win it here before the in-memory re-dispatch; a lost claim means an
    operator surface already executed it, so surface a clean
    already-executed envelope to the agent rather than double-dispatching.

    Audit lineage (#2323, completing #2086): the executed dispatch row must
    parent-link to the ``approval.request`` row so an approved chain replays
    as one subtree (request → decision + executed dispatch), not two roots.
    The operator resume path binds ``parent_audit_id`` off the parked row in
    :func:`~meho_backplane.operations.approval_queue._dispatch_resume_with_bound_context`;
    this in-process waiter re-dispatches on the *agent's own task*, where
    ``parent_audit_id_var`` still carries the original top-level value
    (``None`` for a plain agent tool call) — so without the bind below the
    resumed dispatch would emit ``parent_audit_id = NULL`` and orphan as a
    second audit root. ``agent_session_id_var`` is already bound on the
    agent task (the requester's run), so the session anchor needs no
    re-bind; only the parent link does.

    Composite sub-op park (#3421, the agent side of #3351): when the parked
    row carries ``resume_parent``, the parked ``op_id`` is a composite's
    governed sub-op, not a call the agent can re-run as is. The waiter then
    re-enters the **parent composite** ``_approved=True`` with the approved
    sub-op cleared, through the same
    :func:`~meho_backplane.operations.approval_queue.composite_resume_binding`
    the operator surface uses, so both surfaces resume the same row the same
    way. Without it the plain re-dispatch of the agent's call reached the
    sub-op gate again and parked a new request, so nothing executed. The
    re-entry runs as the agent principal (the operator surface runs as the
    reviewer); any other governed sub-op is checked against the agent's own
    grants, and one that would need a second approval fails closed
    (``composite_resume_multi_gate_unsupported``). The re-entry dispatches
    against the target the row saved by id
    (:func:`~meho_backplane.operations.approval_queue.rehydrate_resume_target`,
    the operator surface's check), never the name in *call_arguments*: a
    target renamed, or deleted and re-created under the same name, while the
    request waited must not receive the approved write. A saved target that
    no longer resolves fails closed with a ``denied`` envelope before the
    claim. A direct park (``resume_parent IS NULL``) keeps the re-dispatch of
    the agent's original *call_arguments*.

    Local imports mirror each other (and the wider module convention):
    ``meta_tools`` imports from ``operations.dispatcher`` which imports back
    through ``meho_backplane.agent.invoke``, so a module-level import here
    would set up an import-time cycle; keeping both inline resolves them
    once at first-call time.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations._audit import parent_audit_id_var
    from meho_backplane.operations.approval_handoff import consume as consume_handoff
    from meho_backplane.operations.approval_handoff import discard as discard_handoff
    from meho_backplane.operations.approval_queue import (
        claim_resume,
        composite_resume_binding,
        get_request,
        rehydrate_resume_target,
    )
    from meho_backplane.operations.meta_tools import call_operation_with_approval

    # Load the parked row (tenant-isolated). Its pre-generated
    # ``approval.request`` audit id is bound below so the resumed dispatch
    # back-links to it; pre-0053 rows carry ``request_audit_id IS NULL`` →
    # binds ``None`` (the var's default), the correct no-op for a chain that
    # never had the anchor. Its ``resume_parent`` (#3421) says whether this is
    # a composite sub-op park that must re-enter the parent composite.
    async with get_sessionmaker()() as session:
        request = await get_request(
            session,
            tenant_id=operator.tenant_id,
            request_id=approval_request_id,
        )

    # A composite re-entry dispatches against the target the row saved by id,
    # the same check the operator surface runs, not the name the agent used
    # when it parked: that name may point at another target by now. A saved
    # target that no longer resolves fails closed here, before the claim, so
    # the refused resume does not burn the one-resumer claim.
    resolved_target: Any = None
    if request.resume_parent is not None:
        resolved_target, denied = await rehydrate_resume_target(operator, request)
        if denied is not None:
            return cast("dict[str, Any]", denied.model_dump(mode="json"))

    if not await claim_resume(approval_request_id):
        _log.info(
            "approval_wait_resume_already_claimed",
            approval_request_id=str(approval_request_id),
            tenant_id=str(operator.tenant_id),
            op_id=awaiting_envelope.get("op_id"),
        )
        return _build_already_resumed_envelope(
            original_envelope=awaiting_envelope,
            approval_request_id=approval_request_id,
        )

    # Only the claim winner touches the credential-write custody row. A direct
    # park discards it: the agent already owns the original inputs in memory.
    # A composite sub-op park whose row only names the parent consumes it,
    # because the parent's params live only in the encrypted copy. The
    # decrypted parent stays in a local and is never written to the ORM row.
    async with get_sessionmaker()() as session:
        resume_parent = request.resume_parent
        if resume_parent is not None and request.execution_handle is not None:
            handoff = await consume_handoff(
                session, request.execution_handle, request.id, request.tenant_id
            )
            resume_parent = cast("dict[str, Any] | None", handoff.get("resume_parent"))
        else:
            await discard_handoff(session, approval_request_id, operator.tenant_id)
        await session.commit()

    # The re-dispatch threads ``_approved=True`` through the policy gate, so
    # the durable approval-decision row is the authorization.
    parent_token = parent_audit_id_var.set(request.request_audit_id)
    try:
        # #3421: same composite re-entry as the operator surface. A composite
        # sub-op park re-enters its parent composite with the approved sub-op
        # cleared; a direct park (``resume_parent IS NULL``) re-dispatches the
        # agent's original call unchanged.
        with composite_resume_binding(
            subop_op_id=request.op_id,
            subop_params_hash=request.params_hash,
            resume_parent=resume_parent,
        ) as parent_call:
            if parent_call is None:
                return await call_operation_with_approval(operator, call_arguments)
            parent_op_id, parent_params = parent_call
            _log.info(
                "approval_wait_resume_composite_parent",
                approval_request_id=str(approval_request_id),
                tenant_id=str(operator.tenant_id),
                subop_op_id=request.op_id,
                parent_op_id=parent_op_id,
                target_id=str(request.target_id) if request.target_id else None,
            )
            # ``target`` is cleared so the stale name is never looked up; the
            # saved target (or ``None`` for a tenant-wide op) is passed as is.
            return await call_operation_with_approval(
                operator,
                {
                    **call_arguments,
                    "connector_id": request.connector_id,
                    "op_id": parent_op_id,
                    "params": parent_params,
                    "target": None,
                },
                resolved_target=resolved_target,
            )
    finally:
        parent_audit_id_var.reset(parent_token)


async def resume_or_surface_awaiting_approval(
    *,
    operator: Operator,
    call_arguments: dict[str, Any],
    awaiting_envelope: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for an awaited approval decision and re-dispatch on approval.

    The single re-entry point the wrapped ``call_operation`` tool calls when
    a dispatch returned ``status="awaiting_approval"``. Reads the
    ``approval_request_id`` off the envelope, blocks on
    :func:`wait_for_approval_decision`, and:

    * **approved** — re-invokes the dispatcher with ``_approved=True`` and the
      original in-memory ``params`` from *call_arguments*, returns the new
      envelope (the executed op's result, with the correct audit attribution
      threaded by the dispatcher). A parked composite sub-op instead
      re-enters its parent composite with that sub-op cleared (#3421).
    * **rejected** — returns the original ``awaiting_approval`` envelope
      annotated with a rejection error code, so the model sees a structured
      tool result.
    * **expired** — returns the envelope annotated with the
      ``approval_expired`` error code. Nothing is dispatched.
    * **timeout** — returns the envelope annotated with the
      :data:`AWAITING_APPROVAL_TIMEOUT_ERROR_CODE` so the agent can reason
      about it (try later / abort).

    Args:
        operator: The agent's :class:`~meho_backplane.auth.operator.Operator`.
            The dispatcher records the resumed dispatch under this principal,
            so the audit chain stays correct (subject = the agent principal
            that made the original call; the approval row holds the human
            reviewer's identity as the decision evidence).
        call_arguments: The original ``call_operation`` ``arguments`` dict
            (``connector_id`` / ``op_id`` / ``target`` / ``params``). The
            *params* are the load-bearing piece — the approval row stores
            only a hash for the swap-defence check; the agent's in-memory
            params are the authoritative source for the re-dispatch.
        awaiting_envelope: The dispatcher's ``OperationResult`` already
            serialised to a dict via ``model_dump(mode="json")``. Its
            ``extras["approval_request_id"]`` is the request id the wait
            keys on. Returned annotated on rejection / timeout.
        timeout_seconds: Overall wait cap. Caller pulls it from
            ``Settings.agent_approval_wait_timeout_seconds`` (tests pin a
            sub-second value).

    Returns:
        The envelope the wrapped ``call_operation`` returns to the model:
        the dispatcher's envelope on approval (it, not the approval, says
        whether the op succeeded, failed or is uncertain), the rejection- or
        expiry-annotated envelope, or the timeout-annotated envelope when no
        decision was seen before the deadline.

    Raises:
        ValueError: ``extras["approval_request_id"]`` is missing or not a
            valid UUID — a dispatcher contract violation; the wait would be
            un-resumable, so we fail loud rather than burn the timeout.
    """
    approval_request_id = _extract_approval_request_id(awaiting_envelope)

    decision = await wait_for_approval_decision(
        tenant_id=operator.tenant_id,
        approval_request_id=approval_request_id,
        timeout_seconds=timeout_seconds,
    )

    if decision == "approved":
        return await _resume_approved_or_already_resumed(
            operator=operator,
            call_arguments=call_arguments,
            awaiting_envelope=awaiting_envelope,
            approval_request_id=approval_request_id,
        )

    if decision in ("rejected", "expired"):
        _log.info(
            f"approval_wait_{decision}_surfaced_to_agent",
            approval_request_id=str(approval_request_id),
            tenant_id=str(operator.tenant_id),
            op_id=awaiting_envelope.get("op_id"),
        )
        build = _build_rejected_envelope if decision == "rejected" else _build_expired_envelope
        return build(original_envelope=awaiting_envelope, approval_request_id=approval_request_id)

    # decision == "timeout"
    return _build_awaiting_approval_timeout_envelope(
        original_envelope=awaiting_envelope,
        approval_request_id=approval_request_id,
        timeout_seconds=timeout_seconds,
    )
