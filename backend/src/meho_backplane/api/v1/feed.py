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
present. When neither is provided, a fresh connection emits a
**backlog prelude** — the last :data:`_BACKLOG_PRELUDE_COUNT`
entries on the tenant stream are replayed in chronological order
before the live-tail BLOCK loop takes over. This solves two
operator-visible bugs that ``$`` alone produces:

1. A fresh ``GET /api/v1/feed`` against a stream with existing
   entries but no new writes during the test window returns zero
   bytes for the first ``_HEARTBEAT_INTERVAL_SECONDS`` — ``$``
   skips backlog AND the heartbeat is 30 s, so curl / EventSource
   intermediaries time out before seeing any byte. The repro in
   ``claude-rdc-hetzner-dc#771`` Finding 14 (G0.16-T3, #1305) is
   exactly this shape: 76+ events on the tenant stream, 0 bytes
   observed at the SSE consumer over 6-8 s.
2. The ``/ui/broadcast`` page renders the header "Live activity
   across tenant X" with an empty event list because the live-tail
   cursor ignores history. The operator perceives the broadcast
   feature as broken even when stream writes are succeeding.

The prelude reads via ``XREVRANGE meho:feed:{tenant} + - COUNT N``,
reverses the result into chronological order, yields each as a
regular ``event: broadcast`` SSE frame, then advances the cursor
to the most recent entry id so the subsequent BLOCK loop reads
strictly past the backlog (no duplicates). When the stream is
empty the prelude is a no-op and the loop enters BLOCK with
cursor ``$`` as before.

Subscribers reconnecting with ``Last-Event-Id`` or callers passing
``since`` skip the prelude — those cursors are explicit
"resume-from-here" anchors and the caller already knows where
they left off.

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
import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from redis.exceptions import RedisError

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
#: live tail. The :func:`_emit_backlog_prelude` helper does an
#: ``XREVRANGE`` before the BLOCK loop so the operator sees recent
#: history immediately and the HTTP intermediary buffer flushes on
#: connection open; see the module docstring's *Replay* section for
#: why the prelude is gated on a ``$`` cursor (explicit replay anchors
#: are honoured verbatim).
_LIVE_TAIL_CURSOR: Final[str] = "$"

#: Number of historical entries replayed on a fresh ``$`` connection
#: before the BLOCK loop runs. 50 matches the
#: :mod:`~meho_backplane.mcp.resources.tenant_feed` snapshot ceiling
#: so the SSE and the MCP-resource snapshot surface the same window
#: of context on first connection. The cap is intentionally tight —
#: a busy tenant with 10 000 entries on the stream should not ship
#: every one of them to a fresh subscriber; an operator who wants the
#: full history queries the audit log instead. Operators who want
#: deeper replay pass an explicit ``since`` cursor.
_BACKLOG_PRELUDE_COUNT: Final[int] = 50

#: Valkey stream entry id shape — ``<ms-timestamp>`` or
#: ``<ms-timestamp>-<sequence>``. Accepts both forms because the
#: bare-timestamp form is legal (Valkey auto-assigns sequence 0) even
#: though XADD-emitted ids always include the sequence suffix.
#: Used by :func:`_validate_cursor_or_400` at the route boundary to
#: reject malformed ``Last-Event-Id`` / ``since`` values before they
#: reach XREAD and trigger an SSE reconnect loop.
_VALKEY_STREAM_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+(?:-\d+)?$")

#: T11-compliant error envelope emitted as an SSE ``event: feed_error``
#: frame when ``XREAD`` raises a :class:`redis.exceptions.RedisError`
#: (connection refused, transport timeout, unexpected response). The
#: shape follows the convention codified in
#: ``docs/codebase/error-message-shape.md`` — stable ``snake_case``
#: code, human-readable message naming the affected component
#: (``meho:feed:<tenant_id>``) at frame-build time, and a doc reference
#: the operator can resolve from their clone. ``data`` is JSON inside
#: the SSE frame so a subscriber can ``JSON.parse`` it without
#: special-casing the error path against the regular ``event: broadcast``
#: data shape.
#:
#: An HTTP 5xx is **not** an option once :func:`_feed_generator` is
#: streaming — ``http.response.start`` was sent on the first outbound
#: yield, so FastAPI cannot retroactively swap to a 503 body. The
#: SSE error event is the only shape the client can observe at that
#: point; closing the stream cleanly afterwards lets the
#: ``EventSource`` reconnect machinery decide whether to retry against
#: the now-known-failing broadcast subsystem rather than tight-looping
#: on the bare-500 it used to see.
_FEED_ERROR_CODE: Final[str] = "broadcast_subsystem_unavailable"
_FEED_ERROR_DOC: Final[str] = "docs/codebase/error-message-shape.md"


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


