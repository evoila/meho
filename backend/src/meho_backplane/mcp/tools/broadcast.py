# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-facing MCP tools for the per-tenant announcement feed.

G6.4 Initiative (#1090) ships three agent-facing MCP tools that mirror
the four-step ``broadcast discipline`` documented in the Layer-2
consumer-onboarding template (PR #1028 / Initiative #229 G7.1):

* T1 (#1091, this module) -- ``meho_broadcast_recent``: read archived
  agent announcements with optional time-window and filter narrowing.
  Returns ``{events, next_cursor, watch_checkpoint, gap}`` (#3748).
* T2 (#1092, follow-on) -- ``meho_broadcast_announce``: agent-authored
  intent / completion publish.
* T3 (#1093, follow-on) -- ``meho_broadcast_watch``: long-poll for
  announcements after a watch checkpoint (``{events, next_cursor,
  gap}``).

Since #3748 both read tools read the ``agent_announcement`` archive by
its commit-order sequence number
(:mod:`meho_backplane.broadcast.archive_reader`); the Valkey stream only
wakes watchers. Operation events are not returned; ``query_audit`` is
their record.

File-layout convention
======================

All three tools share this file by design (mirroring the precedent set
by :mod:`~meho_backplane.mcp.tools.broadcast_overrides` -- three
broadcast-namespaced tools in one file). Each tool's section is bounded
by ``# === <tool-name> ===`` / ``# === end <tool-name> ===`` markers so
T2 and T3 can land in parallel branches with minimal merge-conflict
surface against this file's body.

Untrusted text
==============

Agent-authored free text (``activity`` / ``scope`` / ``target`` /
``targets`` / ``work_ref``) reaches the reading agent wrapped in the
untrusted-content envelope (:func:`~meho_backplane.broadcast.history.dump_event_wire`).

Tenant scoping is structural
============================

Every read is scoped to ``operator.tenant_id`` (the verified JWT
claim). The MCP tool's input schema has NO ``tenant_id`` argument, and
every cursor is bound to the tenant that issued it; a cursor from
another tenant is refused.

References
----------

* Parent Initiative: #1090.
* Parent Goal: #217.
* Stream key + redaction contract:
  :mod:`~meho_backplane.broadcast.events`.
* Archive reader: :mod:`~meho_backplane.broadcast.archive_reader` (#3748).
* Sibling tool registration shape:
  :mod:`~meho_backplane.mcp.tools.broadcast_overrides`.
* Sibling resource (poll-only snapshot of latest 50):
  :mod:`~meho_backplane.mcp.resources.tenant_feed`.
* MCP 2025-06-18 Tools:
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
* Valkey XREAD (the watch wake-up): https://valkey.io/commands/xread/
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, NamedTuple, cast
from uuid import UUID

import structlog

from meho_backplane.auth.agent_admission import (
    AgentAdmission,
    AgentIdentityRequiredError,
    resolve_agent_admission,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    IDEMPOTENCY_KEY_MAX_CHARS,
    MAX_TARGETS,
    OP_CLASS_ENUM,
    PLANNED_OP_CLASS_VALUES,
    TARGET_MAX_CHARS,
    TTL_DEFAULT_MINUTES,
    TTL_MAX_MINUTES,
    TTL_MIN_MINUTES,
    WORK_REF_MAX_CHARS,
    AnnouncementClaimError,
    AnnouncementFilter,
    AnnounceRateLimitError,
    InvalidArchiveCursorError,
    PlannedOpClass,
    build_trusted_announcement,
    encode_watch_checkpoint,
    enforce_announce_rate_limit,
    publish_agent_announcement,
    read_announcement_history,
    watch_announcements,
)
from meho_backplane.broadcast.announcement_claims import Phase
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError, McpRateLimitedError

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# ===========================================================================
# === meho_broadcast_recent ===
# ===========================================================================


def _extract_filter(arguments: dict[str, Any]) -> AnnouncementFilter:
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
    return AnnouncementFilter(op_class, principal, target, actor_sub, work_ref, active_only)


async def _handler_recent(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho_broadcast_recent`` handler.

    Parses the wire arguments and delegates to
    :func:`~meho_backplane.broadcast.archive_reader.read_announcement_history`,
    which reads the announcement archive by commit order (#3748). A bad
    ``cursor`` maps to ``-32602``; an archive (DB) error propagates to the
    dispatcher's ``-32603`` path.

    ``cursor`` is the canonical forward-cursor parameter name across
    every MCP read tool that paginates (G0.18-T5 #1358). ``since``
    remains accepted as a deprecated alias; the two are mutually
    exclusive (passing both is rejected).
    """
    cursor_arg = arguments.get("cursor")
    since_arg = arguments.get("since")
    if cursor_arg is not None and since_arg is not None:
        raise McpInvalidParamsError(
            "pass either `cursor` (canonical) or `since` (deprecated alias), not both",
        )
    cursor = cursor_arg if cursor_arg is not None else since_arg
    if cursor is not None and not isinstance(cursor, str):
        raise McpInvalidParamsError("cursor: must be a string when provided")
    flt = _extract_filter(arguments)
    raw_limit = arguments.get("limit", 100)
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        raise McpInvalidParamsError("limit: must be an integer in [1, 1000]")
    if raw_limit < 1 or raw_limit > 1000:
        raise McpInvalidParamsError("limit: must be in [1, 1000]")
    try:
        return await read_announcement_history(operator, cursor=cursor, flt=flt, limit=raw_limit)
    except InvalidArchiveCursorError as exc:
        raise McpInvalidParamsError(str(exc)) from exc


register_mcp_tool(
    definition=ToolDefinition(
        feature="broadcast",
        name="meho_broadcast_recent",
        surface=ToolSurface.WORKING,
        description=(
            "Read the agent announcements in the operator's tenant: what "
            "other agents said they are starting, doing, or have finished. "
            "WHEN TO CALL: as a first reflex at the start of a working "
            "session — before your first `call_operation` — so you don't "
            "collide with in-flight work; re-read it before picking up a "
            "new target. This is proactive coordination, not only a "
            "reaction to an explicit 'what is happening' request. "
            "MEHO stores every announcement before it is sent out, and "
            "this tool reads that store in the order the announcements "
            "were saved. Operation events are not included; use "
            "`query_audit` for what was run. "
            "Returns {events, next_cursor, watch_checkpoint, gap}, events "
            "oldest first; dedupe by each event's 'event_id'. Default "
            "window is the last 30 minutes; pass an ISO-8601 time "
            "('2026-05-25T10:00:00Z') as 'cursor' to start earlier. "
            "'next_cursor' continues this same read (pass it back as "
            "'cursor'); it is null when you have read everything up to "
            "the moment of the first call. 'watch_checkpoint' is where "
            "`meho_broadcast_watch` should start so you miss nothing that "
            "comes after this read; it is set even when no event matched "
            "your filter. 'gap' is null, or says that older announcements "
            "were removed by archive retention, so the answer is not "
            "complete. Both cursors only work in your own tenant. The "
            "'filter' object narrows by exact-match principal (JWT 'sub') "
            "/ target (an announcement's 'target' or any of its 'targets') "
            "/ work_ref (change ticket), plus 'active_only' (drop expired "
            "claims). 'op_class' and 'actor_sub' match operation events "
            "only, so with them no announcement matches. 'limit' caps the "
            "rows read per page at 1..1000 (default 100); filtering runs "
            "after the read, so a page can hold fewer events. 'since' is "
            "a deprecated alias for 'cursor' and is mutually exclusive "
            "with it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Either an ISO-8601 time ('2026-05-25T10:00:00Z') "
                        "to start the window, OR the 'next_cursor' of an "
                        "earlier page to continue that read. Omit for the "
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
                        "Max archive rows read in one page. The filter runs after the read."
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
# === end meho_broadcast_recent ===
# ===========================================================================


# ===========================================================================
# === meho_broadcast_announce ===
# ===========================================================================


class _AnnounceClaims(NamedTuple):
    """Validated structured intent claims from an announce call."""

    targets: list[str]
    planned_op_class: PlannedOpClass | None
    ttl_minutes: int | None
    work_ref: str | None
    run_id: UUID | None
    claim_id: UUID | None = None
    idempotency_key: str | None = None


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


def _parse_uuid_arg(name: str, raw: Any) -> UUID | None:
    """Validate an optional UUID argument; -32602 on a non-UUID string."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise McpInvalidParamsError(f"{name}: must be a UUID string when provided")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError(f"{name}: must be a valid UUID string") from exc


def _parse_idempotency_key(raw: Any) -> str | None:
    """Validate the optional ``idempotency_key``; -32602 on any miss."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not 1 <= len(raw) <= IDEMPOTENCY_KEY_MAX_CHARS:
        raise McpInvalidParamsError(
            f"idempotency_key: must be a string 1..{IDEMPOTENCY_KEY_MAX_CHARS} chars",
        )
    return raw


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
        run_id=_parse_uuid_arg("run_id", arguments.get("run_id")),
        claim_id=_parse_uuid_arg("claim_id", arguments.get("claim_id")),
        idempotency_key=_parse_idempotency_key(arguments.get("idempotency_key")),
    )


async def _publish_agent_announcement_impl(
    operator: Operator,
    admission: AgentAdmission,
    *,
    activity: str,
    target: str | None,
    scope: str | None,
    phase: str,
    claims: _AnnounceClaims | None = None,
) -> dict[str, Any]:
    """Build + publish one trusted announcement, return the ack.

    Internal helper for ``meho_broadcast_announce``. Kept separate from
    :func:`_handler_announce` so tests can drive the build-and-publish
    step with a directly-constructed :class:`Operator` and
    :class:`~meho_backplane.auth.agent_admission.AgentAdmission`.

    The server stamps every identity field from *admission* (#3736):
    principal, owner, actor, run, and (inside a hosted run) the work
    reference. A ``start`` gets a fresh ``claim_id``; a missing
    ``ttl_minutes`` becomes :data:`TTL_DEFAULT_MINUTES`. Claim rule
    failures surface as ``-32602`` with ``error.data.code`` set to the
    rule's stable code.

    Tenant scoping is structural: the ``tenant_id`` on the constructed
    event is sourced exclusively from ``operator.tenant_id`` (the
    JWT-bound claim). The handler's input schema has no ``tenant_id``
    field; a cross-tenant announce is impossible by construction, not
    by check-then-reject.
    """
    claims = claims or _AnnounceClaims([], None, None, None, None)
    try:
        event = build_trusted_announcement(
            admission,
            tenant_id=operator.tenant_id,
            activity=activity,
            target=target,
            targets=claims.targets,
            scope=scope,
            planned_op_class=claims.planned_op_class,
            ttl_minutes=claims.ttl_minutes,
            work_ref=claims.work_ref,
            run_id=claims.run_id,
            phase=cast("Phase", phase),
            claim_id=claims.claim_id,
            idempotency_key=claims.idempotency_key,
            ts=datetime.now(UTC),
        )
        result = await publish_agent_announcement(event, idempotency_key=claims.idempotency_key)
    except AnnouncementClaimError as exc:
        raise McpInvalidParamsError(str(exc), data={"code": exc.code}) from exc
    stored = result.event
    # ``event_id`` is the per-announcement UUID (the durable row's PK);
    # ``watch_checkpoint`` points just after this announcement in the
    # archive order (#3748); ``delivery_pending`` says the archive row is
    # stored but the stream wake-up failed. The stamped identity and claim
    # fields are trusted structured values, echoed unwrapped.
    ack: dict[str, Any] = {
        "event_id": str(stored.event_id),
        "watch_checkpoint": (
            None if result.seq is None else encode_watch_checkpoint(operator.tenant_id, result.seq)
        ),
        "delivery_pending": result.delivery_pending,
        "claim_id": str(stored.claim_id),
        "phase": stored.phase,
        "owner_sub": stored.owner_sub,
        "admission": stored.admission,
        "ttl_minutes": stored.ttl_minutes,
        "replayed": result.replayed,
    }
    if stored.targets:
        ack["targets"] = list(stored.targets)
    if stored.planned_op_class is not None:
        ack["planned_op_class"] = stored.planned_op_class
    if stored.work_ref is not None:
        ack["work_ref"] = stored.work_ref
    if stored.run_id is not None:
        ack["run_id"] = str(stored.run_id)
    if stored.actor_sub is not None:
        ack["actor_sub"] = stored.actor_sub
    return ack


async def _handler_announce(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho_broadcast_announce`` handler.

    Validates the wire arguments belt-and-suspenders (the dispatcher's
    JSON-Schema validator runs first, but the per-field re-checks here
    preserve the typed contract a direct ``_handler_announce`` call
    site -- e.g. a unit test -- would otherwise miss). Surfaces every
    validation failure as :class:`McpInvalidParamsError` (``-32602``).

    On a clean parse, delegates to
    :func:`_publish_agent_announcement_impl` for the construct +
    publish step. The publisher is **fail-loud** -- a Valkey teardown
    propagates as :class:`Exception` and the MCP dispatcher's generic
    handler maps it to ``-32603`` Internal Error (see the publisher
    module's docstring); the agent explicitly emitted the announcement and
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
    phase_arg = arguments.get("phase", "start")
    if phase_arg not in ("start", "update", "completion"):
        raise McpInvalidParamsError(
            "phase: must be one of 'start', 'update', 'completion'",
        )
    # Parse + validate the structured intent claims (T1 #2544) first, so
    # a malformed request from a looping caller still gets a clean
    # ``-32602`` rather than being masked by the rate-limit gate below.
    claims = _parse_announce_claims(arguments)
    # Trusted admission (#3736): only a checked agent identity publishes.
    # A person's own token gets the stable ``agent_identity_required``
    # error with the way to get a trusted identity.
    try:
        admission = await resolve_agent_admission(operator)
    except AgentIdentityRequiredError as exc:
        raise McpInvalidParamsError(
            str(exc),
            data={"code": exc.code, "reason": exc.reason, "remediation": exc.remediation},
        ) from exc
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
        admission,
        activity=activity,
        target=target,
        scope=scope,
        phase=phase_arg,
        claims=claims,
    )


register_mcp_tool(
    definition=ToolDefinition(
        feature="broadcast",
        name="meho_broadcast_announce",
        surface=ToolSurface.WORKING,
        description=(
            "Publish an agent-authored announcement to the operator's "
            "tenant broadcast stream (Initiative #1090, Task #1092). "
            "Use this to surface intent ('about to investigate cluster "
            "X latency'), progress updates ('halfway through the "
            "vCenter migration'), or completion summaries ('finished -- "
            "rolled back to snapshot S'). Call this proactively — before "
            "executing any operation that mutates infrastructure (check "
            "the hit's `safety_level` / `requires_approval` from "
            "`search_operations`), announce your intent so peers see the "
            "claim before you act, and again on completion — not only "
            "when the operator asks you to broadcast. The announcement "
            "lands on "
            "the same meho:feed:{tenant_id} stream that audit-driven "
            "events use, distinguished by kind='agent_announcement' "
            "(audit-driven events carry kind='operation' per "
            "docs/codebase/api-shape-conventions.md §6); other "
            "operators read it back via meho_broadcast_recent. "
            "ONLY A TRUSTED AGENT CAN PUBLISH: a call from inside a "
            "MEHO-hosted agent run, or a call with the token of a "
            "registered agent principal (`meho agent-principal "
            "register`). Any other caller, such as a person's own login, "
            "gets -32602 with error.data.code='agent_identity_required' "
            "and a remediation. People can still read the feed. "
            "EACH ANNOUNCEMENT IS ONE STEP OF A CLAIM: 'phase'='start' "
            "(the default) opens a claim and the server returns a new "
            "'claim_id'; 'update' and 'completion' continue it and need "
            "that 'claim_id' plus an 'idempotency_key' (a retry with the "
            "same key returns the first result with replayed=true). Only "
            "the claim's owner may update or complete it, and a completed "
            "claim takes no more steps. A claim is advisory: it never "
            "locks a target or blocks anyone. The server sets the owner, "
            "actor, run_id and (in a hosted run) work_ref; do not send "
            "them. "
            "'activity' is mandatory (1..500 chars), 'target' and "
            "'scope' are optional free-form attribution. "
            "Optional STRUCTURED INTENT CLAIMS turn an "
            "announcement into coordination data other agents can trust: "
            "'targets' (up to 10 target names the work touches -- "
            "supersedes 'target' for multi-target claims), "
            "'planned_op_class' (the op class you are about to run, e.g. "
            "'write' / 'credential_read'), 'ttl_minutes' (1..1440, "
            f"default {TTL_DEFAULT_MINUTES}; the "
            "claim's lifetime -- readers derive expires_at = ts + ttl "
            "and can filter to active claims), and 'work_ref' (an opaque "
            "change-ticket reference such as 'gh:evoila/meho#123'; set by "
            "the server inside a hosted run). These typed fields are "
            "server-validated and served UNWRAPPED (they cannot carry an "
            "injection); the free-text 'activity' / 'scope' / 'target' / "
            "'targets' / 'work_ref' stay quarantined behind the "
            "untrusted-content envelope on read. "
            "MEHO first stores the announcement; that is the record "
            "meho_broadcast_recent and meho_broadcast_watch read. If "
            "storing fails you get JSON-RPC -32603 and nothing was "
            "stored. If storing works but the live wake-up for watchers "
            "fails, the call still succeeds with delivery_pending=true: "
            "readers see the announcement anyway; retry with the same "
            "'idempotency_key' to finish the wake-up (no second copy is "
            "stored). Tenant scoping is structural -- the input schema "
            "has no tenant_id argument. Returns {event_id, "
            "watch_checkpoint, delivery_pending, claim_id, phase, "
            "owner_sub, admission, ttl_minutes, replayed} plus any other "
            "claim fields echoed back. 'event_id' is the announcement's "
            "UUID (it matches 'event_id' on recent/watch reads); "
            "'watch_checkpoint' is a meho_broadcast_watch cursor just "
            "after this announcement. Announces are "
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
                        f"({TTL_MIN_MINUTES}..{TTL_MAX_MINUTES}, default "
                        f"{TTL_DEFAULT_MINUTES}). Readers "
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
                        "recent/watch. Inside a hosted run the server "
                        "sets it from the run; a different value is "
                        "refused. UNTRUSTED prose (wrapped on read) "
                        "but filtered pre-wrap."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": (
                        "DEPRECATED. The server sets the run id from the "
                        "checked agent run. A value that is not that run "
                        "is refused with error.data.code="
                        "'server_stamped_field'. Do not send it."
                    ),
                    "deprecated": True,
                },
                "claim_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": (
                        "The claim this step continues. Required for "
                        "'update' and 'completion' (use the claim_id the "
                        "'start' returned); not allowed on 'start'."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": IDEMPOTENCY_KEY_MAX_CHARS,
                    "description": (
                        "Your retry key for this step. Required for "
                        "'update' and 'completion', optional for 'start'. "
                        "Sending the same key again returns the first "
                        "result (replayed=true) and stores nothing new. If "
                        "the first try was stored but its live wake-up "
                        "failed (delivery_pending=true), the retry "
                        "finishes the wake-up."
                    ),
                },
                "phase": {
                    "type": "string",
                    "enum": ["start", "update", "completion"],
                    "default": "start",
                    "description": (
                        "Claim step. 'start' (the default) opens a new "
                        "claim; 'update' reports progress on it; "
                        "'completion' closes it. The Prometheus counter "
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
# === end meho_broadcast_announce ===
# ===========================================================================

# ===========================================================================
# === meho_broadcast_watch ===
# ===========================================================================

#: Default wait window when the caller omits ``timeout_ms``. 10s
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


async def _handler_watch(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """``meho_broadcast_watch`` handler.

    Parses the wire arguments, then delegates to
    :func:`~meho_backplane.broadcast.archive_reader.watch_announcements`
    (#3748): archive scans after the checkpoint, with the Valkey stream
    only as the wake-up and 5-second polling when Valkey fails.
    The dispatcher's JSON-Schema validator catches the coarse shape errors
    (missing ``since_cursor``, ``timeout_ms`` outside the integer range,
    extra top-level keys); the per-field re-checks here are the typed
    contract for the handler itself -- a future schema change wouldn't
    silently widen what the handler accepts.

    Tenant scoping is structural: ``arguments`` has no ``tenant_id`` field,
    and a checkpoint from another tenant is refused with ``-32602``.

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
    checkpoint = cursor_arg if cursor_arg is not None else since_cursor_arg
    if not isinstance(checkpoint, str) or not checkpoint:
        raise McpInvalidParamsError(
            "cursor: required, must be a non-empty watch checkpoint",
        )
    flt = _extract_filter(arguments)
    timeout_ms = _validate_timeout_ms(arguments.get("timeout_ms"))
    try:
        return await watch_announcements(
            operator, checkpoint=checkpoint, flt=flt, timeout_ms=timeout_ms
        )
    except InvalidArchiveCursorError as exc:
        raise McpInvalidParamsError(str(exc)) from exc


register_mcp_tool(
    definition=ToolDefinition(
        feature="broadcast",
        name="meho_broadcast_watch",
        surface=ToolSurface.WORKING,
        description=(
            "Wait for new agent announcements in the operator's tenant "
            "after 'cursor' (a watch checkpoint). Get the first checkpoint "
            "from `meho_broadcast_recent`'s 'watch_checkpoint' (or from "
            "the 'watch_checkpoint' of your own announce), then pass each "
            "answer's 'next_cursor' to the next call. Returns {events, "
            "next_cursor, gap}. MEHO reads its stored announcements after "
            "the checkpoint first; if there are none it waits up to "
            "'timeout_ms' (default 10000, cap 30000) for a new one and "
            "reads again. 'next_cursor' always moves past everything "
            "read, also when your filter hid every event, so you never "
            "get the same event twice when you pass it back. If the live "
            "wake-up is down, MEHO checks every 5 seconds instead, so "
            "nothing is lost, only later. 'gap' is null, or says that "
            "announcements after your checkpoint were removed by archive "
            "retention ('retention_trimmed', with 'missed_events'), or "
            "that the checkpoint is ahead of the archive "
            "('checkpoint_ahead'; watching restarts at the current end). "
            "Dedupe by 'event_id'. Same 'filter' as "
            "`meho_broadcast_recent`. Operation events are not included. "
            "'since_cursor' is a deprecated alias for 'cursor' and is "
            "mutually exclusive with it. A checkpoint only works in the "
            "tenant that issued it. This is a single bounded long-poll, "
            "not a background subscription."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Watch checkpoint. Required (either as `cursor` "
                        "or via the deprecated `since_cursor` alias). "
                        "Get the first one from `meho_broadcast_recent`'s "
                        "`watch_checkpoint`; then pass each answer's "
                        "`next_cursor`. Returns announcements stored "
                        "after this point."
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
                        "How long to wait for a new announcement, in "
                        f"milliseconds. Cap at {_WATCH_MAX_TIMEOUT_MS} "
                        "(long-poll, not infinite-block) to bound chassis "
                        "worker tie-up."
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
# === end meho_broadcast_watch ===
# ===========================================================================
