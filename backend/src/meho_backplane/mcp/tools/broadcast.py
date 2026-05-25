# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-facing MCP tools backed by the per-tenant broadcast stream.

G6.4 Initiative (#1090) ships three agent-facing MCP tools that mirror
the four-step ``broadcast discipline`` documented in the Layer-2
consumer-onboarding template (PR #1028 / Initiative #229 G7.1):

* T1 (#1091, this module) -- ``meho.broadcast.recent``: read the last
  N events from ``meho:feed:{tenant_id}`` with optional time-window
  and filter narrowing. Returns ``{events, next_cursor}``; the cursor
  round-trips back as ``since`` to walk forward without overlap or
  gaps.
* T2 (#1092, follow-on) -- ``meho.broadcast.announce``: agent-authored
  intent / completion publish.
* T3 (#1093, follow-on) -- ``meho.broadcast.watch``: long-poll batch
  read (caller passes ``since_cursor``, server returns ``{events,
  next_cursor}`` capped at N events).

File-layout convention
======================

All three tools share this file by design (mirroring the precedent set
by :mod:`~meho_backplane.mcp.tools.broadcast_overrides` -- three
broadcast-namespaced tools in one file). Each tool's section is bounded
by ``# === <tool-name> ===`` / ``# === end <tool-name> ===`` markers so
T2 and T3 can land in parallel branches with minimal merge-conflict
surface against this file's body.

Shared helpers (``_default_since_ms`` / ``_parse_since`` /
``_event_matches`` / ``_list_recent_events_impl``) live above the
per-tool sections; they're designed for re-use by T2's announce-on-write
(no read) and T3's watch (same xrange shape, different cursor framing
contract).

Why the helper isn't shared with the UI history route
=====================================================

:mod:`~meho_backplane.ui.routes.broadcast.history` ALSO does an
``xrange`` over ``meho:feed:{tenant_id}``, but its surrounding contract
differs in two load-bearing ways:

1. **Failure shape.** The UI history route is fail-soft -- a Valkey
   error returns an empty list so the page degrades to the empty
   state rather than 500-ing. The MCP tool is fail-loud: invalid
   ``since`` / ``filter`` / ``limit`` surfaces as JSON-RPC
   ``-32602`` Invalid Params, and a Valkey teardown bubbles up to
   the dispatcher's ``-32603`` Internal Error path.
2. **Window + cursor semantics.** The UI route always fetches a fixed
   ``now - retention_hours`` window with no filter and no cursor.
   The MCP tool accepts arbitrary ``since`` (ISO-8601 OR Valkey
   cursor), enforces a ``[1, 1000]`` limit, and returns a
   ``next_cursor`` for pagination round-trips.

A shared helper would either compromise the UI's fail-soft contract
(adding exceptions to the call-site) or compromise the MCP tool's
strict-failure contract (swallowing invalid input as empty results).
The duplication cost (one ``xrange`` + parse loop) is small; the
contract-erosion cost would be larger. The UI route stays as-is; this
helper serves T1/T2/T3 only. A future tightening that re-unifies the
two reads MUST first reconcile the failure-shape contract.

PII inheritance
===============

Every entry on ``meho:feed:{tenant_id}`` was already redacted by
:func:`~meho_backplane.broadcast.events.redact_payload` at publish
time (T3, the ``publish-on-write`` hook). This tool reads what's on
the stream verbatim -- it does NOT re-redact, does NOT mutate the
payload field, and does NOT call ``redact_payload``. The contract
flows: publisher redacts -> stream stores redacted -> every reader
(SSE, UI history, MCP resource, THIS tool) surfaces redacted-as-is.

Tenant scoping is structural
============================

The stream key is derived exclusively from ``operator.tenant_id``
(the verified JWT claim). The MCP tool's input schema has NO
``tenant_id`` argument; cross-tenant reads are structurally
impossible -- not "checked then rejected" but "no surface that could
ask for another tenant's stream in the first place". A tenant-A
operator's call always reads ``meho:feed:{A}``; the path that would
read ``meho:feed:{B}`` does not exist.

References
----------

* Parent Initiative: #1090.
* Parent Goal: #217.
* Stream key + redaction contract:
  :mod:`~meho_backplane.broadcast.events`.
* Sibling tool registration shape:
  :mod:`~meho_backplane.mcp.tools.broadcast_overrides`.
* Sibling resource (poll-only snapshot of latest 50):
  :mod:`~meho_backplane.mcp.resources.tenant_feed`.
* MCP 2025-06-18 Tools:
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
* Valkey XRANGE (cursor + exclusive ``(`` syntax):
  https://valkey.io/commands/xrange/
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    AgentAnnouncementEvent,
    BroadcastEvent,
    get_broadcast_client,
    publish_agent_announcement,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# ===========================================================================
# === shared helpers (T1/T2/T3) ===
# ===========================================================================

#: Milliseconds per minute. The default ``since`` window is the last 30m;
#: derived from ``now`` at call time as ``now_ms - 30 * _MS_PER_MINUTE``.
_MS_PER_MINUTE: Final[int] = 60_000

#: Default look-back window when the caller omits ``since``. Pinned to
#: 30 minutes per the task body. Smaller than the 24h retention
#: ceiling so the default response stays bounded even on a busy tenant.
_DEFAULT_WINDOW_MINUTES: Final[int] = 30

#: ``XRANGE`` end anchor. ``"+"`` is Valkey's "latest available entry"
#: sentinel, so the pull spans from the ``since`` cursor to the live
#: tail. Mirrors :mod:`~meho_backplane.ui.routes.broadcast.history`.
_XRANGE_END: Final[str] = "+"

#: Allowed ``op_class`` filter values. Superset of the four values the
#: G6.4-T1 issue body example listed (``read|write|credential_read|
#: audit_query``); we additionally allow ``credential_mint`` and
#: ``other`` because :func:`~meho_backplane.broadcast.events.classify_op`
#: emits those too. Restricting to the four would force the agent to
#: omit the filter when chasing a freshly-minted credential
#: (``harbor.robot.create``) or an unmapped HTTP route, which defeats
#: the filter's purpose.
_OP_CLASS_ENUM: Final[tuple[str, ...]] = (
    "read",
    "write",
    "credential_read",
    "credential_mint",
    "audit_query",
    "other",
)


def _stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.broadcast.publisher._stream_key` and
    every reader-side helper (SSE generator, UI history, MCP resource)
    so the tool reads the same key the publisher writes.
    """
    return f"meho:feed:{tenant_id}"


def _default_since_ms() -> int:
    """Return the inclusive bare-millisecond cursor for the default window.

    Default window is :data:`_DEFAULT_WINDOW_MINUTES` minutes before
    ``now``. A bare ms timestamp as the ``XRANGE`` ``min`` auto-completes
    the sequence to ``0`` (Valkey semantics), so the range is inclusive
    of every entry from that millisecond onward. Clamped at ``0`` so a
    clock skew producing a negative start never reaches Valkey.
    """
    now_ms = int(time.time() * 1000)
    return max(now_ms - _DEFAULT_WINDOW_MINUTES * _MS_PER_MINUTE, 0)


def _parse_since(raw: str | None) -> str:
    """Translate the caller-supplied ``since`` into an ``XRANGE`` ``min``.

    Three shapes accepted:

    * ``None`` -- the default 30-minute window. Returns the bare
      millisecond timestamp for ``now - 30m`` (inclusive lower bound,
      Valkey auto-completes to sequence ``0``).
    * A Valkey stream cursor (``"<ms>-<seq>"``) -- when the caller is
      paginating a previous response's ``next_cursor``. Returned as
      ``"(<cursor>"`` to make the lower bound EXCLUSIVE, which is the
      Valkey-blessed pagination shape (https://valkey.io/commands/xrange/
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

    Invalid inputs raise :class:`McpInvalidParamsError`.
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
        raise McpInvalidParamsError(
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
    :func:`meho_backplane.mcp.resources.tenant_feed._parse_entry`. The
    publisher always emits ``{"event": <json>}`` today; this branch
    is the forward-compat guard against a future Slack-mirror or
    third-party writer XADD'ing a different shape onto the same key.

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
    ``"agent_announcement"`` → :class:`AgentAnnouncementEvent`,
    everything else → :class:`BroadcastEvent` (the default). A
    one-off pre-parse via :func:`json.loads` reads only the
    discriminator; the chosen model class re-parses the same JSON
    via :meth:`pydantic.BaseModel.model_validate_json` so validation
    stays full-fidelity.
    """
    raw_event_json = fields.get("event")
    if not isinstance(raw_event_json, str):
        _log.warning(
            "broadcast_recent_skipped_unknown_field_shape",
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
            "broadcast_recent_skipped_malformed_event",
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
            "broadcast_recent_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None


async def _list_recent_events_impl(
    operator: Operator,
    *,
    since: str | None,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    limit: int,
) -> dict[str, Any]:
    """Read recent events for *operator*'s tenant; the shared helper T1/T2/T3 call.

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
        # round-trips. The remaining fields come from BroadcastEvent
        # (including ``event_id`` as the durable UUID). Both coexist
        # so the agent can correlate a stream-cursor-driven walk
        # with the canonical audit row via ``event_id`` / ``audit_id``.
        matched.append({"id": entry_id, **event.model_dump(mode="json")})

    # next_cursor pages forward from the LAST FETCHED entry, not the
    # last matched one -- a page where every entry was filtered out
    # still produces a non-null cursor so the caller can keep
    # walking. When fewer entries came back than the limit, the
    # caller has reached the live tail; ``None`` signals "no more
    # pages right now" without precluding a later call.
    next_cursor: str | None = raw_entries[-1][0] if len(raw_entries) == limit else None
    return {"events": matched, "next_cursor": next_cursor}


# ===========================================================================
# === meho.broadcast.recent ===
# ===========================================================================


def _extract_filter(arguments: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract the three ``filter.*`` sub-keys, asserting string-or-absent.

    The wire schema's ``filter`` object permits each sub-key to be
    omitted entirely OR set to a string. Anything else (a number, a
    list, an object) surfaces as JSON-RPC ``-32602`` -- the JSON
    Schema validator at the dispatcher layer catches structural
    violations; this helper picks the typed view for the handler.
    """
    filter_obj = arguments.get("filter") or {}
    if not isinstance(filter_obj, dict):
        raise McpInvalidParamsError("filter: must be an object when provided")
    op_class = filter_obj.get("op_class")
    principal = filter_obj.get("principal")
    target = filter_obj.get("target")
    for name, value in (("op_class", op_class), ("principal", principal), ("target", target)):
        if value is not None and not isinstance(value, str):
            raise McpInvalidParamsError(f"filter.{name}: must be a string when provided")
    return op_class, principal, target


async def _handler_recent(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho.broadcast.recent`` handler.

    Parses the wire arguments, delegates to
    :func:`_list_recent_events_impl`, and returns the JSON-shaped
    response the dispatcher wraps in the MCP ``content`` envelope.
    Errors surface as :class:`McpInvalidParamsError` (-32602); the
    JSON Schema validator at the dispatcher already enforces the
    coarse shape (``limit`` range, ``filter`` object type) so the
    per-field re-checks here are belt-and-suspenders for the
    handler's typed contract.
    """
    since = arguments.get("since")
    if since is not None and not isinstance(since, str):
        raise McpInvalidParamsError("since: must be a string when provided")
    op_class, principal, target = _extract_filter(arguments)
    raw_limit = arguments.get("limit", 100)
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        raise McpInvalidParamsError("limit: must be an integer in [1, 1000]")
    if raw_limit < 1 or raw_limit > 1000:
        raise McpInvalidParamsError("limit: must be in [1, 1000]")
    return await _list_recent_events_impl(
        operator,
        since=since,
        op_class=op_class,
        principal=principal,
        target=target,
        limit=raw_limit,
    )


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.recent",
        description=(
            "Read the operator's tenant's recent broadcast events from "
            "meho:feed:{tenant_id} (Initiative #1090, Task #1091). "
            "Returns {events, next_cursor}. Default window is the last "
            "30 minutes when 'since' is omitted; pass an ISO-8601 "
            "timestamp ('2026-05-25T10:00:00Z') or a Valkey stream "
            "cursor ('1747800000000-0') to override. The 'filter' "
            "object narrows by exact-match op_class / principal "
            "(JWT 'sub') / target (target_name). 'limit' caps the "
            "page at 1..1000 (default 100); the response's "
            "'next_cursor' round-trips as the next call's 'since' "
            "for gap-free pagination. Tenant scoping is structural -- "
            "every read targets the operator's own tenant stream; "
            "there is no input that could request another tenant's "
            "events. Payloads inherit the publisher-side redaction "
            "(credential_read + audit_query events are already "
            "aggregate-only on the stream)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "ISO-8601 timestamp ('2026-05-25T10:00:00Z') "
                        "OR Valkey stream cursor ('1747800000000-0'). "
                        "Omit for the last 30 minutes."
                    ),
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "op_class": {
                            "type": "string",
                            "enum": list(_OP_CLASS_ENUM),
                            "description": ("Exact-match filter on event op_class."),
                        },
                        "principal": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": ("Exact-match filter on JWT 'sub' claim."),
                        },
                        "target": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": ("Exact-match filter on event target_name."),
                        },
                    },
                    "additionalProperties": False,
                    "description": (
                        "Filter narrows the result; all sub-keys "
                        "optional. Each non-null sub-key is exact-match."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                    "description": (
                        "Max events returned in one page. The XRANGE "
                        "fetch is capped at this value before filtering."
                    ),
                },
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_handler_recent,
)


# ===========================================================================
# === end meho.broadcast.recent ===
# ===========================================================================


# ===========================================================================
# === meho.broadcast.announce ===
# ===========================================================================


async def _publish_agent_announcement_impl(
    operator: Operator,
    *,
    activity: str,
    target: str | None,
    scope: str | None,
    phase: str,
) -> dict[str, Any]:
    """Build + publish one :class:`AgentAnnouncementEvent`, return ``{event_id}``.

    Internal helper for ``meho.broadcast.announce``. Kept separate from
    :func:`_handler_announce` so the construction-and-publish seam is
    importable by tests that want to bypass the wire-side argument
    plumbing (e.g. cross-tenant isolation assertions that operate on
    a directly-constructed :class:`Operator`).

    ``phase`` arrives pre-validated by the caller (either by the
    JSON-Schema enum on ``inputSchema`` or by a unit-test passing one
    of the three accepted literals). The function casts it to the
    :class:`AgentAnnouncementEvent` ``phase`` field's
    :class:`typing.Literal` union via pydantic validation -- a bad
    value would raise :class:`ValidationError` at the model
    constructor, which the handler maps to ``-32602`` upstream.

    Tenant scoping is structural: the ``tenant_id`` on the constructed
    event is sourced exclusively from ``operator.tenant_id`` (the
    JWT-bound claim). The handler's input schema has no ``tenant_id``
    field; a cross-tenant announce is impossible by construction, not
    by check-then-reject.
    """
    event = AgentAnnouncementEvent(
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        activity=activity,
        target=target,
        scope=scope,
        phase=phase,  # type: ignore[arg-type]  # pydantic validates the Literal at construction
        ts=datetime.now(UTC),
    )
    entry_id = await publish_agent_announcement(event)
    return {"event_id": entry_id}


async def _handler_announce(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho.broadcast.announce`` handler.

    Validates the wire arguments belt-and-suspenders (the dispatcher's
    JSON-Schema validator runs first, but the per-field re-checks here
    preserve the typed contract a direct ``_handler_announce`` call
    site -- e.g. a unit test -- would otherwise miss). Surfaces every
    validation failure as :class:`McpInvalidParamsError` (``-32602``).

    On a clean parse, delegates to
    :func:`_publish_agent_announcement_impl` for the construct +
    publish step. The publisher is **fail-loud** -- a Valkey teardown
    propagates as :class:`Exception` and the MCP dispatcher's generic
    handler maps it to ``-32603`` Internal Error. This is distinct
    from :func:`~meho_backplane.broadcast.publisher.publish_event`'s
    fail-open semantics by design (see the publisher module's
    docstring); the agent explicitly emitted the announcement and
    needs to know whether it landed.

    Trust boundary
    --------------

    ``activity`` and ``scope`` are agent-authored free text. The
    handler does NOT sanitise or rewrite them -- consumers MUST treat
    the strings as UNTRUSTED. This mirrors the
    :mod:`~meho_backplane.conventions.preamble` precedent for
    operator-authored convention bodies wrapped in
    ``<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>`` delimiters.
    See :class:`AgentAnnouncementEvent`'s docstring for the per-surface
    contract (frontend escapes, Slack plain-text, downstream agents
    must not interpret as policy).
    """
    activity = arguments.get("activity")
    if not isinstance(activity, str):
        raise McpInvalidParamsError("activity: must be a string")
    if len(activity) < 1:
        raise McpInvalidParamsError("activity: must be non-empty")
    if len(activity) > ACTIVITY_MAX_CHARS:
        raise McpInvalidParamsError(
            f"activity: too long ({len(activity)} chars); cap is {ACTIVITY_MAX_CHARS}",
        )
    target = arguments.get("target")
    if target is not None and not isinstance(target, str):
        raise McpInvalidParamsError("target: must be a string when provided")
    scope = arguments.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise McpInvalidParamsError("scope: must be a string when provided")
    phase_arg = arguments.get("phase", "update")
    if phase_arg not in ("start", "update", "completion"):
        raise McpInvalidParamsError(
            "phase: must be one of 'start', 'update', 'completion'",
        )
    return await _publish_agent_announcement_impl(
        operator,
        activity=activity,
        target=target,
        scope=scope,
        phase=phase_arg,
    )


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.announce",
        description=(
            "Publish an agent-authored announcement to the operator's "
            "tenant broadcast stream (Initiative #1090, Task #1092). "
            "Use this to surface intent ('about to investigate cluster "
            "X latency'), progress updates ('halfway through the "
            "vCenter migration'), or completion summaries ('finished -- "
            "rolled back to snapshot S'). The announcement lands on "
            "the same meho:feed:{tenant_id} stream that audit-driven "
            "events use, distinguished by event_kind='agent_announcement'; "
            "other operators read it back via meho.broadcast.recent. "
            "'activity' is mandatory (1..500 chars), 'target' and "
            "'scope' are optional free-form attribution, 'phase' is "
            "one of 'start' / 'update' / 'completion' (default "
            "'update'). The publish is fail-loud -- a Valkey teardown "
            "surfaces as JSON-RPC -32603 Internal Error so the agent "
            "knows the announcement did not land (distinct from the "
            "audit-driven publisher which fail-opens). Tenant scoping "
            "is structural -- the input schema has no tenant_id "
            "argument; the event always writes to the operator's own "
            "tenant stream. Returns {event_id} -- the Valkey stream "
            "entry id, round-trip-able through meho.broadcast.recent's "
            "'since' cursor for verification."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "activity": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ACTIVITY_MAX_CHARS,
                    "description": (
                        "Free-text description of the planned, "
                        "in-progress, or completed activity. UNTRUSTED "
                        "agent-authored content; consumers MUST escape "
                        "on render (HTML, Slack, downstream LLM)."
                    ),
                },
                "target": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": (
                        "Optional target_name attribution -- the "
                        "managed target the activity scopes to "
                        "(e.g. 'prod-vc-1', 'kube-prod'). Omit when "
                        "the activity is target-less."
                    ),
                },
                "scope": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": (
                        "Optional free-form scope hint (e.g. "
                        "'investigating cluster X latency'). UNTRUSTED "
                        "agent-authored content; same render rules as "
                        "'activity'."
                    ),
                },
                "phase": {
                    "type": "string",
                    "enum": ["start", "update", "completion"],
                    "default": "update",
                    "description": (
                        "Lifecycle phase. 'start' marks intent at "
                        "task entry; 'update' is the default for "
                        "in-flight progress; 'completion' marks the "
                        "wrap-up summary. The Prometheus counter "
                        "broadcast_agent_announcements_total partitions "
                        "by this label."
                    ),
                },
            },
            "required": ["activity"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="write",
    ),
    handler=_handler_announce,
)


# ===========================================================================
# === end meho.broadcast.announce ===
# ===========================================================================

# ===========================================================================
# === meho.broadcast.watch ===
# ===========================================================================

#: Default ``XREAD BLOCK`` window when the caller omits ``timeout_ms``. 10s
#: balances "wait long enough that the average operator pause produces a
#: hit" against "release the chassis worker before any HTTP intermediary
#: idles the connection". Half the maximum so a default-shaped client has
#: room for a per-call override without bumping into the cap.
_WATCH_DEFAULT_TIMEOUT_MS: Final[int] = 10_000

#: Lower bound on ``timeout_ms``. 100ms is the floor below which the
#: long-poll degenerates into a non-blocking poll plus syscall overhead;
#: clients that want strictly-non-blocking semantics should be using
#: :func:`_handler_recent` instead.
_WATCH_MIN_TIMEOUT_MS: Final[int] = 100

#: Upper bound on ``timeout_ms``. 30s matches the SSE bridge's
#: ``_XREAD_BLOCK_MS`` (:mod:`~meho_backplane.api.v1.feed`) and the
#: common HTTP-intermediary keep-alive ceiling (nginx default
#: ``proxy_read_timeout`` 60s; AWS ALB idle 60s). Allowing infinite-block
#: (``block=0``) here would let a single misbehaving caller tie up a
#: chassis worker for the lifetime of the process; the cap is a hard
#: backpressure boundary, not a hint.
_WATCH_MAX_TIMEOUT_MS: Final[int] = 30_000

#: Upper bound on entries returned in one ``XREAD`` call. Mirrors
#: :data:`meho_backplane.api.v1.feed._XREAD_COUNT` so a busy tenant
#: doesn't surface a 10,000-event burst in a single tool response that
#: would blow the agent's token budget. The caller paginates by calling
#: again with the returned ``next_cursor``.
_WATCH_XREAD_COUNT: Final[int] = 100


def _validate_timeout_ms(raw: Any) -> int:
    """Coerce *raw* into the ``timeout_ms`` integer and enforce the bounds.

    ``raw`` may be absent (None) -- the default :data:`_WATCH_DEFAULT_TIMEOUT_MS`
    applies. Anything other than a plain ``int`` (including ``bool``, which
    Python's ``isinstance(..., int)`` would accept, and ``float``, which
    would silently truncate via ``int(...)``) rejects with
    :class:`McpInvalidParamsError`. Out-of-range integers reject for the
    same reason -- the cap is non-negotiable; a caller cannot opt past it.

    Surface area defence: even though :func:`_handler_watch`'s
    ``inputSchema`` declares ``"type": "integer"`` plus
    ``minimum`` / ``maximum``, the per-field re-check here is the
    handler-side guarantee. A future schema relaxation or a registration
    bug wouldn't quietly widen the contract; the handler would still
    reject the bad input as ``-32602``.
    """
    if raw is None:
        return _WATCH_DEFAULT_TIMEOUT_MS
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise McpInvalidParamsError(
            f"timeout_ms: must be an integer in [{_WATCH_MIN_TIMEOUT_MS}, {_WATCH_MAX_TIMEOUT_MS}]",
        )
    if raw < _WATCH_MIN_TIMEOUT_MS or raw > _WATCH_MAX_TIMEOUT_MS:
        raise McpInvalidParamsError(
            f"timeout_ms: must be in [{_WATCH_MIN_TIMEOUT_MS}, {_WATCH_MAX_TIMEOUT_MS}]",
        )
    return raw


async def _xread_or_cancelled(
    operator: Operator,
    *,
    stream_key: str,
    since_cursor: str,
    timeout_ms: int,
) -> Any:
    """Await ``XREAD BLOCK`` for *operator*'s stream; re-raise cancellation cleanly.

    Wraps the single ``xread`` call so the ``except asyncio.CancelledError``
    arm stays self-contained -- which both bounds the cancellation
    contract to one shape (xread is the only awaitable in this leg of
    :func:`_watch_events_impl`) and keeps the parent function under the
    code-quality function-size ceiling.

    Cancellation contract: when Starlette cancels the JSON-RPC request
    mid-block (client hung up, transport closed, MCP dispatcher tore the
    call down), the pending ``xread`` raises
    :class:`asyncio.CancelledError`. We log the structured disconnect and
    re-raise -- swallowing ``CancelledError`` breaks the task tree's
    unwind invariants per the asyncio contract (Sonar S7497; Python 3.13+
    re-issues cancellation if it goes unpropagated). The chassis worker
    is released the moment the re-raise lands.

    *since_cursor* is passed to ``xread`` verbatim; XREAD reads entries
    strictly past the cursor (its "last id seen" semantics are
    exclusive, unlike XRANGE's inclusive ``min``).
    """
    client = get_broadcast_client()
    try:
        return await client.xread(
            {stream_key: since_cursor},
            block=timeout_ms,
            count=_WATCH_XREAD_COUNT,
        )
    except asyncio.CancelledError:
        _log.info(
            "broadcast_watch_cancelled",
            stream_key=stream_key,
            operator_sub=operator.sub,
            since_cursor=since_cursor,
        )
        raise


def _xread_items_or_none(
    raw_response: Any,
) -> list[tuple[str, dict[str, str]]] | None:
    """Unwrap the redis-py ``XREAD`` envelope to the entry list, or ``None``.

    XREAD's two distinguishable result shapes:

    * ``None`` (timeout-with-no-entries) or an empty outer list -- the
      "nothing for us" branch. The truthiness check collapses both into
      the same return ``None`` here; the SSE bridge's
      ``_feed_generator`` uses the same idiom.
    * ``[[stream_key, [(entry_id, fields), ...]]]`` -- one outer tuple
      per stream queried. We query exactly one stream (the operator's
      tenant feed), so ``raw_response[0][1]`` is the
      ``[(entry_id, fields_dict), ...]`` list. A defensive third branch
      catches an outer tuple with an empty inner list (never observed
      with redis-py 7.x in practice -- the client collapses to ``None``
      -- but the branch keeps the response safe under a future redis-py
      shape change).

    Returning the inner list (or ``None``) collapses three branches in
    the caller to one ``if items is None`` check.
    """
    if not raw_response:
        return None
    entries = cast(
        "list[tuple[str, list[tuple[str, dict[str, str]]]]]",
        raw_response,
    )
    _key, items = entries[0]
    if not items:
        return None
    return items


def _filter_xread_items(
    items: list[tuple[str, dict[str, str]]],
    *,
    stream_key: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> list[dict[str, Any]]:
    """Parse + filter the XREAD batch into the wire-shape events list.

    Each surviving entry carries ``id`` (the Valkey stream cursor, the
    round-trip handle) plus every :class:`BroadcastEvent` field
    (including the durable ``event_id`` UUID and the ``audit_id`` for
    audit-log correlation). Entries that fail :func:`_parse_entry` (bad
    field shape, malformed JSON) are logged + skipped inside the helper
    so the watch handler never raises on a single bad entry.
    """
    matched: list[dict[str, Any]] = []
    for entry_id, fields in items:
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
        matched.append({"id": entry_id, **event.model_dump(mode="json")})
    return matched


async def _watch_events_impl(
    operator: Operator,
    *,
    since_cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    """Long-poll the operator's tenant stream for entries strictly past *since_cursor*.

    Issues a single ``XREAD BLOCK`` against ``meho:feed:{operator.tenant_id}``
    with ``timeout_ms`` as the block window; the caller's chassis worker
    is tied up for up to that long. When entries arrive, advances the
    cursor to the **last fetched** entry id (NOT the last *matched* one --
    same invariant as :func:`_list_recent_events_impl`) so a filter-heavy
    page where every entry was dropped still advances the cursor and the
    next call doesn't re-read the same batch.

    No-events shape: returns ``{events: [], next_cursor: since_cursor}``.
    The unchanged cursor signals "I waited; nothing landed" -- the caller
    re-polls with the same cursor.

    Implementation split across three helpers:

    * :func:`_xread_or_cancelled` owns the ``xread`` await and the
      :class:`asyncio.CancelledError` re-raise contract.
    * :func:`_xread_items_or_none` unwraps the redis-py envelope.
    * :func:`_filter_xread_items` parses + filters the batch.

    The caller obtains an initial cursor from :func:`_handler_recent`'s
    ``next_cursor`` field; from that point forward each watch call's
    ``next_cursor`` feeds the next call's ``since_cursor``.
    """
    stream_key = _stream_key(operator.tenant_id)
    raw_response = await _xread_or_cancelled(
        operator,
        stream_key=stream_key,
        since_cursor=since_cursor,
        timeout_ms=timeout_ms,
    )
    items = _xread_items_or_none(raw_response)
    if items is None:
        return {"events": [], "next_cursor": since_cursor}
    # Advance to the LAST FETCHED entry id BEFORE filtering, mirroring
    # the M2 fix on the SSE generator: a page where every entry is
    # filtered out still moves the cursor forward so a busy-but-filtered
    # tenant doesn't deliver the same batch on every poll. The cursor
    # is the canonical "last id seen" -- the filter is the caller's
    # post-fetch concern.
    next_cursor = items[-1][0]
    matched = _filter_xread_items(
        items,
        stream_key=stream_key,
        op_class=op_class,
        principal=principal,
        target=target,
    )
    return {"events": matched, "next_cursor": next_cursor}


async def _handler_watch(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho.broadcast.watch`` handler.

    Parses the wire arguments, then delegates to :func:`_watch_events_impl`.
    The dispatcher's JSON-Schema validator catches the coarse shape errors
    (missing ``since_cursor``, ``timeout_ms`` outside the integer range,
    extra top-level keys); the per-field re-checks here are the typed
    contract for the handler itself -- a future schema change wouldn't
    silently widen what the handler accepts.

    Tenant scoping is structural: ``arguments`` has no ``tenant_id`` field,
    so the stream key is always ``meho:feed:{operator.tenant_id}``.
    """
    since_cursor = arguments.get("since_cursor")
    if not isinstance(since_cursor, str) or not since_cursor:
        raise McpInvalidParamsError(
            "since_cursor: required, must be a non-empty Valkey stream cursor",
        )
    op_class, principal, target = _extract_filter(arguments)
    timeout_ms = _validate_timeout_ms(arguments.get("timeout_ms"))
    return await _watch_events_impl(
        operator,
        since_cursor=since_cursor,
        op_class=op_class,
        principal=principal,
        target=target,
        timeout_ms=timeout_ms,
    )


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.watch",
        description=(
            "Long-poll the operator's tenant broadcast stream for new "
            "events past 'since_cursor' (Initiative #1090, Task #1093). "
            "Issues XREAD BLOCK against meho:feed:{tenant_id} with "
            "'timeout_ms' as the block window (default 10000, cap "
            "30000 -- the chassis worker is tied up for up to that long, "
            "so the cap is a hard backpressure boundary, not a hint). "
            "Returns {events, next_cursor}. When no events arrive within "
            "the window: returns {events: [], next_cursor: <unchanged>} -- "
            "re-poll with the same cursor. When events arrive: "
            "'next_cursor' advances to the last *fetched* entry id (NOT "
            "the last matched one) so a busy-but-filtered tenant still "
            "progresses. Designed for the agent-side discipline loop: "
            "'broadcast_watch -> process -> broadcast_watch with new "
            "cursor'. Obtain the initial 'since_cursor' from "
            "'meho.broadcast.recent's 'next_cursor'. The 'filter' object "
            "narrows by exact-match op_class / principal (JWT 'sub') / "
            "target (target_name). Tenant scoping is structural -- every "
            "watch targets the operator's own tenant stream; there is no "
            "input that could request another tenant's stream. Payloads "
            "inherit the publisher-side redaction (credential_read + "
            "audit_query events are already aggregate-only on the stream). "
            "MCP 'resources/subscribe' (server-push) remains advertised "
            "as 'false' per backend/src/meho_backplane/mcp/resources/kb.py; "
            "this long-poll is the v0.2 pragmatic shape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "since_cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Valkey stream cursor ('1747800000000-0'). "
                        "Required. Obtain initial value from "
                        "meho.broadcast.recent's 'next_cursor'. "
                        "XREAD reads entries strictly past this cursor."
                    ),
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "op_class": {
                            "type": "string",
                            "enum": list(_OP_CLASS_ENUM),
                            "description": ("Exact-match filter on event op_class."),
                        },
                        "principal": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": ("Exact-match filter on JWT 'sub' claim."),
                        },
                        "target": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": ("Exact-match filter on event target_name."),
                        },
                    },
                    "additionalProperties": False,
                    "description": (
                        "Filter narrows the result; all sub-keys "
                        "optional. Each non-null sub-key is exact-match."
                    ),
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": _WATCH_MIN_TIMEOUT_MS,
                    "maximum": _WATCH_MAX_TIMEOUT_MS,
                    "default": _WATCH_DEFAULT_TIMEOUT_MS,
                    "description": (
                        "XREAD BLOCK window in milliseconds. Cap at "
                        f"{_WATCH_MAX_TIMEOUT_MS} (long-poll, not "
                        "infinite-block) to bound chassis worker tie-up."
                    ),
                },
            },
            "required": ["since_cursor"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_handler_watch,
)


# ===========================================================================
# === end meho.broadcast.watch ===
# ===========================================================================
