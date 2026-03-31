# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Memory context builder for agent prompt injection.

Formats connector-scoped memories into structured markdown blocks for
injection into specialist agent system prompts and orchestrator synthesis.

Phase 11 - Agent Context Injection (INFRA-01, MEM-05):
- build_memory_context(): Full memory block for specialist agent system prompt
- build_memory_summary(): Lightweight title list for orchestrator synthesis

Phase 89.1 - Semantic Memory Relevance (MEM-RELEVANCE):
- build_relevant_memory_context(): Semantic search + token budget for specialist
- build_relevant_memory_summary(): Semantic search for orchestrator synthesis
"""

from __future__ import annotations

from meho_app.modules.memory.schemas import MemoryResponse

# Display order for memory type groups
_TYPE_ORDER = ["entity", "pattern", "outcome", "config"]

# Human-readable labels for each memory type
_TYPE_LABELS = {
    "entity": "Known Entities",
    "pattern": "Diagnostic Patterns",
    "outcome": "Past Outcomes",
    "config": "Configuration Notes",
}

# Confidence badges for full context (specialist agent)
_CONFIDENCE_BADGES = {
    "operator": "[operator-provided]",
    "confirmed_outcome": "[confirmed]",
    "auto_extracted": "[auto-extracted]",
}

# Short badge abbreviations for summary (orchestrator synthesis)
_CONFIDENCE_ABBREVS = {
    "operator": "OP",
    "confirmed_outcome": "OK",
    "auto_extracted": "AI",
}

# Confidence sort priority (lower = higher priority = sorted first)
_CONFIDENCE_SORT_ORDER = {
    "operator": 0,
    "confirmed_outcome": 1,
    "auto_extracted": 2,
}


def build_memory_context(memories: list[MemoryResponse]) -> str:
    """Build full memory context block for specialist agent system prompt.

    Groups memories by type, orders by confidence then recency, and wraps
    in XML tags with a preamble instructing the agent how to use memories.

    Args:
        memories: List of MemoryResponse objects for a single connector.

    Returns:
        Formatted markdown string wrapped in <connector_memory> tags,
        or empty string if no memories.
    """
    if not memories:
        return ""

    # Group memories by type
    groups: dict[str, list[MemoryResponse]] = {t: [] for t in _TYPE_ORDER}
    for mem in memories:
        mt = mem.memory_type
        if mt in groups:
            groups[mt].append(mem)
        else:
            # Unknown type -- append to config as fallback
            groups["config"].append(mem)

    # Sort within each group: confidence descending (operator first), then by recency (newest first)
    for mt in _TYPE_ORDER:
        groups[mt].sort(
            key=lambda m: (
                _CONFIDENCE_SORT_ORDER.get(m.confidence_level, 99),
                # Negate timestamp for descending sort (newest first)
                -m.last_seen.timestamp() if m.last_seen else 0,
            )
        )

    # Build the output
    lines: list[str] = []
    lines.append("<connector_memory>")
    lines.append("")
    lines.append("## Connector Memory")
    lines.append("")
    lines.append(
        "You have accumulated knowledge about this connector from past diagnostics "
        "and operator input. Use these memories proactively during your investigation."
    )
    lines.append("")
    lines.append(
        "- **Operator-provided memories are authoritative** -- treat them as fact. "
        'Use voice: "You told me that..." / "You mentioned that..."'
    )
    lines.append(
        "- **Auto-extracted memories are observations** from past diagnostics. "
        'Use voice: "From past diagnostics, I noticed that..." / "I\'ve observed that..."'
    )
    lines.append("- If an observation **contradicts** a memory, flag the contradiction explicitly.")
    lines.append("")

    # Emit each type group (only if it has memories)
    for mt in _TYPE_ORDER:
        group = groups[mt]
        if not group:
            continue

        label = _TYPE_LABELS.get(mt, mt.title())
        lines.append(f"### {label}")
        lines.append("")

        for mem in group:
            badge = _CONFIDENCE_BADGES.get(mem.confidence_level, "[unknown]")
            lines.append(f"**{mem.title}** {badge}")
            lines.append(mem.body)
            lines.append("")

    lines.append("</connector_memory>")

    return "\n".join(lines)


def build_memory_summary(memories: list[MemoryResponse]) -> str:
    """Build lightweight memory summary for orchestrator synthesis.

    One line per memory with badge abbreviation, type, and title.
    No body text, no grouping -- just a flat list for cross-connector
    synthesis awareness.

    Args:
        memories: List of MemoryResponse objects for a single connector.

    Returns:
        Formatted markdown string with summary header,
        or empty string if no memories.
    """
    if not memories:
        return ""

    lines: list[str] = []
    lines.append("## Memory Summary")
    lines.append("")

    for mem in memories:
        abbrev = _CONFIDENCE_ABBREVS.get(mem.confidence_level, "??")
        lines.append(f"- [{abbrev}] {mem.memory_type}: {mem.title}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Operator-only builder (Phase 99 - INV-05/INV-06)
# ---------------------------------------------------------------------------


async def build_operator_memory_context(
    connector_id: str,
    tenant_id: str,
) -> str:
    """Build operator-only memory context for specialist system prompt.

    Phase 99: Only tier-1 (operator-provided) memories are injected into the
    system prompt. Auto-extracted memories are available via recall_memory tool.

    Args:
        connector_id: Connector scope for memory lookup.
        tenant_id: Tenant scope for memory lookup.

    Returns:
        Formatted markdown via build_memory_context() with operator memories only,
        or empty string if no operator memories.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.schemas import MemoryFilter
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()
    async with session_maker() as db:
        svc = get_memory_service(db)
        operator_memories = await svc.list_memories(
            MemoryFilter(
                connector_id=connector_id,
                tenant_id=tenant_id,
                confidence_level="operator",
                limit=100,
            )
        )

    if not operator_memories:
        return ""

    return build_memory_context(list(operator_memories))


