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

import time
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent, get_broadcast_client
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
    event: BroadcastEvent,
    *,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> bool:
    """Return ``True`` iff *event* matches every non-``None`` filter.

    Exact-match semantics on each non-``None`` field; mirrors
    :func:`meho_backplane.api.v1.feed._passes_filter`. ``target_name`` is
    nullable on :class:`BroadcastEvent`; an event with no target
    attribution never passes a non-``None`` *target* filter (the
    caller explicitly asked for a target -- an untagged event doesn't
    qualify).
    """
    if op_class is not None and event.op_class != op_class:
        return False
    if principal is not None and event.principal_sub != principal:
        return False
    return not (target is not None and event.target_name != target)


def _parse_entry(
    entry_id: str,
    fields: dict[str, str],
    *,
    stream_key: str,
) -> BroadcastEvent | None:
    """Deserialise one ``XRANGE`` entry; log + skip on shape failure.

    Mirrors the safety net in
    :func:`meho_backplane.mcp.resources.tenant_feed._parse_entry`. The
    publisher always emits ``{"event": <json>}`` today; this branch
    is the forward-compat guard against a future Slack-mirror or
    third-party writer XADD'ing a different shape onto the same key.
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
        return BroadcastEvent.model_validate_json(raw_event_json)
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
