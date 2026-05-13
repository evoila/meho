# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/feed`` — Server-Sent Events feed for broadcast events (G6.1-T4).

Per-tenant SSE endpoint that streams :class:`BroadcastEvent` records
``XADD``\\ ed by T3's publish-on-write hook onto
``meho:feed:{tenant_id}``. Subscribers — `meho status --watch` (T5,
#311), the future Slack mirror (G6.2 #333), and any third-party
operator dashboard — open one HTTP connection per session and
receive events as soon as they're published.

Transport
=========

Standard SSE per the WHATWG ``EventSource`` spec. Each event is
emitted as::

    event: broadcast
    data: <model_dump_json of BroadcastEvent>
    id: <Valkey stream entry id, e.g. 1715600000000-0>

Clients reconnecting with ``Last-Event-Id: <id>`` receive replay from
that point. The ``id:`` field carries the Valkey entry id verbatim so
the reconnect handshake is server-stateless — the Valkey stream IS the
session state.

Filtering
=========

Three optional query parameters apply **after** the XREAD pull:

* ``op_class`` — exact-match filter on
  :attr:`BroadcastEvent.op_class` (e.g. ``read``, ``write``,
  ``credential_read``).
* ``principal`` — exact-match filter on
  :attr:`BroadcastEvent.principal_sub` (the JWT ``sub`` claim, not the
  human ``name``).
* ``target`` — exact-match filter on
  :attr:`BroadcastEvent.target_name`.

All three default to "no filter" — an unfiltered feed yields every
event the operator's tenant produces.

Replay
======

Either of the two replay knobs may be set; ``Last-Event-Id`` header
takes precedence over the ``since`` query parameter when both are
present. The default cursor is ``$`` (Valkey's "from now" anchor), so
a fresh connection yields only events ``XADD``\\ ed after the SSE
handshake completes.

Tenant scoping
==============

The stream key is ``meho:feed:{operator.tenant_id}`` derived from the
validated JWT; the client cannot subscribe to a different tenant's
stream by passing a tenant_id in the query string — there is no such
parameter. RBAC requires ``operator`` role minimum (read_only → 403);
the rationale matches the retrieval route's gate: read-only operators
have lower-friction surfaces (``meho status``, kb search) that don't
need real-time feed.

Heartbeat
=========

The generator emits ``: heartbeat\\n\\n`` (SSE comment line) every
``_HEARTBEAT_INTERVAL_SECONDS`` of **outbound silence** (no event
frame yielded to the subscriber) to keep intermediaries from
idle-timing-out the connection. Two scenarios trigger the heartbeat:

1. **Valkey-quiet** — ``XREAD BLOCK`` returns no entries within the
   block window. The natural quiet-time signal Valkey gives the loop.
2. **Subscriber-filtered** — ``XREAD BLOCK`` returns entries but they
   all fail the operator's ``op_class`` / ``principal`` / ``target``
   filter, so the generator yields nothing outbound. Without this
   second path a noisy tenant with a narrow-filtered subscriber
   would emit zero outbound bytes (events filtered, no heartbeat
   either) and the nginx / ALB / CloudFront ~60 s idle timeout would
   drop the connection.

``last_heartbeat`` tracks the wall-clock of the last outbound yield
(event frame or heartbeat); inbound XREAD activity that doesn't
produce an outbound frame does NOT reset it.

Disconnect handling
===================

Client disconnect propagates as :class:`asyncio.CancelledError` into
the generator (Starlette cancels the request task on
``http.disconnect``). The ``except`` arm logs the structured
``feed_subscriber_disconnected`` event for operator triage, then
re-raises so the cancellation unwinds the task tree per asyncio's
contract (Sonar S7497; Python 3.13+ asyncio re-issues cancellation
if it goes unpropagated). The audit row at session end still records
a clean 200 close because ``http.response.start`` was sent on the
first yield — before any cancellation point — so
:class:`~meho_backplane.audit.AuditMiddleware`'s buffered
``status_code`` is already locked at 200 by the time the cancellation
propagates. Valkey's BLOCKing XREAD releases the connection from the
pool the next event-loop tick.

References
----------

* SSE / EventSource: https://html.spec.whatwg.org/multipage/server-sent-events.html
* Valkey ``XREAD``: https://valkey.io/commands/xread/
* FastAPI ``StreamingResponse``:
  https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Iterator
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.broadcast import BroadcastEvent, get_broadcast_client

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["feed"])

_log = structlog.get_logger(__name__)

#: Operator-minimum gate, factored to module scope so B008 (the
#: "no function call in default" lint rule) doesn't trip on
#: ``Depends(require_role(...))`` inside the handler's argument list.
#: Mirrors the in-repo pattern in :mod:`meho_backplane.api.v1.retrieve`.
_REQUIRE_OPERATOR = Depends(require_role(TenantRole.OPERATOR))


#: XREAD ``BLOCK`` window in milliseconds. The generator yields a
#: heartbeat once per quiet window of this length; the value also
#: bounds how long a client-disconnect-cancel takes to land in the
#: generator (the BLOCK call has to time out or return entries before
#: the surrounding loop can observe the cancelled context).
#:
#: 30 seconds matches the issue body's spec and the common SSE
#: intermediary keep-alive window (nginx default proxy_read_timeout
#: is 60s; AWS ALB idle timeout 60s).
_XREAD_BLOCK_MS: Final[int] = 30_000

#: Wall-clock idle interval after which the generator emits a
#: heartbeat. Same value as the XREAD block window so a quiet period
#: produces exactly one heartbeat per cycle — bypasses the "BLOCK 30s
#: returned empty, did the connection drop?" ambiguity for both the
#: client and any HTTP intermediaries.
_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 30.0

#: Upper bound on entries pulled per XREAD call. Bounded to keep the
#: generator's tick latency-aware: a burst of N events still trickles
#: out to the client in chunks of up to this size rather than blocking
#: the event-loop tick on a 1000-event ``model_validate_json`` sweep.
_XREAD_COUNT: Final[int] = 20

#: Initial cursor when the client doesn't provide one. Valkey's ``$``
#: anchor means "only entries XADD'd after this XREAD call started" —
#: a fresh connection without ``Last-Event-Id`` / ``since`` gets the
#: live tail, not a backlog.
_LIVE_TAIL_CURSOR: Final[str] = "$"

#: Valkey stream entry id shape — ``<ms-timestamp>`` or
#: ``<ms-timestamp>-<sequence>``. Accepts both forms because the
#: bare-timestamp form is legal (Valkey auto-assigns sequence 0) even
#: though XADD-emitted ids always include the sequence suffix.
#: Used by :func:`_validate_cursor_or_400` at the route boundary to
#: reject malformed ``Last-Event-Id`` / ``since`` values before they
#: reach XREAD and trigger an SSE reconnect loop.
_VALKEY_STREAM_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+(?:-\d+)?$")


def _validate_cursor_or_400(cursor: str) -> str:
    """Validate the SSE replay cursor; raise HTTP 400 on bad input.

    Cursor sources are operator-controlled (``Last-Event-Id`` header,
    ``since`` query parameter). Without this gate, a malformed cursor
    propagates verbatim into the ``XREAD`` call and Valkey rejects it
    with a ``redis.ResponseError`` mid-stream — the SSE response was
    already sent ``http.response.start``, so the failure surfaces as a
    connection drop. The browser ``EventSource`` auto-reconnects per
    the WHATWG spec with the SAME bad cursor and tightens into a
    reconnect loop.

    Returning HTTP 400 at the route boundary (before any streaming)
    flips the SSE state machine to ``readyState=CLOSED`` (the spec
    aborts auto-reconnect on 4xx-class responses), giving the client
    a recoverable error rather than a hot-loop.

    Accepts:

    * ``"$"`` — Valkey's "live tail" anchor.
    * ``"<int>"`` or ``"<int>-<int>"`` — Valkey stream id forms.

    Rejects everything else with ``HTTPException(400)``. The detail
    string deliberately doesn't echo the input — operator-controlled
    cursors could carry log-injection payloads if reflected verbatim
    into structured log shippers.
    """
    if cursor == _LIVE_TAIL_CURSOR or _VALKEY_STREAM_ID_RE.fullmatch(cursor):
        return cursor
    raise HTTPException(
        status_code=400,
        detail="invalid_cursor: expected Valkey stream id (e.g. '1715600000000-0') or '$'",
    )


def _stream_key(operator: Operator) -> str:
    """Build the per-tenant stream key from the JWT-derived tenant id.

    Centralised here so a future tenancy-isolation tightening (e.g. a
    per-environment prefix) lands in one place. Mirrors the same
    helper in :mod:`meho_backplane.broadcast.publisher`.
    """
    return f"meho:feed:{operator.tenant_id}"


def _format_event(entry_id: str, raw_event_json: str) -> str:
    """Format one Valkey entry as an SSE ``event: broadcast`` frame.

    The ``data:`` field carries the JSON verbatim — no
    pretty-printing or re-serialisation. Subscribers parse this back
    into a :class:`BroadcastEvent` on their side, so a re-dump here
    would only churn newlines (and SSE forbids embedded newlines in
    ``data:`` fields unless explicitly split into multiple ``data:``
    lines — easier to enforce by passing the canonical single-line
    JSON straight through from
    :meth:`BroadcastEvent.model_dump_json`).

    The ``id:`` line is the Valkey stream entry id verbatim;
    clients use it as the next ``Last-Event-Id`` value.
    """
    return f"event: broadcast\ndata: {raw_event_json}\nid: {entry_id}\n\n"


def _passes_filter(
    event: BroadcastEvent,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> bool:
    """Return ``True`` iff the event matches every non-None filter.

    None means "no filter on this field". Exact-match semantics — no
    substring or pattern matching today; G6.3 may revisit if operator
    feedback flags the gap. ``target_name`` is nullable on
    :class:`BroadcastEvent`; an event with ``target_name=None`` and
    a non-None *target* filter never passes (the operator asked for
    a specific target; an event with no target attribution doesn't
    qualify).
    """
    if op_class is not None and event.op_class != op_class:
        return False
    if principal is not None and event.principal_sub != principal:
        return False
    return not (target is not None and event.target_name != target)


def _resolve_cursor(
    last_event_id_header: str | None,
    since: str | None,
) -> str:
    """Pick the XREAD cursor per the ``Last-Event-Id`` > ``since`` > ``$`` order.

    Per the SSE spec, ``Last-Event-Id`` is the canonical reconnect
    mechanism — clients automatically resend it after a connection
    drop. The ``since`` query parameter exists for callers that need
    explicit replay control without relying on the SSE auto-reconnect
    machinery (e.g. server-side bridges that consume the feed via
    plain HTTP). When both are present the header wins because it
    encodes "I'm continuing a session I already had"; the query
    parameter is the explicit override.
    """
    if last_event_id_header:
        return last_event_id_header
    if since:
        return since
    return _LIVE_TAIL_CURSOR


def _process_entries(
    items: list[tuple[str, dict[str, str]]],
    *,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    stream_key: str,
) -> Iterator[tuple[str, str]]:
    """Yield ``(entry_id, sse_frame)`` for every entry that passes the filter.

    Handles three skip paths inline:

    * Unknown field shape (entry XADD'd without an ``event`` field) —
      log + skip. T3's publisher is currently the only writer; this
      branch is the safety net against a future Slack-mirror /
      downstream tool writing alternate field shapes onto the same
      stream key.
    * Malformed JSON in the ``event`` field — log + skip rather than
      tearing the subscriber down. A T3 bug or stream-key collision
      with a foreign writer surfaces here as a logged warning, not a
      500.
    * Filter rejection — silently drop (the operator's filter is
      working as intended).

    Lifted out of :func:`_feed_generator` so the main loop's
    cognitive complexity stays under the SonarCloud S3776 ceiling.
    The helper itself is a single ``for``-loop with three
    ``continue`` arms.
    """
    for entry_id, fields in items:
        raw_event_json = fields.get("event")
        if not isinstance(raw_event_json, str):
            _log.warning(
                "feed_skipped_unknown_field_shape",
                stream_key=stream_key,
                entry_id=entry_id,
                fields=list(fields.keys()),
            )
            continue
        try:
            event = BroadcastEvent.model_validate_json(raw_event_json)
        except ValidationError:
            _log.warning(
                "feed_skipped_malformed_event",
                stream_key=stream_key,
                entry_id=entry_id,
            )
            continue
        if not _passes_filter(event, op_class, principal, target):
            continue
        yield entry_id, _format_event(entry_id, raw_event_json)


async def _feed_generator(
    operator: Operator,
    cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> AsyncIterator[str]:
    """SSE generator: BLOCK on XREAD, delegate parsing, heartbeat-on-silence.

    Heartbeat semantics — ``last_heartbeat`` tracks the wall-clock of
    the **last outbound yield** (event frame or heartbeat), NOT the
    last inbound XREAD result. A noisy tenant where every event is
    filtered out for this subscriber still produces zero outbound
    bytes; without this guarantee the connection would idle-timeout
    at the nginx / ALB / CloudFront layer. The "all entries filtered
    out" path therefore emits an inline heartbeat when the idle
    window has elapsed — both quiet and busy-but-filtered tenants
    keep the connection alive.

    On client disconnect Starlette raises
    :class:`asyncio.CancelledError` into the pending ``xread`` await;
    the handler logs and re-raises per the asyncio cancellation
    contract (Sonar S7497 — swallowing CancelledError breaks the task
    tree's unwind invariants and Python 3.13+ asyncio internals re-
    issue cancellation when it goes unpropagated). The audit row at
    session end still records a clean 200 close because
    ``http.response.start`` was sent on the first yield (before any
    cancellation point), so AuditMiddleware's buffered status_code
    is already locked at 200.
    """
    client = get_broadcast_client()
    stream_key = _stream_key(operator)
    last_heartbeat = time.monotonic()

    try:
        while True:
            entries = await client.xread(
                {stream_key: cursor},
                block=_XREAD_BLOCK_MS,
                count=_XREAD_COUNT,
            )
            now = time.monotonic()
            emitted_any = False
            if entries:
                # redis-py returns ``[[stream_key, [(entry_id, fields), ...]], ...]``
                # — one outer tuple per stream queried. We always
                # query exactly one stream (the operator's tenant
                # feed), so ``entries[0][1]`` is the list of
                # ``(entry_id, fields_dict)`` tuples.
                _key, items = entries[0]
                if items:
                    # Advance the cursor past EVERY consumed entry,
                    # not only the ones that survive the filter. The
                    # M2 refactor moved skip-paths (filter mismatch,
                    # malformed JSON, unknown field shape) inside
                    # ``_process_entries``; the helper consumes those
                    # entries without yielding, so a ``cursor =
                    # entry_id`` placed inside the post-helper
                    # ``for ... yield`` loop never advances past them.
                    # Under explicit-cursor replay (``since=<id>`` or
                    # ``Last-Event-Id``) a tenant where every entry is
                    # filtered out would otherwise re-read the same
                    # batch forever: XREAD with cursor=id_N returns
                    # IDs > id_N, helper drops them, cursor stays at
                    # id_N, next XREAD returns the same set. Set the
                    # cursor here, BEFORE the yield loop, so the next
                    # XREAD reads strictly past this batch regardless
                    # of whether anything yielded out the other side.
                    cursor = items[-1][0]
                for _entry_id, frame in _process_entries(
                    items,
                    op_class=op_class,
                    principal=principal,
                    target=target,
                    stream_key=stream_key,
                ):
                    yield frame
                    emitted_any = True
            if emitted_any:
                last_heartbeat = now
            elif now - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now
    except asyncio.CancelledError:
        # Client disconnect. Log the structured event for operator
        # triage, then re-raise so the task tree unwinds per asyncio's
        # cancellation contract — Sonar S7497, Python 3.13+ asyncio
        # internals (re-issue cancellation if it goes unpropagated).
        _log.info(
            "feed_subscriber_disconnected",
            stream_key=stream_key,
            operator_sub=operator.sub,
        )
        raise


@router.get("/feed")
async def feed_endpoint(
    request: Request,
    op_class: str | None = Query(default=None, description="Filter by event op_class."),
    principal: str | None = Query(default=None, description="Filter by principal_sub."),
    target: str | None = Query(default=None, description="Filter by target_name."),
    since: str | None = Query(
        default=None,
        description="Explicit replay cursor; superseded by Last-Event-Id when present.",
    ),
    operator: Operator = _REQUIRE_OPERATOR,
) -> StreamingResponse:
    """Stream broadcast events for the operator's tenant as Server-Sent Events.

    Headers:
        ``Last-Event-Id``: optional SSE reconnect cursor. Takes
        precedence over the ``since`` query parameter when both are
        present.

    Returns:
        :class:`StreamingResponse` with ``media_type=text/event-stream``.
        ``Cache-Control: no-cache, no-store, must-revalidate`` and
        ``X-Accel-Buffering: no`` keep intermediaries from buffering
        the stream into a single response (nginx default behaviour
        would otherwise stall every event behind its buffer flush).
    """
    cursor = _validate_cursor_or_400(
        _resolve_cursor(
            last_event_id_header=request.headers.get("Last-Event-Id"),
            since=since,
        )
    )
    generator = _feed_generator(
        operator=operator,
        cursor=cursor,
        op_class=op_class,
        principal=principal,
        target=target,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )
