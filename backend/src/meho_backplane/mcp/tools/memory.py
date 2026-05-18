# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``search_memory`` + ``add_to_memory`` — memory meta-tools (G5.1-T3).

Two of the ~17 agent-facing meta-tools defined by ``CLAUDE.md`` postulate
5 (Memory family). Both call into
:class:`~meho_backplane.memory.service.MemoryService` (G5.1-T1, #421);
the matching REST routes are owned by the sibling Task #422 and the CLI
verbs by #424 — every front-end converges on the same service layer,
which carries the per-scope RBAC matrix and the tenant boundary in one
place so the MCP surface here stays a thin shell.

Why this surface is load-bearing
================================

Per consumer-needs.md §G5 L131: "The single biggest unlock from a synced
MEHO isn't speed — it's that the *team* becomes the unit of memory, not
the individual operator." Every agent session that learns something
worth retaining (operator preference, tenant convention, target gotcha)
calls ``add_to_memory``; every agent session that needs to recall those
learnings calls ``search_memory``. That cadence makes the **tool
descriptions** the single most leveraged piece of agent UX in the memory
surface. The descriptions follow the AI-engineering anchor (name what,
when to use, when NOT to use) and explicitly cross-reference G5.2's
default 7-day TTL on user-scope writes so an agent learns the lifecycle
contract from the tool definition itself rather than guessing.

CLAUDE.md note on #332 explicitly carves the agent surface: ``forget``
and ``list`` are CLI-only conveniences and intentionally absent from
the MCP tool list. An agent reaches list-style queries via
``search_memory(scope=...)``; ``forget`` is a deliberate-write op
normally driven by operators, not the agent without explicit
confirmation.

RBAC + tenant scoping
=====================

Both tools require ``operator`` role at the registry layer. The
per-scope RBAC matrix is enforced inside
:class:`~meho_backplane.memory.service.MemoryService` (see
:class:`~meho_backplane.memory.rbac.MemoryRbacResolver`): user-scoped
writes are open to any operator (the service binds ``operator.sub``
server-side); ``tenant`` writes require ``tenant_admin``; ``target``
writes are open to any operator in the tenant (G0.3-T3 will tighten
later). A write that fails the matrix surfaces as
:class:`PermissionDeniedError` from the service, which this handler
re-raises as :class:`McpInvalidParamsError` so the JSON-RPC error code
stays in the ``-32602`` "invalid params" lane — the dispatcher
otherwise has no HTTP-403 analogue.

Tenant scoping is enforced via :attr:`Operator.tenant_id` — every
:class:`MemoryService` call binds the tenant from the validated JWT
operator, so cross-tenant access is structurally impossible. The
substrate's tenant filter then carries the boundary down to the SQL
layer.

Read-side 404-vs-403 collapse
=============================

The companion :mod:`meho_backplane.mcp.resources.memory` resource
collapses "not found" and "RBAC denied" reads into the same error
shape (``MemoryService.recall`` returns ``None`` on both paths so an
operator probing for another operator's user-scoped slugs can't use
the response shape as a tenant/user existence oracle). The matching
T2 REST route renders both as 404 for the same reason. This module's
write path does not need the same collapse — write denial is the
operator's own action, not a probe of someone else's state.

Audit + broadcast
=================

The dispatcher in :mod:`meho_backplane.mcp.handlers` emits one
``audit_log`` row + one broadcast event per ``tools/call`` invocation;
the ``op_class`` here (``"read"`` for search, ``"write"`` for add)
flows into :func:`~meho_backplane.broadcast.classify.classify_op` so
the broadcast payload is shaped correctly. ``search_memory`` /
``add_to_memory`` are op-ids the classifier matches via its fall-
through ``other`` bucket; both surface full-payload broadcasts (no
credential redaction).

TTL semantics
=============

``add_to_memory`` accepts ``ttl`` as an ISO 8601 duration string
(``"P7D"`` for 7 days, ``"PT1H"`` for one hour) per ISO 8601-2:2019.
The handler parses it into a concrete ``expires_at`` timestamp before
calling the service so the storage layer sees the same wall-clock
shape it accepts on the REST surface (T2 #422). A missing ``ttl`` on
a user-scope write picks up the **default 7-day TTL** that G5.2 #374
will inject when its background expiry executor lands; for G5.1 the
metadata column simply stores ``None`` and the read-side filter
treats it as "never expires". The tool description names this
contract explicitly so an agent that wants a specific lifetime knows
to set ``ttl``, and an agent that wants the default knows to omit it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.memory.rbac import PermissionDeniedError
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService

__all__: list[str] = []


#: Op-class strings keep parity with :mod:`meho_backplane.broadcast.classify`'s
#: credential / audit / read / write taxonomy. ``search_memory`` is read;
#: ``add_to_memory`` is write.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"

#: Default + maximum hit count for ``search_memory``. Matches the
#: :meth:`MemoryService.search_memories` default (10); the retrieval
#: substrate caps at 50 internally.
_DEFAULT_SEARCH_LIMIT: Final[int] = 10
_MAX_SEARCH_LIMIT: Final[int] = 50

#: ISO 8601 enum of the five scope values, exposed via the MCP
#: ``inputSchema`` ``enum`` constraint. Built from
#: :class:`MemoryScope` so an enum extension lands here automatically.
_SCOPE_ENUM: Final[list[str]] = [scope.value for scope in MemoryScope]


# ---------------------------------------------------------------------------
# ISO 8601 duration parsing
# ---------------------------------------------------------------------------


def _parse_iso_duration(value: str) -> datetime:
    """Parse an ISO 8601 duration string into an absolute ``expires_at``.

    Accepts the subset ``P[nD][T[nH][nM][nS]]`` — days / hours / minutes
    / seconds with integer components — and anchors the result at
    :func:`datetime.now(UTC)`. Months and years are deliberately
    rejected: they have variable length (Feb has 28-29 days, leap-
    year edges) and ``timedelta`` does not support them. For longer
    TTLs operators specify ``P30D`` / ``P365D`` etc.

    Returns the computed ``expires_at`` as a UTC-aware datetime. Raises
    :class:`ValueError` with a human-readable message on any parse
    failure so the caller can surface
    :class:`~meho_backplane.mcp.server.McpInvalidParamsError`.
    """
    if not value or value[0] != "P":
        raise ValueError(f"ttl {value!r} must be an ISO 8601 duration (e.g. 'P7D', 'PT1H')")
    # Split on the optional 'T' separator between date and time parts.
    remainder = value[1:]
    date_part, time_part = remainder, ""
    if "T" in remainder:
        date_part, time_part = remainder.split("T", 1)
    if not date_part and not time_part:
        raise ValueError(f"ttl {value!r} carries no duration components")

    days = _consume_unit(date_part, "D", value, ("Y", "M", "W"))
    hours = _consume_unit(time_part, "H", value, ())
    # After hours, only minutes and seconds remain.
    minute_seconds_part = _strip_consumed(time_part, "H")
    minutes = _consume_unit(minute_seconds_part, "M", value, ())
    seconds_part = _strip_consumed(minute_seconds_part, "M")
    seconds = _consume_unit(seconds_part, "S", value, ())

    total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total_seconds <= 0:
        raise ValueError(f"ttl {value!r} must resolve to a positive duration")
    return datetime.now(UTC) + timedelta(seconds=total_seconds)


def _consume_unit(
    segment: str,
    unit: str,
    full_value: str,
    rejected_units: tuple[str, ...],
) -> int:
    """Consume the integer-prefix-then-unit slice of an ISO duration.

    ``segment`` is the substring already split out by date/time parsing;
    ``unit`` is the trailing letter to find (``D`` / ``H`` / ``M`` /
    ``S``); ``full_value`` is the original ttl value used only for
    error messages so an operator sees what they passed.

    Rejected units (years/months/weeks on the date side) surface a
    distinct error message rather than silently parsing — operators
    who try ``P1Y`` should know the contract is days-and-below.
    """
    for rejected in rejected_units:
        if rejected in segment:
            raise ValueError(
                f"ttl {full_value!r} uses unsupported unit {rejected!r}; "
                "only days (D), hours (H), minutes (M), seconds (S) are accepted",
            )
    index = segment.find(unit)
    if index == -1:
        return 0
    prefix = segment[:index]
    if not prefix or not prefix.isdigit():
        raise ValueError(
            f"ttl {full_value!r}: expected integer before {unit!r}, got {prefix!r}",
        )
    return int(prefix)


def _strip_consumed(segment: str, unit: str) -> str:
    """Return the part of ``segment`` after the leading ``<int><unit>``."""
    index = segment.find(unit)
    if index == -1:
        return segment
    return segment[index + 1 :]


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _search_memory_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run a hybrid BM25 + cosine retrieval over the operator's visible memories.

    Delegates to :meth:`MemoryService.search_memories` with the
    operator's JWT-bound :attr:`~Operator.tenant_id`. The service-side
    RBAC matrix post-filters the substrate's hits so an operator's
    search never surfaces another operator's user-scoped row even when
    retrieval ranked it highly — the wire shape carries only entries
    the operator is allowed to see.

    The ``scope`` argument is optional; omitting it searches across
    every kind the operator can read (user / user-tenant / user-target
    / tenant / target). Passing one narrows the retrieval to that
    single kind, useful when the agent knows it's looking for a
    tenant convention vs an operator preference.
    """
    query: str = arguments["query"]
    scope_raw = arguments.get("scope")
    scope = MemoryScope(scope_raw) if scope_raw is not None else None
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    service = MemoryService()
    hits = await service.search_memories(
        operator,
        query,
        scope=scope,
        limit=limit,
    )
    return {
        "hits": [hit.model_dump(mode="json") for hit in hits],
    }


async def _add_to_memory_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Create one memory entry under the operator's tenant.

    Three things this handler is responsible for:

    * Translating the ``scope`` argument from its wire string into the
      typed :class:`MemoryScope` before reaching the service. The
      tool's ``inputSchema`` enum gates the values, so a bad scope
      surfaces as JSON-Schema validation failure (``-32602``) before
      this handler runs.
    * Parsing the optional ``ttl`` ISO 8601 duration into an absolute
      ``expires_at``. The service stores the parsed timestamp in
      ``doc_metadata.expires_at`` and the read-side filter uses it
      directly; G5.2's expiry executor reaps rows whose ``expires_at``
      has passed.
    * Catching :class:`PermissionDeniedError` from the service's RBAC
      matrix (most commonly: an ``operator`` role attempting a
      ``TENANT`` write) and re-raising as
      :class:`McpInvalidParamsError`. JSON-RPC has no HTTP-403
      analogue; the spec example for "Unknown tool" uses ``-32602``
      so a parallel RBAC denial uses the same code rather than
      inventing a server-side semantic.

    Returns the full :class:`~meho_backplane.memory.schemas.MemoryEntry`
    payload (id, slug, body, metadata, timestamps) so the agent can
    verify the write landed without a follow-up
    ``resources/read meho://memory/{scope}/{slug}`` round-trip.
    """
    content: str = arguments["content"]
    scope = MemoryScope(arguments["scope"])
    slug = arguments.get("slug")
    target_name = arguments.get("target_name")
    tags = arguments.get("tags")

    # Mirror the REST shape (T2 #422): ``tags`` is rendered into
    # ``metadata.tags`` so the service-layer ``has_tag`` filter picks
    # it up uniformly across MCP and REST writes.
    metadata: dict[str, Any] | None = {"tags": list(tags)} if tags is not None else None

    expires_at: datetime | None = None
    ttl_raw = arguments.get("ttl")
    if ttl_raw is not None:
        try:
            expires_at = _parse_iso_duration(ttl_raw)
        except ValueError as exc:
            raise McpInvalidParamsError(f"add_to_memory: {exc}") from exc

    service = MemoryService()
    try:
        entry = await service.remember(
            operator,
            scope=scope,
            body=content,
            slug=slug,
            metadata=metadata,
            expires_at=expires_at,
            target_name=target_name,
        )
    except PermissionDeniedError as exc:
        raise McpInvalidParamsError(f"add_to_memory: {exc}") from exc
    except ValueError as exc:
        # Service raises ValueError for missing target_name on
        # target-scoped writes and for invalid slug shape. Both are
        # caller-input errors → INVALID_PARAMS.
        raise McpInvalidParamsError(f"add_to_memory: {exc}") from exc

    return entry.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="search_memory",
        description=(
            "Search the operator's accessible memories (own user-scoped "
            "entries + tenant-shared + target-shared entries visible to "
            "this operator). Returns ranked hits with a body snippet and "
            "the (scope, slug) pair you'll need to fetch the full body "
            "via `resources/read meho://memory/{scope}/{slug}`. "
            "Call when you need to recall established team conventions, "
            "operator preferences for this tenant, or notes about a "
            "specific target — BEFORE asking the operator a question the "
            "memory may already answer. "
            "Scope filter is optional: omit it to search across all "
            "visible scopes (user / user-tenant / user-target / tenant / "
            "target); pass one to narrow (e.g. scope='tenant' for shared "
            "team conventions only). "
            "Do NOT use this for durable team knowledge — that lives in "
            "the knowledge base (`search_knowledge`). Memory is for "
            "shorter-lived, operator/tenant/target-scoped state. "
            "Limit defaults to 10; cap is 50 (substrate-enforced)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Free-form query. Both BM25 (lexical) and cosine "
                        "(semantic) signals consume it; ranks are fused via RRF."
                    ),
                },
                "scope": {
                    "type": ["string", "null"],
                    "enum": [*_SCOPE_ENUM, None],
                    "description": (
                        "Optional scope filter. One of 'user' "
                        "(personal across tenants), 'user-tenant' (personal "
                        "within this tenant), 'user-target' (personal scoped "
                        "to one target), 'tenant' (shared with the team), "
                        "'target' (shared with anyone touching this target). "
                        "Null or omitted searches across every scope the "
                        "operator can read."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": _DEFAULT_SEARCH_LIMIT,
                    "description": "Maximum number of ranked hits to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_search_memory_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="add_to_memory",
        description=(
            "Add a new memory entry. Use when you (or the operator "
            "working with you) learn something worth retaining beyond "
            "this session: an operator preference, a tenant convention, "
            "a target-specific gotcha. "
            "Pick the NARROWEST scope that captures intent: "
            "'user' (personal across tenants) when the note is about "
            "you the operator, regardless of which tenant you're "
            "working in; "
            "'user-tenant' (personal within this tenant) when the note "
            "is your own preference for one specific tenant; "
            "'user-target' (personal scoped to one target) when the "
            "note applies to your interaction with one infrastructure "
            "target — requires `target_name`; "
            "'tenant' (shared with the team — tenant_admin role only) "
            "when the note is a team-wide convention every operator in "
            "the tenant should see; "
            "'target' (shared with anyone touching this target) when the "
            "note is a target-specific gotcha every operator should see "
            "— requires `target_name`. "
            "An operator without tenant_admin role attempting a 'tenant' "
            "write is denied (INVALID_PARAMS); promote via the operator-"
            "driven `meho promote` verb (G5.2) instead. "
            "ALWAYS call `search_memory` FIRST with the topic you're "
            "about to capture: re-adding under a different slug "
            "fragments the corpus. If a matching entry already exists, "
            "prefer extending its body (re-add with same slug, body-hash "
            "short-circuit updates in place). "
            "TTL is optional, formatted as ISO 8601 duration ('P7D' = "
            "7 days, 'PT1H' = 1 hour). Omit to accept the backend "
            "default (G5.2 #374 will inject a 7-day TTL on user-scope "
            "writes; tenant- and target-scope writes default to no "
            "expiry). "
            "Do NOT use this for durable, generalizable team knowledge — "
            "that belongs in `add_to_knowledge`. Memory is for shorter-"
            "lived, operator/tenant/target-scoped state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Memory body. Markdown is fine; stored as-is. "
                        "The retrieval substrate indexes it as a single "
                        "document (BM25 over tsvector + 384-dim embedding "
                        "for cosine)."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": _SCOPE_ENUM,
                    "description": (
                        "One of 'user', 'user-tenant', 'user-target', "
                        "'tenant', 'target'. Pick the narrowest scope "
                        "that captures intent; see the tool description "
                        "for the matrix. 'user-target' and 'target' "
                        "require `target_name`."
                    ),
                },
                "ttl": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional ISO 8601 duration ('P7D', 'PT1H', "
                        "'PT30M'). Months and years are not accepted "
                        "(variable-length). Omit to accept the backend "
                        "default — G5.2 #374 will inject a 7-day TTL on "
                        "user-scope writes; tenant/target writes default "
                        "to no expiry."
                    ),
                },
                "target_name": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "description": (
                        "Target name, required when scope is "
                        "'user-target' or 'target'. Ignored for other "
                        "scopes (server-side write boundary clears it)."
                    ),
                },
                "slug": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "description": (
                        "Operator-facing identifier within the scope. "
                        "Allowed characters: letters, digits, hyphen, "
                        "underscore, dot. Omit to let the backend "
                        "auto-generate a 12-char UUID hex prefix."
                    ),
                },
                "tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Optional list of tag strings. Stored in "
                        "metadata.tags and filterable via the `list` REST "
                        "verb; the MCP search surface does not filter on "
                        "tags directly in v0.2."
                    ),
                },
            },
            "required": ["content", "scope"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_add_to_memory_handler,
)
