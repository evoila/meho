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

T1's xrange + filter + redact-aware parse loop lives in
:mod:`meho_backplane.broadcast.history`; the UI history route at
``/ui/broadcast/history`` uses the same body through its fail-soft
sibling wrapper (G6.4-T4 #1103). The parse primitives the watch tool
(T3) shares with T1 (``event_matches`` / ``parse_entry`` /
``stream_key``) also live in that module and are re-used here.

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
* Shared read-side helper (xrange + filter + redact-aware parse):
  :mod:`~meho_backplane.broadcast.history` (G6.4-T4 #1103).
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
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    OP_CLASS_ENUM,
    AgentAnnouncementEvent,
    InvalidSinceError,
    get_broadcast_blocking_client,
    list_recent_events_strict,
    publish_agent_announcement,
)
from meho_backplane.broadcast.history import (
    dump_event_wire,
    event_matches,
    parse_entry,
    stream_key,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# ===========================================================================
# === meho.broadcast.recent ===
# ===========================================================================


def _extract_filter(
    arguments: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Extract the five ``filter.*`` sub-keys, asserting string-or-absent.

    The wire schema's ``filter`` object permits each sub-key to be
    omitted entirely OR set to a string. Anything else (a number, a
    list, an object) surfaces as JSON-RPC ``-32602`` -- the JSON
    Schema validator at the dispatcher layer catches structural
    violations; this helper picks the typed view for the handler.

    ``actor_sub`` and ``work_ref`` are the lineage filters (T3 #2545):
    ``actor_sub`` narrows to a delegated agent's own work (the RFC 8693
    actor, distinct from ``principal`` which matches the human subject),
    ``work_ref`` groups events by external change ticket.
    """
    filter_obj = arguments.get("filter") or {}
    if not isinstance(filter_obj, dict):
        raise McpInvalidParamsError("filter: must be an object when provided")
    op_class = filter_obj.get("op_class")
    principal = filter_obj.get("principal")
    target = filter_obj.get("target")
    actor_sub = filter_obj.get("actor_sub")
    work_ref = filter_obj.get("work_ref")
    for name, value in (
        ("op_class", op_class),
        ("principal", principal),
        ("target", target),
        ("actor_sub", actor_sub),
        ("work_ref", work_ref),
    ):
        if value is not None and not isinstance(value, str):
            raise McpInvalidParamsError(f"filter.{name}: must be a string when provided")
    return op_class, principal, target, actor_sub, work_ref


async def _handler_recent(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho.broadcast.recent`` handler.

    Parses the wire arguments, delegates to
    :func:`~meho_backplane.broadcast.history.list_recent_events_strict`
    (the fail-loud wrapper -- a Valkey teardown propagates to the MCP
    dispatcher's ``-32603`` Internal Error path), and returns the
    JSON-shaped response the dispatcher wraps in the MCP ``content``
    envelope. Errors surface as :class:`McpInvalidParamsError`
    (-32602); the JSON Schema validator at the dispatcher already
    enforces the coarse shape (``limit`` range, ``filter`` object
    type) so the per-field re-checks here are belt-and-suspenders for
    the handler's typed contract.

    A malformed ``since`` arrives as :class:`InvalidSinceError` from
    the helper module and is mapped to ``-32602`` Invalid Params here
    -- the helper raises a generic domain error so the UI fail-soft
    counterpart can swallow non-RedisError teardowns selectively
    without taking the MCP error vocabulary as a dependency.

    Forward-cursor naming (G0.18-T5 #1358 RDC #789 N4)
    --------------------------------------------------

    ``cursor`` is the canonical forward-cursor parameter name across
    every MCP read tool that paginates (audit / topology / targets /
    operation groups). ``since`` remains accepted as a deprecated
    alias for backward compatibility with the v0.8.0 wire shape; the
    two are mutually exclusive (passing both is rejected). New
    callers SHOULD use ``cursor``.
    """
    cursor_arg = arguments.get("cursor")
    since_arg = arguments.get("since")
    if cursor_arg is not None and since_arg is not None:
        raise McpInvalidParamsError(
            "pass either `cursor` (canonical) or `since` (deprecated alias), not both",
        )
    since = cursor_arg if cursor_arg is not None else since_arg
    if since is not None and not isinstance(since, str):
        raise McpInvalidParamsError("cursor: must be a string when provided")
    op_class, principal, target, actor_sub, work_ref = _extract_filter(arguments)
    raw_limit = arguments.get("limit", 100)
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        raise McpInvalidParamsError("limit: must be an integer in [1, 1000]")
    if raw_limit < 1 or raw_limit > 1000:
        raise McpInvalidParamsError("limit: must be in [1, 1000]")
    try:
        return await list_recent_events_strict(
            operator,
            since=since,
            op_class=op_class,
            principal=principal,
            target=target,
            actor_sub=actor_sub,
            work_ref=work_ref,
            limit=raw_limit,
        )
    except InvalidSinceError as exc:
        raise McpInvalidParamsError(str(exc)) from exc


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.recent",
        description=(
            "Read the operator's tenant's recent broadcast events from "
            "meho:feed:{tenant_id} (Initiative #1090, Task #1091). "
            "Returns {events, next_cursor}. Default window is the last "
            "30 minutes when 'cursor' is omitted; pass an ISO-8601 "
            "timestamp ('2026-05-25T10:00:00Z') or a Valkey stream "
            "cursor ('1747800000000-0') to override. The 'filter' "
            "object narrows by exact-match op_class / principal "
            "(JWT 'sub') / target (target_name) / actor_sub (the "
            "delegated agent) / work_ref (change ticket). 'limit' caps the "
            "page at 1..1000 (default 100); the response's "
            "'next_cursor' round-trips as the next call's 'cursor' "
            "for gap-free pagination. 'since' is accepted as a "
            "deprecated alias for 'cursor' (v0.8.0 wire shape) and is "
            "mutually exclusive with it. Tenant scoping is structural -- "
            "every read targets the operator's own tenant stream; "
            "there is no input that could request another tenant's "
            "events. Payloads inherit the publisher-side redaction "
            "(credential_read + audit_query events are already "
            "aggregate-only on the stream)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Forward-pagination cursor. Either an ISO-8601 "
                        "timestamp ('2026-05-25T10:00:00Z') OR a Valkey "
                        "stream cursor ('1747800000000-0'). Omit for the "
                        "last 30 minutes. Canonical name; matches the "
                        "`cursor` parameter on `query_audit` / "
                        "`query_topology` / `list_targets`."
                    ),
                },
                "since": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "DEPRECATED alias for `cursor` (v0.8.0 wire "
                        "shape). Accepted for backward compatibility; "
                        "new callers SHOULD use `cursor`. Mutually "
                        "exclusive with `cursor`; passing both rejects "
                        "with -32602."
                    ),
                    "deprecated": True,
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "op_class": {
                            "type": "string",
                            "enum": list(OP_CLASS_ENUM),
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
                        "actor_sub": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "Exact-match filter on the RFC 8693 actor "
                                "(the delegated agent that acted). Answers "
                                "'what has this agent been doing' -- distinct "
                                "from 'principal', which matches the human "
                                "subject the agent acted for."
                            ),
                        },
                        "work_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "Exact-match filter on the external "
                                "change-ticket reference, grouping every "
                                "event tied to one ticket / CR / issue."
                            ),
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
            "events use, distinguished by kind='agent_announcement' "
            "(audit-driven events carry kind='operation' per "
            "docs/codebase/api-shape-conventions.md §6); other "
            "operators read it back via meho.broadcast.recent. "
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

    Reads via the long-timeout blocking client
    (:func:`get_broadcast_blocking_client`, ``socket_timeout=35 s``) so
    a 30 s BLOCK against a quiet stream returns ``None`` (the natural
    "nothing for us" branch :func:`_xread_items_or_none` already
    handles) instead of raising ``redis.TimeoutError`` from the socket
    layer at 5 s -- the spurious ``feed_error`` shape catalogued in
    RDC #789 N1 / Initiative #1353 on the SSE surface. See
    :mod:`meho_backplane.broadcast.client` for the two-client rationale.
    """
    client = get_broadcast_blocking_client()
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
    actor_sub: str | None,
    work_ref: str | None,
) -> list[dict[str, Any]]:
    """Parse + filter the XREAD batch into the wire-shape events list.

    Each surviving entry carries ``id`` (the Valkey stream cursor, the
    round-trip handle) plus every :class:`BroadcastEvent` field
    (including the durable ``event_id`` UUID and the ``audit_id`` for
    audit-log correlation). Entries that fail :func:`parse_entry` (bad
    field shape, malformed JSON) are logged + skipped inside the helper
    so the watch handler never raises on a single bad entry.

    Serialisation goes through
    :func:`~meho_backplane.broadcast.history.dump_event_wire` so
    agent-authored announcement free-text (``activity`` / ``scope`` /
    ``target``) reaches the calling agent wrapped in the
    untrusted-content envelope — same stored-prompt-injection guard as
    the ``broadcast.recent`` path (evoila-bosnia/meho-internal#154).
    """
    matched: list[dict[str, Any]] = []
    for entry_id, fields in items:
        event = parse_entry(entry_id, fields, stream_key=stream_key)
        if event is None:
            continue
        if not event_matches(
            event,
            op_class=op_class,
            principal=principal,
            target=target,
            actor_sub=actor_sub,
            work_ref=work_ref,
        ):
            continue
        matched.append({"id": entry_id, **dump_event_wire(event)})
    return matched


async def _watch_events_impl(
    operator: Operator,
    *,
    since_cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    actor_sub: str | None,
    work_ref: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    """Long-poll the operator's tenant stream for entries strictly past *since_cursor*.

    Issues a single ``XREAD BLOCK`` against ``meho:feed:{operator.tenant_id}``
    with ``timeout_ms`` as the block window; the caller's chassis worker
    is tied up for up to that long. When entries arrive, advances the
    cursor to the **last fetched** entry id (NOT the last *matched* one --
    same invariant as
    :func:`~meho_backplane.broadcast.history._list_recent_events_core`) so a filter-heavy
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
    key = stream_key(operator.tenant_id)
    raw_response = await _xread_or_cancelled(
        operator,
        stream_key=key,
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
        stream_key=key,
        op_class=op_class,
        principal=principal,
        target=target,
        actor_sub=actor_sub,
        work_ref=work_ref,
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

    Forward-cursor naming (G0.18-T5 #1358 RDC #789 N4)
    --------------------------------------------------

    ``cursor`` is the canonical forward-cursor parameter name across
    every MCP read tool that paginates (audit / topology / targets /
    operation groups / broadcast.recent). ``since_cursor`` remains
    accepted as a deprecated alias for the v0.8.0 wire shape; the
    two are mutually exclusive (passing both is rejected). New
    callers SHOULD use ``cursor``.
    """
    cursor_arg = arguments.get("cursor")
    since_cursor_arg = arguments.get("since_cursor")
    if cursor_arg is not None and since_cursor_arg is not None:
        raise McpInvalidParamsError(
            "pass either `cursor` (canonical) or `since_cursor` (deprecated alias), not both",
        )
    since_cursor = cursor_arg if cursor_arg is not None else since_cursor_arg
    if not isinstance(since_cursor, str) or not since_cursor:
        raise McpInvalidParamsError(
            "cursor: required, must be a non-empty Valkey stream cursor",
        )
    op_class, principal, target, actor_sub, work_ref = _extract_filter(arguments)
    timeout_ms = _validate_timeout_ms(arguments.get("timeout_ms"))
    return await _watch_events_impl(
        operator,
        since_cursor=since_cursor,
        op_class=op_class,
        principal=principal,
        target=target,
        actor_sub=actor_sub,
        work_ref=work_ref,
        timeout_ms=timeout_ms,
    )


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.watch",
        description=(
            "Long-poll the operator's tenant broadcast stream for new "
            "events past 'cursor' (Initiative #1090, Task #1093). "
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
            "cursor'. Obtain the initial 'cursor' from "
            "'meho.broadcast.recent's 'next_cursor'. 'since_cursor' is "
            "accepted as a deprecated alias for 'cursor' (v0.8.0 wire "
            "shape) and is mutually exclusive with it. The 'filter' "
            "object narrows by exact-match op_class / principal "
            "(JWT 'sub') / target (target_name) / actor_sub (the "
            "delegated agent) / work_ref (change ticket). Tenant scoping is "
            "structural -- every watch targets the operator's own tenant "
            "stream; there is no input that could request another "
            "tenant's stream. Payloads inherit the publisher-side "
            "redaction (credential_read + audit_query events are already "
            "aggregate-only on the stream). MCP 'resources/subscribe' "
            "(server-push) remains advertised as 'false' per "
            "backend/src/meho_backplane/mcp/resources/kb.py; "
            "this long-poll is the v0.2 pragmatic shape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Valkey stream cursor ('1747800000000-0'). "
                        "Required (either as `cursor` or via the "
                        "deprecated `since_cursor` alias). Obtain "
                        "initial value from `meho.broadcast.recent`'s "
                        "`next_cursor`. XREAD reads entries strictly "
                        "past this cursor. Canonical name; matches "
                        "the `cursor` parameter on `query_audit` / "
                        "`query_topology` / `list_targets`."
                    ),
                },
                "since_cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "DEPRECATED alias for `cursor` (v0.8.0 wire "
                        "shape). Accepted for backward compatibility; "
                        "new callers SHOULD use `cursor`. Mutually "
                        "exclusive with `cursor`; passing both rejects "
                        "with -32602."
                    ),
                    "deprecated": True,
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "op_class": {
                            "type": "string",
                            "enum": list(OP_CLASS_ENUM),
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
                        "actor_sub": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "Exact-match filter on the RFC 8693 actor "
                                "(the delegated agent that acted). Answers "
                                "'what has this agent been doing' -- distinct "
                                "from 'principal', which matches the human "
                                "subject the agent acted for."
                            ),
                        },
                        "work_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "Exact-match filter on the external "
                                "change-ticket reference, grouping every "
                                "event tied to one ticket / CR / issue."
                            ),
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
            # Either `cursor` (canonical) OR `since_cursor` (deprecated
            # alias) must be supplied; the handler enforces the XOR
            # (passing both rejects with -32602).
            "anyOf": [
                {"required": ["cursor"]},
                {"required": ["since_cursor"]},
            ],
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
