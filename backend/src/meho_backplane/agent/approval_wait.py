# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-runtime resume substrate: wait for an approval decision (G11.1-T9 / #1117).

This module is the agent-side half of the **operator/agent split** that the
G11.2 approval substrate established:

* The operator decides — via REST ``/decide``, MCP ``meho.approvals.{approve,reject}``,
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
``meho.broadcast.watch`` tool read from) and blocks until it sees the
``approval.{approved,rejected}`` event for *this* request, or the timeout
elapses. The caller (the agent's wrapped ``call_operation`` tool) then either
re-dispatches with ``_approved=True`` (approved), reports the rejection back
to the model (rejected), or surfaces a timeout as a structured tool result the
agent can reason about.

Why broadcast and not DB polling
================================

The acceptance criteria explicitly require the agent runtime to "subscribe (via
the existing broadcast SSE/watch substrate)" — broadcast already exists, runs
sub-second latency (vs. a polling interval), and is the same channel the UI
wall-monitor consumes, so the agent learns of decisions in lockstep with human
operators. The substrate is fail-open at the publish side (a broadcast outage
never blocks the durable decision), so the agent-side wait must tolerate a
missed event — it does, via the timeout: if the broadcast happens to drop the
event the agent run fails the wait cleanly with a structured ``"timeout"``
return; the operator can re-issue or the run can be cancelled.

Why ``XREAD BLOCK`` and not the high-level ``watch`` tool
=========================================================

The MCP ``meho.broadcast.watch`` tool wraps ``XREAD BLOCK`` plus filtering,
but it's bounded by ``_WATCH_MAX_TIMEOUT_MS`` (30s) — a sensible cap for
an MCP-call latency budget, the wrong shape for a human approval which can
take minutes to hours. This helper drives ``XREAD BLOCK`` directly with the
agent-runtime's own timeout (default 30 min, settings-driven), looping over
small block windows so cancellation (operator cancels the agent run) lands
promptly.

Cursor discipline
=================

``XREAD`` reads entries strictly past the cursor it's given (last-id-seen
exclusive). The wait starts the cursor at "now" — the ``approval.pending``
event was published *before* the agent's wrapped ``call_operation`` returned
the ``awaiting_approval`` envelope, so a cursor at "now" cannot miss the
subsequent decision. Concretely we pass ``"$"`` to ``XREAD`` on the first
read (the standard "tail from this moment forward" cursor); subsequent reads
in the loop advance to the most recent entry id seen so a busy tenant doesn't
re-emit already-considered entries.

Fail-open semantics
===================

Every recoverable broadcast failure (Valkey unreachable, malformed entry,
parse error) returns ``"timeout"`` rather than raising. The wrapper caller
maps that to a structured awaiting-approval-timeout tool result that lets
the agent reason about the failure (try again later / abort) instead of
crashing the loop. The audit row + decision row remain the canonical record
of what the operator actually decided; the broadcast is only the real-time
view.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import structlog
from redis.exceptions import RedisError

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
#: ``approved`` / ``rejected`` mirror the durable decision states; ``timeout``
#: covers the timeout cap and every recoverable broadcast failure (Valkey
#: unreachable, malformed entries) so the caller has a single non-decision
#: branch to handle.
ApprovalDecision = Literal["approved", "rejected", "timeout"]

#: Per-iteration ``XREAD BLOCK`` window inside the wait loop. Smaller than
#: the overall timeout so an ``asyncio.CancelledError`` (operator cancels the
#: run, the agent loop tears down) lands within this window rather than at
#: the end of the full multi-minute timeout. 5s balances "responsive to
#: cancellation" against "syscall overhead from re-issuing ``XREAD`` too
#: often"; mirrors the ``meho.broadcast.watch`` per-call default with the
#: orders-of-magnitude longer overall wait wrapped around it.
_BLOCK_WINDOW_MS: Final[int] = 5_000

#: Upper bound on entries returned per ``XREAD`` invocation. The wait only
#: cares about the single matching ``approval.{approved,rejected}`` entry,
#: but a busy tenant may publish many unrelated events between iterations;
#: 100 keeps each batch bounded so the filter loop stays fast.
_XREAD_COUNT: Final[int] = 100

