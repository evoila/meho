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
from typing import Any, Final, NamedTuple, cast
from uuid import UUID

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    MAX_TARGETS,
    OP_CLASS_ENUM,
    PLANNED_OP_CLASS_VALUES,
    TARGET_MAX_CHARS,
    TTL_MAX_MINUTES,
    TTL_MIN_MINUTES,
    WORK_REF_MAX_CHARS,
    AgentAnnouncementEvent,
    AnnounceRateLimitError,
    InvalidSinceError,
    PlannedOpClass,
    enforce_announce_rate_limit,
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
from meho_backplane.mcp.server import McpInvalidParamsError, McpRateLimitedError

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# ===========================================================================
# === meho.broadcast.recent ===
# ===========================================================================


class _BroadcastFilter(NamedTuple):
    """Typed view of the ``filter`` object shared by recent + watch."""

    op_class: str | None
    principal: str | None
    target: str | None
    actor_sub: str | None
    work_ref: str | None
    active_only: bool


def _extract_filter(arguments: dict[str, Any]) -> _BroadcastFilter:
    """Extract the ``filter.*`` sub-keys, asserting each field's type.

    The wire schema's ``filter`` object permits each sub-key to be
    omitted entirely OR set to its typed value (string for
    ``op_class`` / ``principal`` / ``target`` / ``actor_sub`` /
    ``work_ref``, boolean for ``active_only``). Anything else (a number,
    a list, an object) surfaces as JSON-RPC ``-32602`` -- the JSON
    Schema validator at the dispatcher layer catches structural
    violations; this helper picks the typed view for the handler.

    ``actor_sub`` narrows to a delegated agent's own operations -- the
    RFC 8693 actor, distinct from ``principal`` which matches the human
    subject (T3 #2545); ``work_ref`` narrows to any event linked to
    that opaque change-ticket reference; ``active_only`` (default
    ``False``) drops TTL'd claims whose ``expires_at`` has already
    elapsed.
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
    active_only = filter_obj.get("active_only", False)
    if not isinstance(active_only, bool):
        raise McpInvalidParamsError("filter.active_only: must be a boolean when provided")
    return _BroadcastFilter(op_class, principal, target, actor_sub, work_ref, active_only)


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
    flt = _extract_filter(arguments)
    raw_limit = arguments.get("limit", 100)
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        raise McpInvalidParamsError("limit: must be an integer in [1, 1000]")
    if raw_limit < 1 or raw_limit > 1000:
        raise McpInvalidParamsError("limit: must be in [1, 1000]")
    try:
        return await list_recent_events_strict(
            operator,
            since=since,
            op_class=flt.op_class,
            principal=flt.principal,
            target=flt.target,
            actor_sub=flt.actor_sub,
            work_ref=flt.work_ref,
            active_only=flt.active_only,
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
            "Returns {events, next_cursor}. Each event row carries "
            "'cursor' -- its Valkey stream entry id, usable as this "
            "tool's (or meho.broadcast.watch's) 'cursor' arg -- plus "
            "'id' as a legacy alias of the same stream cursor (NOT "
            "the event's UUID; that is 'event_id' on operation rows). "
            "Default window is the last "
            "30 minutes when 'cursor' is omitted; pass an ISO-8601 "
            "timestamp ('2026-05-25T10:00:00Z') or a Valkey stream "
            "cursor ('1747800000000-0') to override. The 'filter' "
            "object narrows by exact-match op_class / principal "
            "(JWT 'sub') / target (target_name or any of an "
            "announcement's 'targets') / actor_sub (the delegated agent) "
            "/ work_ref (change ticket), plus 'active_only' "
            "(drop expired TTL claims). 'limit' caps the "
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
                            "description": (
                                "Exact-match filter on target attribution. "
                                "Matches a BroadcastEvent's target_name or "
                                "an announcement's single 'target' OR any "
                                "entry in its 'targets' list."
                            ),
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
                            "maxLength": WORK_REF_MAX_CHARS,
                            "description": (
                                "Exact-match filter on an external "
                                "change-ticket reference (e.g. "
                                "'gh:evoila/meho#123'), grouping every event "
                                "tied to one ticket -- an announcement's "
                                "declared 'work_ref' OR an audit-driven "
                                "operation's projected lineage 'work_ref'."
                            ),
                        },
                        "active_only": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When true, drop TTL'd announcement claims "
                                "whose expires_at (ts + ttl_minutes) has "
                                "elapsed. Non-TTL events always pass."
                            ),
                        },
                    },
                    "additionalProperties": False,
                    "description": (
                        "Filter narrows the result; all sub-keys "
                        "optional. Each non-null sub-key is exact-match "
                        "(except 'active_only', a boolean mode)."
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


class _AnnounceClaims(NamedTuple):
    """Validated structured intent claims from an announce call."""

    targets: list[str]
    planned_op_class: PlannedOpClass | None
    ttl_minutes: int | None
    work_ref: str | None
    run_id: UUID | None


def _parse_targets(raw: Any) -> list[str]:
    """Validate the optional ``targets`` list; -32602 on any violation.

    Empty/absent → ``[]`` (the model's default). A non-list, an
    over-long list, or a non-string / out-of-bounds element rejects.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise McpInvalidParamsError("targets: must be an array of strings when provided")
    if len(raw) > MAX_TARGETS:
        raise McpInvalidParamsError(f"targets: at most {MAX_TARGETS} entries")
    for item in raw:
        if not isinstance(item, str) or not 1 <= len(item) <= TARGET_MAX_CHARS:
            raise McpInvalidParamsError(
                f"targets: each entry must be a string 1..{TARGET_MAX_CHARS} chars",
            )
    return cast("list[str]", raw)


def _parse_run_id(raw: Any) -> UUID | None:
    """Validate the optional ``run_id``; -32602 on a non-UUID string."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise McpInvalidParamsError("run_id: must be a UUID string when provided")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError("run_id: must be a valid UUID string") from exc


def _parse_announce_claims(arguments: dict[str, Any]) -> _AnnounceClaims:
    """Validate the optional structured-claim fields; -32602 on any miss.

    These are TRUSTED coordination fields (a bounded enum, a bounded
    int, a UUID, a list of bounded strings): the handler re-validates
    them belt-and-suspenders (the dispatcher's JSON-Schema runs first)
    so a direct handler call gets the same typed contract. Validated
    structured metadata cannot carry an injection, so these values are
    echoed back unwrapped on the response and served unwrapped on reads.
    """
    planned = arguments.get("planned_op_class")
    if planned is not None and planned not in PLANNED_OP_CLASS_VALUES:
        raise McpInvalidParamsError(
            f"planned_op_class: must be one of {list(PLANNED_OP_CLASS_VALUES)}",
        )
    ttl = arguments.get("ttl_minutes")
    if ttl is not None:
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise McpInvalidParamsError(
                f"ttl_minutes: must be an integer in [{TTL_MIN_MINUTES}, {TTL_MAX_MINUTES}]",
            )
        if ttl < TTL_MIN_MINUTES or ttl > TTL_MAX_MINUTES:
            raise McpInvalidParamsError(
                f"ttl_minutes: must be in [{TTL_MIN_MINUTES}, {TTL_MAX_MINUTES}]",
            )
    work_ref = arguments.get("work_ref")
    if work_ref is not None:
        if not isinstance(work_ref, str):
            raise McpInvalidParamsError("work_ref: must be a string when provided")
        if not 1 <= len(work_ref) <= WORK_REF_MAX_CHARS:
            raise McpInvalidParamsError(f"work_ref: must be 1..{WORK_REF_MAX_CHARS} chars")
    return _AnnounceClaims(
        targets=_parse_targets(arguments.get("targets")),
        planned_op_class=cast("PlannedOpClass | None", planned),
        ttl_minutes=ttl,
        work_ref=work_ref,
        run_id=_parse_run_id(arguments.get("run_id")),
    )


async def _publish_agent_announcement_impl(
    operator: Operator,
    *,
    activity: str,
    target: str | None,
    scope: str | None,
    phase: str,
    claims: _AnnounceClaims | None = None,
) -> dict[str, Any]:
    """Build + publish one :class:`AgentAnnouncementEvent`, return the ack.

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

    ``claims`` carries the optional structured intent fields (typed
    targets / planned_op_class / TTL / work_ref / run_id). When
    omitted, the announcement is a plain narrative event (the pre-v2
    shape). The declared (non-empty) claim fields are echoed back on
    the ack so the caller can confirm what landed.

    Tenant scoping is structural: the ``tenant_id`` on the constructed
    event is sourced exclusively from ``operator.tenant_id`` (the
    JWT-bound claim). The handler's input schema has no ``tenant_id``
    field; a cross-tenant announce is impossible by construction, not
    by check-then-reject.
    """
    claims = claims or _AnnounceClaims([], None, None, None, None)
    event = AgentAnnouncementEvent(
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        activity=activity,
        target=target,
        targets=claims.targets,
        scope=scope,
        planned_op_class=claims.planned_op_class,
        ttl_minutes=claims.ttl_minutes,
        work_ref=claims.work_ref,
        run_id=claims.run_id,
        phase=phase,  # type: ignore[arg-type]  # pydantic validates the Literal at construction
        ts=datetime.now(UTC),
    )
    entry_id = await publish_agent_announcement(event)
    # Both keys carry the Valkey stream entry id of the appended
    # announcement. ``cursor`` is the self-labelled canonical name
    # (round-trips through recent/watch's ``cursor`` arg, #2479);
    # ``event_id`` is the historical alias — a misnomer kept for wire
    # compatibility: it is the stream cursor, NOT a durable event
    # UUID (announcements carry no UUID today; #2547 mints one).
    ack: dict[str, Any] = {"event_id": entry_id, "cursor": entry_id}
    # Echo only the declared (non-empty) structured claims -- a plain
    # announce keeps the pre-v2 ``{event_id, cursor}`` ack shape. These
    # are trusted structured values, so they are echoed unwrapped.
    if claims.targets:
        ack["targets"] = claims.targets
    if claims.planned_op_class is not None:
        ack["planned_op_class"] = claims.planned_op_class
    if claims.ttl_minutes is not None:
        ack["ttl_minutes"] = claims.ttl_minutes
    if claims.work_ref is not None:
        ack["work_ref"] = claims.work_ref
    if claims.run_id is not None:
        ack["run_id"] = str(claims.run_id)
    return ack


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

    ``activity`` / ``scope`` / ``target`` / ``targets`` / ``work_ref``
    are agent-authored free text. The handler does NOT sanitise or
    rewrite them -- consumers MUST treat the strings as UNTRUSTED. This
    mirrors the :mod:`~meho_backplane.conventions.preamble` precedent
    for operator-authored convention bodies wrapped in
    ``<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>`` delimiters.
    The structured intent claims (``planned_op_class`` / ``ttl_minutes``
    / ``run_id``, plus ``phase`` / ``ts``) are the inverse: bounded
    enums, ints, UUIDs and timestamps, server-validated at the boundary,
    so they are served UNWRAPPED as trustworthy coordination data.
    See :class:`AgentAnnouncementEvent`'s docstring for the per-surface
    contract (frontend escapes, Slack plain-text, downstream agents
    must not interpret prose as policy).
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
    # Parse + validate the structured intent claims (T1 #2544) first, so
    # a malformed request from a looping caller still gets a clean
    # ``-32602`` rather than being masked by the rate-limit gate below.
    claims = _parse_announce_claims(arguments)
    # Per-principal flood control BEFORE the publish (G6.5-T6 #2546): the
    # tenant stream is count-trimmed (``BROADCAST_MAXLEN`` = 10000), so a
    # looping principal could otherwise evict the whole tenant's
    # coordination window in a burst. Runs after cheap arg validation but
    # before the expensive publish. Over-limit surfaces as a typed
    # ``-32000`` rate-limited error carrying the window details -- the
    # fail-loud announce contract is preserved (no silent drop).
    try:
        await enforce_announce_rate_limit(operator.tenant_id, operator.sub)
    except AnnounceRateLimitError as exc:
        raise McpRateLimitedError(
            str(exc),
            data={
                "limit": exc.limit,
                "window_seconds": exc.window_seconds,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        ) from exc
    return await _publish_agent_announcement_impl(
        operator,
        activity=activity,
        target=target,
        scope=scope,
        phase=phase_arg,
        claims=claims,
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
            "'update'). Optional STRUCTURED INTENT CLAIMS turn an "
            "announcement into coordination data other agents can trust: "
            "'targets' (up to 10 target names the work touches -- "
            "supersedes 'target' for multi-target claims), "
            "'planned_op_class' (the op class you are about to run, e.g. "
            "'write' / 'credential_read'), 'ttl_minutes' (1..1440; the "
            "claim's lifetime -- readers derive expires_at = ts + ttl "
            "and can filter to active claims), 'work_ref' (an opaque "
            "change-ticket reference such as 'gh:evoila/meho#123'), and "
            "'run_id' (the UUID of the agent run). These typed fields are "
            "server-validated and served UNWRAPPED (they cannot carry an "
            "injection); the free-text 'activity' / 'scope' / 'target' / "
            "'targets' / 'work_ref' stay quarantined behind the "
            "untrusted-content envelope on read. The publish is fail-loud "
            "-- a Valkey teardown surfaces as JSON-RPC -32603 Internal "
            "Error so the agent knows the announcement did not land "
            "(distinct from the audit-driven publisher which fail-opens). "
            "Tenant scoping is structural -- the input schema has no "
            "tenant_id argument; the event always writes to the "
            "operator's own tenant stream. Returns {event_id, cursor} "
            "plus any declared claim fields echoed back. Both event_id "
            "and cursor are the Valkey stream entry id of the appended "
            "announcement; 'cursor' is the canonical self-labelled name "
            "and round-trips through meho.broadcast.recent/watch's "
            "'cursor' arg for verification; 'event_id' is a legacy "
            "alias of the same stream cursor, NOT a durable event "
            "UUID (announcements carry no UUID). Announces are "
            "rate-limited per principal (default 10 per minute); "
            "exceeding the limit returns a -32000 error naming the "
            "window and a retry-after -- announce meaningful "
            "transitions, not a tight loop."
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
                    "maxLength": TARGET_MAX_CHARS,
                    "description": (
                        "Optional single target_name attribution -- the "
                        "managed target the activity scopes to "
                        "(e.g. 'prod-vc-1', 'kube-prod'). Omit when "
                        "the activity is target-less. Use 'targets' for "
                        "a multi-target claim. UNTRUSTED prose."
                    ),
                },
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": TARGET_MAX_CHARS,
                    },
                    "maxItems": MAX_TARGETS,
                    "description": (
                        "Optional list of target_names the claim touches "
                        f"(each 1..{TARGET_MAX_CHARS} chars, at most "
                        f"{MAX_TARGETS}). Supersedes 'target' for "
                        "multi-target work; recent/watch's target filter "
                        "matches an event when the query equals 'target' "
                        "OR appears in 'targets'. UNTRUSTED prose "
                        "(each element wrapped on read)."
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
                "planned_op_class": {
                    "type": "string",
                    "enum": list(PLANNED_OP_CLASS_VALUES),
                    "description": (
                        "Optional declared intent: the op class the "
                        "agent is about to run (the classify_op "
                        "taxonomy). TRUSTED structured metadata, served "
                        "unwrapped so peers can reason about crossfire."
                    ),
                },
                "ttl_minutes": {
                    "type": "integer",
                    "minimum": TTL_MIN_MINUTES,
                    "maximum": TTL_MAX_MINUTES,
                    "description": (
                        "Optional claim lifetime in minutes "
                        f"({TTL_MIN_MINUTES}..{TTL_MAX_MINUTES}). Readers "
                        "derive expires_at = ts + ttl and can drop "
                        "elapsed claims via recent/watch 'active_only'. "
                        "TRUSTED int, served unwrapped."
                    ),
                },
                "work_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": WORK_REF_MAX_CHARS,
                    "description": (
                        "Optional opaque change-ticket reference "
                        "(e.g. 'gh:evoila/meho#123'), same convention as "
                        "AgentRun.work_ref. Exact-match filterable on "
                        "recent/watch. UNTRUSTED prose (wrapped on read) "
                        "but filtered pre-wrap."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": (
                        "Optional UUID of the agent run this "
                        "announcement belongs to, so peers/humans can "
                        "group a run's announcements. TRUSTED UUID, "
                        "served unwrapped."
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
    active_only: bool,
) -> list[dict[str, Any]]:
    """Parse + filter the XREAD batch into the wire-shape events list.

    Each surviving entry carries the Valkey stream entry id twice --
    as ``cursor`` (self-labelled to match the ``cursor`` input arg it
    round-trips through, #2479) and as ``id`` (the historical alias)
    -- plus every :class:`BroadcastEvent` field
    (including the durable ``event_id`` UUID and the ``audit_id`` for
    audit-log correlation). Entries that fail :func:`parse_entry` (bad
    field shape, malformed JSON) are logged + skipped inside the helper
    so the watch handler never raises on a single bad entry.

    Serialisation goes through
    :func:`~meho_backplane.broadcast.history.dump_event_wire` so
    agent-authored announcement free-text (``activity`` / ``scope`` /
    ``target`` / ``targets`` / ``work_ref``) reaches the calling agent
    wrapped in the untrusted-content envelope — same
    stored-prompt-injection guard as the ``broadcast.recent`` path
    (evoila-bosnia/meho-internal#154). The structured claim fields
    (``planned_op_class`` / ``ttl_minutes`` / ``run_id`` / ``phase`` /
    ``ts`` / ``expires_at``) are served unwrapped.
    """
    now = datetime.now(UTC)
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
            active_only=active_only,
            now=now,
        ):
            continue
        matched.append({"id": entry_id, "cursor": entry_id, **dump_event_wire(event)})
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
    active_only: bool,
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
        active_only=active_only,
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
    flt = _extract_filter(arguments)
    timeout_ms = _validate_timeout_ms(arguments.get("timeout_ms"))
    return await _watch_events_impl(
        operator,
        since_cursor=since_cursor,
        op_class=flt.op_class,
        principal=flt.principal,
        target=flt.target,
        actor_sub=flt.actor_sub,
        work_ref=flt.work_ref,
        active_only=flt.active_only,
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
            "Returns {events, next_cursor}. Each event row carries "
            "'cursor' -- its Valkey stream entry id, usable as this "
            "tool's (or meho.broadcast.recent's) 'cursor' arg -- plus "
            "'id' as a legacy alias of the same stream cursor (NOT "
            "the event's UUID; that is 'event_id' on operation rows). "
            "When no events arrive within "
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
            "(JWT 'sub') / target (target_name or any of an "
            "announcement's 'targets') / actor_sub (the delegated agent) "
            "/ work_ref (change ticket), plus 'active_only' "
            "(drop expired TTL claims). Tenant scoping is "
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
                            "description": (
                                "Exact-match filter on target attribution. "
                                "Matches a BroadcastEvent's target_name or "
                                "an announcement's single 'target' OR any "
                                "entry in its 'targets' list."
                            ),
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
                            "maxLength": WORK_REF_MAX_CHARS,
                            "description": (
                                "Exact-match filter on an external "
                                "change-ticket reference (e.g. "
                                "'gh:evoila/meho#123'), grouping every event "
                                "tied to one ticket -- an announcement's "
                                "declared 'work_ref' OR an audit-driven "
                                "operation's projected lineage 'work_ref'."
                            ),
                        },
                        "active_only": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When true, drop TTL'd announcement claims "
                                "whose expires_at (ts + ttl_minutes) has "
                                "elapsed. Non-TTL events always pass."
                            ),
                        },
                    },
                    "additionalProperties": False,
                    "description": (
                        "Filter narrows the result; all sub-keys "
                        "optional. Each non-null sub-key is exact-match "
                        "(except 'active_only', a boolean mode)."
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