def _format_feed_error(stream_key: str, exception_type: str) -> str:
    """Format a ``redis.RedisError`` as a T11-compliant SSE error frame.

    Emitted in two situations: (a) the very first ``XREAD`` call on a
    fresh subscriber connection where Valkey is unreachable (matches
    signal 10's ``claude-rdc-hetzner-dc#697`` shape — broadcast pod
    down on a fresh deploy), and (b) a mid-stream transport failure
    where an already-open SSE connection loses its Valkey backend
    after one or more events have already shipped. The frame shape is
    identical in both cases — the client side cannot distinguish
    "broadcast never came up" from "broadcast went away" once the SSE
    headers are out, and the operator's remediation
    (``docs/codebase/error-message-shape.md``) is the same.

    The frame complies with the three-clause convention from
    ``docs/codebase/error-message-shape.md``:

    * ``code`` — stable ``snake_case`` classifier
      (``broadcast_subsystem_unavailable``) callers pattern-match
      without re-parsing prose.
    * ``message`` — names the affected component
      (``meho:feed:<tenant_id>``) and the underlying redis-py exception
      class, then points at the remediation doc. The exception class
      name is structured-log material that the operator's checked-out
      clone can resolve back to the redis-py docs page; the actual
      transport-level message string is **not** echoed here because
      it can name infrastructure topology (broker hostnames, internal
      IPs) per the info-leak boundary the convention doc codifies.
    * ``doc`` — relative path the operator can ``cat`` from their
      checked-out clone or render in the docs site.

    No ``id:`` line — error events are not part of the replay cursor
    sequence; reconnecting subscribers should not re-fetch them.
    """
    detail = {
        "code": _FEED_ERROR_CODE,
        "message": (
            f"broadcast stream '{stream_key}' is unavailable "
            f"(redis-py exception: {exception_type}); see "
            f"{_FEED_ERROR_DOC} for the error-shape convention "
            f"and broadcast-subsystem remediation"
        ),
        "doc": _FEED_ERROR_DOC,
    }
    return f"event: feed_error\ndata: {json.dumps(detail)}\n\n"


def _consume_xread_batch(
    entries: object,
    *,
    cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    stream_key: str,
) -> tuple[str, list[str]]:
    """Unwrap an ``XREAD`` response into ``(new_cursor, [frames_to_yield])``.

    Collapses the ``if entries:`` branch out of :func:`_feed_generator`'s
    main loop. The two responsibilities — advancing the cursor past the
    consumed batch and producing the post-filter list of SSE frames —
    share enough state (the ``items`` list, the per-entry processing)
    that a tuple return is cleaner than two separate helpers.

    *entries* shape: redis-py returns ``[[stream_key, [(entry_id,
    fields), ...]], ...]`` (one outer tuple per stream queried; we
    always query exactly one). ``None`` (BLOCK timeout) or empty outer
    list → the caller sees the cursor unchanged and an empty frame
    list, which is the heartbeat-or-keepalive path.

    The cursor advances to ``items[-1][0]`` BEFORE the post-filter
    frames are yielded so a tenant where every entry filters out
    (busy-but-filtered) still moves past the consumed batch — without
    this, an explicit-cursor replay (``since=<id>`` / ``Last-Event-Id``)
    would re-read the same batch on every iteration.
    """
    if not entries:
        return cursor, []
    # redis-py guarantees the outer shape; cast statically here.
    typed_entries: list[tuple[str, list[tuple[str, dict[str, str]]]]] = entries  # type: ignore[assignment]
    _key, items = typed_entries[0]
    if not items:
        return cursor, []
    new_cursor = items[-1][0]
    frames = [
        frame
        for _entry_id, frame in _process_entries(
            items,
            op_class=op_class,
            principal=principal,
            target=target,
            stream_key=stream_key,
        )
    ]
    return new_cursor, frames