#: Op-id prefix every decision broadcast event carries. The publisher in
#: :func:`~meho_backplane.operations.approval_queue.publish_approval_event`
#: builds ``f"approval.{decision}"`` for ``decision`` in
#: ``{"pending", "approved", "rejected", "expired"}``; we filter for the two
#: terminal decisions on this request id only.
_APPROVED_OP_ID: Final[str] = "approval.approved"
_REJECTED_OP_ID: Final[str] = "approval.rejected"


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
    """Return ``"approved"``/``"rejected"`` if *fields* is THIS request's
    terminal decision, ``None`` otherwise.

    Each stream entry carries a single ``event`` field whose value is the
    JSON-serialised :class:`~meho_backplane.broadcast.events.BroadcastEvent`.
    We parse it, gate on (a) ``op_id`` being one of the two terminal decisions
    and (b) ``payload.approval_request_id`` matching the request the agent is
    waiting on. Anything else (other approvals' decisions, ``pending`` /
    ``expired`` for this request, unrelated ops) is filtered out and the wait
    keeps looping.

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
    op_id = payload.get("op_id")
    if op_id not in (_APPROVED_OP_ID, _REJECTED_OP_ID):
        return None
    event_payload = payload.get("payload") or {}
    if event_payload.get("approval_request_id") != target_request_id:
        return None
    return "approved" if op_id == _APPROVED_OP_ID else "rejected"


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
    remaining: float,
) -> tuple[ApprovalDecision | None, str]:
    """Run one ``XREAD BLOCK`` iteration; return (decision-or-None, new-cursor).

    Three outcomes the caller folds into the surrounding while-loop:

    * ``("approved" | "rejected", _)`` — terminal decision observed; the loop
      returns.
    * ``(None, advanced_cursor)`` — entries arrived but none matched this
      request; the loop continues with the advanced cursor (so a busy
      tenant doesn't re-process the same entries).
    * ``(None, cursor)`` — BLOCK timed out empty *or* a recoverable broadcast
      error fired; the loop re-issues with the same cursor.

    A :class:`~redis.exceptions.RedisError` (Valkey unreachable, malformed
    response) or :class:`OSError` (socket-layer hiccup) is logged + folded
    into the "loop again" branch with a small back-off sleep — the same
    fail-open posture the publisher uses at write time.
    :class:`asyncio.CancelledError` propagates verbatim so the agent loop
    tears down cleanly when the run is cancelled.
    """
    try:
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
    except (RedisError, OSError) as exc:
        _log.warning(
            "approval_wait_xread_error",
            approval_request_id=target_request_id,
            tenant_id=str(tenant_id),
            error_class=type(exc).__name__,
        )
        await asyncio.sleep(min(1.0, max(0.0, remaining)))
        return (None, cursor)

    items = _xread_items(raw_response)
    if not items:
        return (None, cursor)
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
            return (decision, new_cursor)
    return (None, new_cursor)


async def wait_for_approval_decision(
    *,
    tenant_id: uuid.UUID,
    approval_request_id: uuid.UUID,
    timeout_seconds: float,
) -> ApprovalDecision:
    """Block until the broadcast feed reports a terminal decision, or time out.

    Drives ``XREAD BLOCK`` against ``meho:feed:{tenant_id}`` with a short
    per-call window (:data:`_BLOCK_WINDOW_MS`), looping until either an
    ``approval.{approved,rejected}`` event for *approval_request_id* arrives
    or the cumulative wait reaches *timeout_seconds*. Returns one of the three
    :data:`ApprovalDecision` literals; the caller's wrapped ``call_operation``
    branches on the return.

    Per-iteration mechanics are extracted to :func:`_xread_one_iteration`;
    this function owns the deadline / cursor-bootstrap / outer loop.

    Args:
        tenant_id: The operator's tenant; selects the per-tenant Valkey stream
            to read from. The dispatcher recorded the request under this
            tenant; cross-tenant decisions are structurally impossible because
            the stream key is tenant-scoped.
        approval_request_id: The ``approval_request.id`` the agent is awaiting,
            lifted from the ``call_operation`` envelope's
            ``extras["approval_request_id"]``.
        timeout_seconds: Overall wait cap. Default policy lives in the caller
            (``Settings.agent_approval_wait_timeout_seconds``); we take the
            number here so a test can pin a sub-second value without monkey-
            patching settings.

    Returns:
        - ``"approved"`` — the operator approved; the caller should re-dispatch
          with ``_approved=True``.
        - ``"rejected"`` — the operator rejected; the caller surfaces the
          rejection to the model.
        - ``"timeout"`` — either the wall-clock cap elapsed without a decision
          event arriving, or every ``XREAD`` invocation in this wait raised a
          broadcast-substrate error. The caller surfaces this as an
          ``awaiting_approval_timeout`` tool result so the agent can reason
          about it (no decision has been observed; the durable decision row
          remains the source of truth and may exist regardless).

    Notes:
        ``asyncio.CancelledError`` from a parent cancellation (operator cancels
        the run mid-wait) propagates verbatim — the wait is one part of the
        agent's cooperative-cancellation tree.
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
    # ``"$"`` is the Valkey "tail from the moment of this call" cursor. The
    # ``approval.pending`` event for this request was published BEFORE the
    # agent's wrapped ``call_operation`` returned and we entered this wait, so
    # ``"$"`` cannot miss the subsequent terminal decision event. Subsequent
    # iterations advance the cursor to the latest entry id seen so a busy
    # tenant doesn't re-emit already-considered entries.
    cursor: str = "$"
    _log.info(
        "approval_wait_started",
        approval_request_id=target_request_id,
        tenant_id=str(tenant_id),
        timeout_seconds=timeout_seconds,
    )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _log.info(
                "approval_wait_timeout",
                approval_request_id=target_request_id,
                tenant_id=str(tenant_id),
            )
            return "timeout"
        # Cap each iteration's BLOCK at the smaller of the per-iteration
        # window and the remaining wall-clock; the integer cast guards against
        # a fractional-millisecond sub-1ms tail at the very end of the budget.
        block_ms = max(1, min(_BLOCK_WINDOW_MS, int(remaining * 1000)))
        decision, cursor = await _xread_one_iteration(
            client,
            stream_key=stream_key,
            cursor=cursor,
            block_ms=block_ms,
            target_request_id=target_request_id,
            tenant_id=tenant_id,
            remaining=remaining,
        )
        if decision is not None:
            return decision


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
    a wait-context string. The audit row is untouched — broadcast outage or a
    pre-deadline forgotten request both surface here, but neither is a durable
    decision; the operator's actual decision row (if one exists) remains the
    canonical record.
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
      threaded by the dispatcher).
    * **rejected** — returns the original ``awaiting_approval`` envelope
      annotated with a rejection error code, so the model sees a structured
      tool result.
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
        a fresh dispatch result on approval, the rejection-annotated
        envelope on rejection, or the timeout-annotated envelope on
        timeout / broadcast failure.

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
        # Local import — `meta_tools` imports from `operations.dispatcher`
        # which imports back through ``meho_backplane.agent.invoke`` for
        # the contextvar lookup. A module-level import here would set up
        # a circular reference at import time; keeping it inline resolves
        # it once at first-call time. The re-dispatch threads
        # ``_approved=True`` through the policy gate, so the durable
        # approval-decision row is the authorization.
        from meho_backplane.operations.meta_tools import (
            call_operation_with_approval,
        )

        return await call_operation_with_approval(operator, call_arguments)

    if decision == "rejected":
        _log.info(
            "approval_wait_rejected_surfaced_to_agent",
            approval_request_id=str(approval_request_id),
            tenant_id=str(operator.tenant_id),
            op_id=awaiting_envelope.get("op_id"),
        )
        return _build_rejected_envelope(
            original_envelope=awaiting_envelope,
            approval_request_id=approval_request_id,
        )

    # decision == "timeout"
    return _build_awaiting_approval_timeout_envelope(
        original_envelope=awaiting_envelope,
        approval_request_id=approval_request_id,
        timeout_seconds=timeout_seconds,
    )
