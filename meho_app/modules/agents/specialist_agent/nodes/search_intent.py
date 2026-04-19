# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Search Intent Node - LLM decides what to search for.

This is step 1 of the deterministic workflow.
The LLM analyzes the user goal and outputs a search query.

Skill injection: If skill_content is available in state, it is prepended
to the prompt so the LLM has domain-specific knowledge when deciding
what operation to search for.

Multi-turn context awareness:
- Includes cached table information in prompts
- Detects follow-up queries that should use cached data
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.inference import infer_structured
from meho_app.modules.agents.shared.context_utils import build_tables_context
from meho_app.modules.agents.specialist_agent.models import SearchIntent

if TYPE_CHECKING:
    from meho_app.modules.agents.specialist_agent.state import WorkflowState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class SearchIntentNode:
    """Node that asks LLM what operation to search for.

    Includes multi-turn context awareness - when cached tables exist,
    the LLM can detect follow-up queries and indicate they should use
    cached data instead of calling new operations.

    Skill content from state.skill_content is prepended to the prompt
    to provide domain-specific knowledge for better search decisions.

    Emits:
        thought: "Analyzing query to find relevant operations..."
        thought: "search_intent: {query}"
    """

    connector_name: str
    connector_type: str

    async def run(
        self,
        state: WorkflowState,
        emitter: EventEmitter | None,
    ) -> SearchIntent:
        """Execute the search intent step.

        Args:
            state: Current workflow state (includes cached_tables, skill_content).
            emitter: Event emitter for SSE streaming.

        Returns:
            SearchIntent with the query to search for, and optionally
            use_cached_data=True if this is a follow-up query.
        """
        # Emit thought event
        if emitter:
            await emitter.thought("Analyzing query to find relevant operations...")

        # Build cached tables context for multi-turn awareness
        tables_context = build_tables_context(state.cached_tables)

        # Debug: Log available cached tables
        if state.cached_tables:
            logger.debug(
                f"[{self.connector_name}] Cached tables available: "
                f"{list(state.cached_tables.keys())}"
            )
        else:
            logger.debug(f"[{self.connector_name}] No cached tables available")

        # Build prompt with optional cached tables context
        cached_data_instructions = ""
        if tables_context:
            cached_data_instructions = f"""
{tables_context}

**IMPORTANT - Use Cached Data When Possible:**
If the user's question can be answered using data from a cached table above, you MUST:
1. Set use_cached_data=true
2. Set cached_table_name to the relevant table name

Reference patterns that REQUIRE using cached data:
- "those X", "the X", "these X" (e.g., "those VMs", "the instances")
- "show more", "more details", "more info about"
- "what about...", "check the...", "look at the..."
- Asking for different columns/fields from the same resource type
- Any follow-up about resources we already fetched

Only call a NEW operation if:
- The data doesn't exist in any cached table
- The user explicitly asks for fresh/updated data
- A completely different resource type is needed
"""

        # Prepend skill content for domain-specific knowledge
        skill_context = ""
        if state.skill_content:
            skill_context = f"""<domain_knowledge>
{state.skill_content}
</domain_knowledge>

"""

        prompt = f"""{skill_context}You are investigating connector "{self.connector_name}" ({self.connector_type}) to answer:

User Question: {state.user_goal}
{cached_data_instructions}
What operation should we search for? Provide a search query that would find relevant API operations.

Examples:
- "list namespaces" for namespace questions
- "get pods" for pod questions
- "cluster health" for health status
"""

        result = await infer_structured(
            prompt=prompt,
            response_model=SearchIntent,
        )

        # Emit result - include cached data detection info
        if emitter:
            if result.use_cached_data and result.cached_table_name:
                await emitter.thought(
                    f"Detected follow-up query - will use cached '{result.cached_table_name}' table"
                )
            else:
                await emitter.thought(f"search_intent: {result.query}")

        # Update state
        if result.use_cached_data:
            state.steps_executed.append(
                f"search_intent: use_cached_data={result.cached_table_name}"
            )
        else:
            state.steps_executed.append(f"search_intent: {result.query}")

        logger.debug(
            f"[{self.connector_name}] Search intent: query={result.query}, "
            f"use_cached_data={result.use_cached_data}, "
            f"cached_table={result.cached_table_name}"
        )
        return result