def _log_and_format_broadcast_unavailable(
    operator: Operator,
    stream_key: str,
    exc: RedisError,
) -> str:
    """Log the structured broadcast-down event and return the SSE error frame.

    Extracted out of :func:`_feed_generator`'s ``except RedisError``
    arm so the main loop stays under the code-quality function-size
    ceiling. The two responsibilities collapse into one helper because
    they're always paired — every RedisError catch site logs and
    yields the same shape; splitting them would require the caller to
    carry both pieces of state across two helper calls for no
    readability benefit.

    The log line records the exception class name verbatim plus
    ``exc_info=True`` for the structlog renderer to attach the
    traceback; the response frame carries only the class name (no
    transport-level message string) per the info-leak boundary
    codified in ``docs/codebase/error-message-shape.md``.
    """
    _log.warning(
        "feed_broadcast_unavailable",
        stream_key=stream_key,
        operator_sub=operator.sub,
        exception_type=type(exc).__name__,
        exc_info=True,
    )
    return _format_feed_error(stream_key, type(exc).__name__)


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


async def _emit_backlog_prelude(
    client: object,
    *,
    stream_key: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> tuple[list[str], str | None]:
    """Read up to :data:`_BACKLOG_PRELUDE_COUNT` recent entries; return frames + advance.

    Issues a single ``XREVRANGE stream_key + - COUNT N`` (latest-first)
    and reverses the result into chronological order so the SSE
    consumer sees entries in publish order. Each surviving entry is
    formatted into a regular ``event: broadcast`` frame via the same
    :func:`_process_entries` helper the live loop uses, so the wire
    shape is identical between the prelude and live tail — clients
    can't distinguish "this is replay" from "this is fresh" at the
    frame level (the entry id distinguishes them at the application
    level if needed).

    Returns ``(frames, last_entry_id)``:

    * ``frames`` — the SSE frames to yield, post-filter. Empty list
      when the stream has no entries OR every entry filters out.
    * ``last_entry_id`` — the Valkey id of the most recent entry
      *fetched* (NOT *matched*) — ``None`` when the stream is empty.
      Mirrors the live-loop invariant
      (:func:`_consume_xread_batch`'s ``new_cursor = items[-1][0]``):
      advance the cursor past every consumed entry, not just the
      matched ones, so a busy-but-filtered tenant doesn't re-read
      the same prelude batch on the first BLOCK iteration.

    A :class:`redis.exceptions.RedisError` during the prelude is the
    same operator-visible condition as the live loop's first XREAD
    failure (broadcast pod down, network partition); we let it
    propagate so the live-loop's ``except RedisError`` arm formats a
    single ``feed_error`` frame for the subscriber. Catching it here
    would double-handle the error path.

    :param client: the redis-py asyncio client.
        Typed as ``object`` because the public surface of
        :class:`redis.asyncio.Redis` carries methods that mypy can't
        verify against this module's narrow use (xrevrange takes
        ``name, max, min, count`` keyword-only or positional depending
        on version); we trust the runtime + the framework-research
        introspection over the published stub shape.
    """
    # ``xrevrange`` returns ``[(entry_id, fields_dict), ...]`` —
    # latest-first per the Valkey command contract. ``count`` is
    # keyword-only on redis-py 7.x; the introspection
    # (`uv run python -c "import redis.asyncio as r; help(r.Redis.xrevrange)"`)
    # confirms the signature on the installed wheel.
    raw_items = await client.xrevrange(  # type: ignore[attr-defined]
        stream_key,
        count=_BACKLOG_PRELUDE_COUNT,
    )
    if not raw_items:
        return [], None
    # XREVRANGE returns latest-first; reverse for chronological emit.
    items: list[tuple[str, dict[str, str]]] = list(reversed(raw_items))
    last_entry_id = items[-1][0]
    frames = [
        frame
        for _entry_id, frame in _process_entries(
            items,
            op_class=op_class,
            principal=principal,
            target=target,
            stream_key=stream_key,
        )
    ]
    return frames, last_entry_id


async def _feed_generator(
    operator: Operator,
    cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> AsyncIterator[str]:
    """SSE generator: prelude → BLOCK on XREAD, delegate parsing, heartbeat-on-silence.

    Backlog prelude — when *cursor* is ``$`` (the live-tail default
    selected by :func:`_resolve_cursor` when neither ``Last-Event-Id``
    nor ``since`` is provided), the generator first calls
    :func:`_emit_backlog_prelude` to replay the most recent
    :data:`_BACKLOG_PRELUDE_COUNT` entries on the stream. This solves
    the consumer-visible bug surfaced by ``claude-rdc-hetzner-dc#771``
    Finding 14 (G0.16-T3, #1305): a fresh
    ``GET /api/v1/feed`` against a tenant with existing entries but
    no new writes during the test window otherwise returns zero bytes
    for ``_HEARTBEAT_INTERVAL_SECONDS`` (``$`` skips backlog AND the
    heartbeat cadence is 30 s), tripping curl / EventSource
    intermediary timeouts before any byte is observed. The prelude
    also gives the ``/ui/broadcast`` page a populated initial render
    instead of a misleading empty list under a "Live activity" header.
    Subscribers that pass an explicit replay cursor
    (``Last-Event-Id`` or ``since``) skip the prelude — those cursors
    are "resume exactly here" anchors and the caller already saw the
    history.

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

    On :class:`redis.exceptions.RedisError` (connection refused,
    transport timeout, unexpected response) the generator emits one
    T11-compliant ``event: feed_error`` frame and breaks. The
    empty-stream case (Valkey reachable, no entries yet) is NOT a
    failure: redis-py returns ``None`` from XREAD and the
    :func:`_consume_xread_batch` helper falls through to the heartbeat
    path. The failure handled here is the genuinely-broken case
    (broadcast pod down, network partition) — signal 10 of
    ``claude-rdc-hetzner-dc#697``.
    """
    client = get_broadcast_client()
    stream_key = _stream_key(operator)
    last_heartbeat = time.monotonic()

    try:
        # Backlog prelude — only when the caller didn't pin an explicit
        # replay anchor. ``Last-Event-Id`` / ``since`` are honoured as
        # "resume exactly here", which is incompatible with a backlog
        # dump from "+" (would replay events the caller already saw).
        # See the module docstring's *Replay* section for the full
        # rationale and the consumer-side repro this addresses.
        if cursor == _LIVE_TAIL_CURSOR:
            try:
                prelude_frames, prelude_last_id = await _emit_backlog_prelude(
                    client,
                    stream_key=stream_key,
                    op_class=op_class,
                    principal=principal,
                    target=target,
                )
            except RedisError as exc:
                # Same operator-visible condition as the live-loop's
                # first XREAD failure (broadcast pod down). Emit one
                # T11-compliant error frame and break — the connection
                # closes cleanly and ``EventSource`` reconnects per its
                # spec semantics, which will re-evaluate Valkey
                # reachability on the next handshake.
                yield _log_and_format_broadcast_unavailable(operator, stream_key, exc)
                return
            for frame in prelude_frames:
                yield frame
            if prelude_last_id is not None:
                # Advance past every prelude entry — including the ones
                # that filtered out — so the BLOCK loop reads strictly
                # past the prelude window. Without this advance, a
                # fully-filtered prelude would leave cursor at ``$``,
                # the BLOCK loop would silently re-skip every prelude
                # entry on its next read (XREAD with id strictly later
                # than $ never returns older entries, but a busy stream
                # could still flag this as "we re-fetched and the
                # filter dropped them again"). Setting the cursor here
                # makes the intent explicit.
                cursor = prelude_last_id
            if prelude_frames:
                last_heartbeat = time.monotonic()
        while True:
            try:
                entries = await client.xread(
                    {stream_key: cursor},
                    block=_XREAD_BLOCK_MS,
                    count=_XREAD_COUNT,
                )
            except RedisError as exc:
                # Catch the full ``RedisError`` family — its concrete
                # subclasses (``ConnectionError``, ``TimeoutError``,
                # ``ResponseError``) all share the same operator-side
                # remediation: "the broadcast subsystem is not
                # reachable; check the broadcast pod / network /
                # ``BROADCAST_REDIS_URL`` and read the doc". A single
                # ``feed_error`` frame is the right granularity at the
                # SSE boundary; the helper handles structured logging
                # and frame formatting in one paired call.
                #
                # Re-raising would propagate to FastAPI's default
                # handler — but ``http.response.start`` was already
                # sent on the first iteration (or on a prior yielded
                # frame on subsequent iterations), so FastAPI cannot
                # swap to a 5xx body; the consumer sees a bare drop.
                # Yielding a structured frame + breaking the loop
                # gives the client a recoverable signal and lets the
                # underlying transport close cleanly.
                yield _log_and_format_broadcast_unavailable(operator, stream_key, exc)
                break
            now = time.monotonic()
            cursor, frames = _consume_xread_batch(
                entries,
                cursor=cursor,
                op_class=op_class,
                principal=principal,
                target=target,
                stream_key=stream_key,
            )
            for frame in frames:
                yield frame
            if frames:
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