# ---------------------------------------------------------------------------
# Semantic relevance builders (Phase 89.1 - MEM-RELEVANCE)
# ---------------------------------------------------------------------------


async def build_relevant_memory_context(
    query: str,
    connector_id: str,
    tenant_id: str,
    token_budget: int = 5000,
) -> str:
    """Build semantically relevant memory context within a token budget.

    Opens its own DB session for independent read-only queries.
    This is intentional -- memory queries do not participate in
    the caller's transaction (e.g., the specialist agent's session).

    Two-tier approach:
      Tier 1: Operator-provided memories (always included, exempt from budget).
      Tier 2: Semantically relevant auto-extracted memories within token budget.

    Args:
        query: The user's natural language query for semantic relevance.
        connector_id: Connector scope for memory lookup.
        tenant_id: Tenant scope for memory lookup.
        token_budget: Maximum tokens for non-operator memories (default 5000).

    Returns:
        Formatted markdown string via build_memory_context(),
        or empty string if no relevant memories.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.knowledge.text_validation import count_tokens
    from meho_app.modules.memory.schemas import MemoryFilter
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()
    async with session_maker() as db:
        svc = get_memory_service(db)

        # Tier 1: Operator memories (always included, exempt from budget per D-09)
        operator_memories = await svc.list_memories(
            MemoryFilter(
                connector_id=connector_id,
                tenant_id=tenant_id,
                confidence_level="operator",
                limit=100,
            )
        )

        # Tier 2: Semantically relevant memories (per D-11)
        relevant_results = await svc.search(
            query=query,
            connector_id=connector_id,
            tenant_id=tenant_id,
            top_k=20,
            score_threshold=0.5,
        )

    # Build operator ID set for dedup
    operator_ids = {mem.id for mem in operator_memories}

    # Budget enforcement (per D-10): track tokens for non-operator memories
    selected: list[MemoryResponse] = list(operator_memories)
    used_tokens = 0

    # Iterate semantic results (already sorted by final_score descending per D-12)
    for result in relevant_results:
        mem = result.memory
        # Skip if already in operator set (no duplicates)
        if mem.id in operator_ids:
            continue

        mem_tokens = count_tokens(f"{mem.title}\n{mem.body}")
        if used_tokens + mem_tokens > token_budget:
            # Skip this one but continue -- a smaller memory might fit
            continue

        selected.append(mem)
        used_tokens += mem_tokens

    return build_memory_context(selected)


async def build_relevant_memory_summary(
    query: str,
    connector_id: str,
    tenant_id: str,
) -> str:
    """Build semantically relevant memory summary for orchestrator synthesis.

    Opens its own DB session for independent read-only queries.
    This is intentional -- memory queries do not participate in
    the caller's transaction (e.g., the orchestrator's session).

    Lighter version of build_relevant_memory_context -- uses search with
    top_k=10 instead of list_memories(limit=1000). Always includes operator
    memories.

    Args:
        query: The user's natural language query for semantic relevance.
        connector_id: Connector scope for memory lookup.
        tenant_id: Tenant scope for memory lookup.

    Returns:
        Formatted markdown string via build_memory_summary(),
        or empty string if no relevant memories.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.schemas import MemoryFilter
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()
    async with session_maker() as db:
        svc = get_memory_service(db)

        # Always include operator memories
        operator_memories = await svc.list_memories(
            MemoryFilter(
                connector_id=connector_id,
                tenant_id=tenant_id,
                confidence_level="operator",
                limit=100,
            )
        )

        # Semantic search for relevant memories
        relevant_results = await svc.search(
            query=query,
            connector_id=connector_id,
            tenant_id=tenant_id,
            top_k=10,
            score_threshold=0.5,
        )

    # Dedup operator memories from search results
    operator_ids = {mem.id for mem in operator_memories}
    selected: list[MemoryResponse] = list(operator_memories)

    for result in relevant_results:
        if result.memory.id not in operator_ids:
            selected.append(result.memory)

    return build_memory_summary(selected)
