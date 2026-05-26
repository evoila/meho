# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared read-side helper for the per-tenant Valkey broadcast stream.

G6.4-T4 (#1103) factors the xrange + filter + redaction-aware parse
loop into a single implementation that both the MCP ``broadcast.recent``
tool (Task #1091) and the UI ``/ui/broadcast/history`` route (Task #869)
go through. Two callers had previously duplicated this body because
their **failure shape** differs:

* The MCP tool is **fail-loud** -- an agent calling ``broadcast.recent``
  needs a Valkey teardown to surface as a JSON-RPC ``-32603`` Internal
  Error; a silent empty result would be a correctness bug.
* The UI history route is **fail-soft** -- a transient broadcast-subchart
  blip should degrade the Last-24h pane to its empty state, not 500 the
  fragment.

The previous design solved this by writing the xrange + parse loop
twice. Every redaction-policy or schema tweak then had to land in two
places and stay in sync, which is exactly the kind of drift that
produces incidents (see PR #1097 follow-up notes).

This module factors the loop into a single core helper plus two named
wrappers, one per failure shape. New callers should pick the wrapper
whose name matches the failure contract their call site holds; the
core helper is internal because a bare call elides the contract choice.

Why two wrappers instead of ``fail_soft: bool``
================================================

A ``fail_soft: bool`` flag would compress the API to one function, but
it gates the function's error-handling semantics on a parameter -- the
"flag argument" anti-pattern Beck / Martin both flag as a code smell.
The caller's intent ("I tolerate a missing read") would live in the
*value* of an argument rather than the *name* of the function, which
hurts grep ("show me every fail-soft call site"), hurts diff review
(a one-character flip changes a load-bearing contract), and reads
ambiguously at the call site. Two named wrappers cost one extra
function definition each; that is a cheap trade for a clearer contract.

PII inheritance
===============

Every entry on ``meho:feed:{tenant_id}`` was already redacted by
:func:`~meho_backplane.broadcast.events.redact_payload` at publish time
(the publish-on-write hook in
:mod:`~meho_backplane.broadcast.publisher`). The helpers in this module
surface what's on the stream verbatim -- they do NOT re-redact, do NOT
mutate the payload field, and do NOT call ``redact_payload``. The
contract flows: publisher redacts -> stream stores redacted -> every
reader (SSE, UI history, MCP tools) surfaces redacted-as-is.

Tenant scoping
==============

The stream key is derived exclusively from the :class:`Operator`'s
verified ``tenant_id`` JWT claim. The helpers take no separate
``tenant_id`` argument; cross-tenant reads are structurally impossible.

References
----------

* Initiative: #1090 (G6.4 broadcast meta-tools).
* T1 origin (the MCP tool that first factored the helper out):
  Task #1091 / PR #1097.
* T4 unifier (this module): Task #1103.
* Sibling primitive: :mod:`meho_backplane.broadcast.publisher`
  (write-side; the fail-loud / fail-open split there mirrors the
  fail-loud / fail-soft split here).
* Sibling read surface, unmodified: the SSE feed generator at
  :func:`meho_backplane.api.v1.feed._feed_generator`. It uses XREAD
  BLOCK (open-ended stream tail), not XRANGE (finite window), so it
  shares the parse primitives in spirit but cannot reuse this body
  verbatim.
* Valkey XRANGE: https://valkey.io/commands/xrange/
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError
from redis.exceptions import RedisError

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent

__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "OP_CLASS_ENUM",
    "InvalidSinceError",
    "list_recent_events_fail_soft",
    "list_recent_events_strict",
]

_log = structlog.get_logger(__name__)

#: Milliseconds per minute. The default ``since`` window is the last
#: :data:`DEFAULT_WINDOW_MINUTES` minutes; computed as
#: ``now_ms - DEFAULT_WINDOW_MINUTES * _MS_PER_MINUTE``.
_MS_PER_MINUTE: Final[int] = 60_000

#: Default look-back window when the caller omits ``since``. 30 minutes
#: -- smaller than the 24h retention ceiling so the default response
#: stays bounded on a busy tenant. The MCP tool's input schema documents
#: this default; the UI history route does NOT use it (the UI always
#: passes an explicit retention-bound ``since``).
DEFAULT_WINDOW_MINUTES: Final[int] = 30

#: ``XRANGE`` end anchor. ``"+"`` is Valkey's "latest available entry"
#: sentinel, so the pull spans from the ``since`` cursor to the live
#: tail. Mirrors :mod:`~meho_backplane.ui.routes.broadcast.stream` and
#: :mod:`~meho_backplane.api.v1.feed`.
_XRANGE_END: Final[str] = "+"

#: Allowed ``op_class`` filter values. The four values the issue body
#: example listed (``read|write|credential_read|audit_query``) plus
#: ``credential_mint`` and ``other`` -- :func:`classify_op` emits those
#: too. Restricting to the four would force the caller to omit the
#: filter when chasing a freshly-minted credential (e.g.
#: ``harbor.robot.create``) or an unmapped HTTP route, which defeats
#: the filter's purpose.
OP_CLASS_ENUM: Final[tuple[str, ...]] = (
    "read",
    "write",
    "credential_read",
    "credential_mint",
    "audit_query",
    "other",
)


class InvalidSinceError(ValueError):
    """Raised when :func:`_parse_since` rejects the caller's input.

    Surfaced as a domain error so a wire-layer caller (the MCP handler)
    can map it to ``-32602`` Invalid Params, and a non-wire caller (the
    UI fail-soft path) can choose to swallow it. The exception body is
    a human-readable description of the offending value.
    """


def _stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.broadcast.publisher._stream_key` and
    every reader-side helper (SSE generator, UI live stream, MCP
    resource) so this module reads the same key the publisher writes.
    """
    return f"meho:feed:{tenant_id}"


def _default_since_ms() -> int:
    """Return the inclusive bare-millisecond cursor for the default window.

    Default window is :data:`DEFAULT_WINDOW_MINUTES` minutes before
    ``now``. A bare ms timestamp as the ``XRANGE`` ``min`` auto-completes
    the sequence to ``0`` (Valkey semantics), so the range is inclusive
    of every entry from that millisecond onward. Clamped at ``0`` so a
    clock skew producing a negative start never reaches Valkey.
    """
    now_ms = int(time.time() * 1000)
    return max(now_ms - DEFAULT_WINDOW_MINUTES * _MS_PER_MINUTE, 0)


def _parse_since(raw: str | None) -> str:
    """Translate the caller-supplied ``since`` into an ``XRANGE`` ``min``.

    Three shapes accepted:

    * ``None`` -- the default :data:`DEFAULT_WINDOW_MINUTES`-minute
      window. Returns the bare millisecond timestamp for ``now -
      DEFAULT_WINDOW_MINUTES`` (inclusive lower bound; Valkey
      auto-completes to sequence ``0``).
    * A Valkey stream cursor (``"<ms>-<seq>"``) -- when the caller is
      paginating a previous response's ``next_cursor``. Returned as
      ``"(<cursor>"`` to make the lower bound EXCLUSIVE, the Valkey-
      blessed pagination shape (https://valkey.io/commands/xrange/
      under "Iterating a stream"). Without the ``(``, the next call
      would re-fetch the last event of the previous page.
    * An ISO-8601 timestamp (``"2026-05-25T10:00:00Z"`` or any value
      :meth:`datetime.fromisoformat` accepts on 3.13) -- converted to
      bare milliseconds (inclusive). The caller's first wall-clock
      query uses this shape; subsequent paginated calls use the
      cursor.

    A trailing ``Z`` is normalised to ``+00:00`` for
    :meth:`datetime.fromisoformat` (the stdlib parser accepts the
    ``Z`` suffix only from 3.11 onwards; the explicit replace makes
    the contract version-independent and reads consistently in tests).

    Invalid inputs raise :class:`InvalidSinceError`.
    """
    if raw is None:
        return str(_default_since_ms())
    if _looks_like_stream_cursor(raw):
        return f"({raw}"
    return _iso8601_to_min_cursor(raw)


def _looks_like_stream_cursor(raw: str) -> bool:
    """Match the Valkey stream entry id shape ``"<ms>-<seq>"``.

    A stream id is always two integers joined by ``-``. ISO-8601
    timestamps contain ``T`` / ``:`` / ``Z`` / ``+`` characters that
    a cursor never carries, so the discriminant is a simple
    ``<digits>-<digits>`` pattern. Loose enough to accept the
    bare-ms form when a caller copy-pastes one back, strict enough
    to reject any ISO-8601 shape.
    """
    if "-" not in raw:
        return False
    ms_part, sep, seq_part = raw.partition("-")
    return bool(sep) and ms_part.isdigit() and seq_part.isdigit()


def _iso8601_to_min_cursor(raw: str) -> str:
    """Convert an ISO-8601 timestamp string to a bare-ms inclusive cursor."""
    iso_normalised = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso_normalised)
    except ValueError as exc:
        raise InvalidSinceError(
            f"since: not a valid ISO-8601 timestamp or Valkey stream cursor: {raw!r}",
        ) from exc
    if parsed.tzinfo is None:
        # Treat a naive timestamp as UTC explicitly; otherwise
        # ``.timestamp()`` would interpret it in the worker's local
        # timezone and produce off-by-hours window starts.
        parsed = parsed.replace(tzinfo=UTC)
    return str(int(parsed.timestamp() * 1000))


def _event_matches(
    event: BroadcastEvent | AgentAnnouncementEvent,
    *,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> bool:
    """Return ``True`` iff *event* matches every non-``None`` filter.

    Exact-match semantics on each non-``None`` field; mirrors
    :func:`meho_backplane.api.v1.feed._passes_filter`.

    Two event classes share the broadcast stream: the audit-driven
    :class:`BroadcastEvent` (one event per audited operation, written
    by :func:`meho_backplane.broadcast.publisher.publish_event`) and
    the agent-authored :class:`AgentAnnouncementEvent` (one event per
    ``meho.broadcast.announce`` call, written by
    :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`).
    They share ``principal_sub`` but diverge on ``op_class`` and
    target attribution:

    * **op_class filter:** Only audit-driven events carry an
      ``op_class``. Announcements have no operation classification, so
      a caller asking ``filter.op_class=write`` is asking for a
      specific *operation* class -- an announcement doesn't qualify
      regardless of its content. (Mirrors the "untagged event doesn't
      satisfy a target filter" rule below.)
    * **target filter:** :class:`BroadcastEvent` carries ``target_name``;
      :class:`AgentAnnouncementEvent` carries ``target``. Both behave
      identically under a non-``None`` filter -- events with no target
      attribution (either field ``None``) never qualify.
    """
    if op_class is not None:
        # AgentAnnouncementEvent has no ``op_class``; the caller asked
        # for an operation classification, an announcement isn't one.
        if isinstance(event, AgentAnnouncementEvent):
            return False
        if event.op_class != op_class:
            return False
    if principal is not None and event.principal_sub != principal:
        return False
    if target is not None:
        event_target = event.target_name if isinstance(event, BroadcastEvent) else event.target
        if event_target != target:
            return False
    return True


def _parse_entry(
    entry_id: str,
    fields: dict[str, str],
    *,
    stream_key: str,
) -> BroadcastEvent | AgentAnnouncementEvent | None:
    """Deserialise one ``XRANGE`` entry; log + skip on shape failure.

    Mirrors the safety net in
    :func:`meho_backplane.mcp.resources.tenant_feed._parse_entry` and
    :func:`meho_backplane.api.v1.feed._process_entries`. The publisher
    always emits ``{"event": <json>}`` today; this branch is the
    forward-compat guard against a future Slack-mirror or third-party
    writer XADD'ing a different shape onto the same key.

    Two event kinds live on the per-tenant stream (G6.4-T2 #1092):

    * Audit-driven :class:`BroadcastEvent` -- written by
      :func:`~meho_backplane.broadcast.publisher.publish_event` per
      audited operation. JSON has no ``event_kind`` field (pre-T2
      schema).
    * Agent-authored :class:`AgentAnnouncementEvent` -- written by
      :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
      per ``meho.broadcast.announce`` call. JSON carries
      ``"event_kind": "agent_announcement"``.

    Dispatch is on the ``event_kind`` field: present and matching
    ``"agent_announcement"`` -> :class:`AgentAnnouncementEvent`,
    everything else -> :class:`BroadcastEvent` (the default). A
    one-off pre-parse via :func:`json.loads` reads only the
    discriminator; the chosen model class re-parses the same JSON
    via :meth:`pydantic.BaseModel.model_validate_json` so validation
    stays full-fidelity.
    """
    raw_event_json = fields.get("event")
    if not isinstance(raw_event_json, str):
        _log.warning(
            "broadcast_history_skipped_unknown_field_shape",
            stream_key=stream_key,
            entry_id=entry_id,
            fields=list(fields.keys()),
        )
        return None
    try:
        # Cheap discriminator peek -- json.loads on a short string is
        # microseconds; an XRANGE page of ``limit <= 1000`` events
        # incurs at most one extra parse pass per entry. The full
        # pydantic validation runs on the chosen model class below.
        peek = json.loads(raw_event_json)
    except json.JSONDecodeError:
        _log.warning(
            "broadcast_history_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None
    event_kind = peek.get("event_kind") if isinstance(peek, dict) else None
    model_cls: type[BroadcastEvent] | type[AgentAnnouncementEvent] = (
        AgentAnnouncementEvent if event_kind == "agent_announcement" else BroadcastEvent
    )
    try:
        return model_cls.model_validate_json(raw_event_json)
    except ValidationError:
        _log.warning(
            "broadcast_history_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None


async def _list_recent_events_core(
    operator: Operator,
    *,
    since: str | None,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    limit: int,
) -> dict[str, Any]:
    """Read recent events for *operator*'s tenant; the shared core body.

    Internal -- callers pick :func:`list_recent_events_strict` or
    :func:`list_recent_events_fail_soft` so the failure-shape choice
    lives in the function name rather than a flag argument.

    Issues a single ``XRANGE`` over ``meho:feed:{operator.tenant_id}``
    from the resolved ``since`` lower bound to ``"+"`` (latest),
    capped at ``limit``. Filters the result in-process (the filter
    surface area is small; per-event server-side filtering would
    require a Lua script and isn't worth the complexity for ``limit
    <= 1000``).

    Returns ``{"events": [<event dict with id>, ...], "next_cursor":
    <str or None>}``. ``next_cursor`` is the **stream entry id of the
    last fetched entry** (NOT the last *matched* one) -- the caller
    can pass it back as ``since`` to walk forward without overlap or
    gaps even when the filter dropped every entry in this page. The
    cursor is ``None`` when the stream returned fewer entries than
    ``limit`` (caller has reached the live tail).

    Raises :class:`InvalidSinceError` on a malformed ``since`` and
    propagates :class:`redis.exceptions.RedisError` on Valkey teardown.
    The two wrappers around this core decide which subset of those to
    re-raise.
    """
    client = get_broadcast_client()
    stream_key = _stream_key(operator.tenant_id)
    min_cursor = _parse_since(since)
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrange(
            stream_key,
            min=min_cursor,
            max=_XRANGE_END,
            count=limit,
        ),
    )

    matched: list[dict[str, Any]] = []
    for entry_id, fields in raw_entries:
        event = _parse_entry(entry_id, fields, stream_key=stream_key)
        if event is None:
            continue
        if not _event_matches(
            event,
            op_class=op_class,
            principal=principal,
            target=target,
        ):
            continue
        # ``id`` is the Valkey stream entry id; the cursor a caller
        # round-trips. The remaining fields come from the event model
        # (including ``event_id`` as the durable UUID for BroadcastEvent
        # or the announcement-id equivalent for AgentAnnouncementEvent).
        # Both coexist so the caller can correlate a stream-cursor walk
        # with the canonical audit row via ``event_id`` / ``audit_id``.
        matched.append({"id": entry_id, **event.model_dump(mode="json")})

    # next_cursor pages forward from the LAST FETCHED entry, not the
    # last matched one -- a page where every entry was filtered out
    # still produces a non-null cursor so the caller can keep walking.
    # When fewer entries came back than the limit, the caller has
    # reached the live tail; ``None`` signals "no more pages right now"
    # without precluding a later call.
    next_cursor: str | None = raw_entries[-1][0] if len(raw_entries) == limit else None
    return {"events": matched, "next_cursor": next_cursor}


async def list_recent_events_strict(
    operator: Operator,
    *,
    since: str | None = None,
    op_class: str | None = None,
    principal: str | None = None,
    target: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Fail-loud variant: re-raises any :class:`RedisError` from Valkey.

    Use at call sites that need to know about Valkey teardowns -- e.g.
    the MCP ``broadcast.recent`` tool, where a silent empty result
    would mislead the calling agent into thinking the stream is empty.
    The MCP dispatcher's generic error handler maps the propagated
    exception to JSON-RPC ``-32603`` Internal Error.

    A malformed ``since`` argument surfaces as
    :class:`InvalidSinceError`; wire-layer callers map that to
    ``-32602`` Invalid Params.

    Mirrors the fail-loud contract on
    :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
    (its write-side counterpart).
    """
    return await _list_recent_events_core(
        operator,
        since=since,
        op_class=op_class,
        principal=principal,
        target=target,
        limit=limit,
    )


async def list_recent_events_fail_soft(
    operator: Operator,
    *,
    since: str | None = None,
    op_class: str | None = None,
    principal: str | None = None,
    target: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Fail-soft variant: a :class:`RedisError` returns the empty result.

    On a Valkey teardown the call returns
    ``{"events": [], "next_cursor": None}`` rather than raising, so a
    caller that surfaces a user-facing pane (the UI history fragment,
    a status dashboard) degrades to the empty state rather than 500-ing
    the response. The error class is logged with a structured warning so
    operators see the teardown in the log; the redis-py exception's
    string form is NOT logged because redis-py exceptions can embed the
    endpoint URL.

    An :class:`InvalidSinceError` is treated as a bug, not a Valkey
    teardown, and is **not** caught here. Fail-soft is "the data store
    is unavailable", not "the caller passed garbage" -- the latter is a
    programming error the caller should surface.

    Mirrors the fail-open contract on
    :func:`~meho_backplane.broadcast.publisher.publish_event` (its
    write-side counterpart for non-load-bearing publishes).
    """
    try:
        return await _list_recent_events_core(
            operator,
            since=since,
            op_class=op_class,
            principal=principal,
            target=target,
            limit=limit,
        )
    except RedisError as exc:
        _log.warning(
            "broadcast_history_fetch_failed",
            error_class=type(exc).__name__,
            stream_key=_stream_key(operator.tenant_id),
        )
        return {"events": [], "next_cursor": None}
