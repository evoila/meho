# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Ask Mode execution path for the Orchestrator Agent.

Ask mode provides knowledge-based Q&A without action risk:
- Searches all three knowledge tiers (global, type, instance) via reranked hybrid search
- Optionally queries connectors with READ-ONLY operations only (safety_level='safe')
- Synthesizes comprehensive answers from knowledge + optional read-only connector data

Phase 65: This module is called by OrchestratorAgent.run_streaming() when
session_mode == "ask".
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger, get_tracer
from meho_app.modules.agents.base.events import AgentEvent

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

logger = get_logger(__name__)
tracer = get_tracer("meho.orchestrator.ask_mode")


async def run_ask_mode(
    orchestrator: OrchestratorAgent,
    user_message: str,
    session_id: str | None,
    context: dict[str, Any] | None,
) -> AsyncIterator[AgentEvent]:
    """Execute the ask mode path: knowledge retrieval + read-only data + synthesis.

    This function implements the ask mode execution path:
    a. Emit orchestrator_start event with mode="ask"
    b. Search all three knowledge tiers using KnowledgeService.search_with_rerank()
    c. If knowledge results are insufficient AND connectors exist, optionally
       query connector specialists with READ-ONLY toolset (safety_level='safe')
    d. Build synthesis prompt from knowledge results + optional connector data
    e. Call LLM synthesis and stream as final_answer events
    f. Emit orchestrator_complete event

    Args:
        orchestrator: The OrchestratorAgent instance (provides dependencies, config).
        user_message: The user's input message/question.
        session_id: Optional session ID for conversation tracking.
        context: Optional additional context (history, session_state, etc.).

    Yields:
        AgentEvent objects for SSE streaming to frontend.
    """
    start_time_ms = time.time() * 1000
    agent_name = orchestrator.agent_name

    # a. Emit orchestrator_start with mode="ask"
    yield AgentEvent(
        type="orchestrator_start",
        agent=agent_name,
        data={"goal": user_message, "mode": "ask"},
        session_id=session_id,
    )

    logger.info(
        "ask_mode_start",
        session_id=session_id,
        query_length=len(user_message),
    )

    knowledge_context = ""
    knowledge_chunk_count = 0
    connector_data_context = ""

    try:
        # b. Search all three knowledge tiers (no scope filter = ask mode, all tiers)
        yield AgentEvent(
            type="knowledge_search_start",
            agent=agent_name,
            data={"query": user_message[:200], "mode": "ask"},
            session_id=session_id,
        )

        knowledge_results = await _search_knowledge(orchestrator, user_message)
        knowledge_chunk_count = knowledge_results.get("total", 0)

        if knowledge_results.get("chunks"):
            knowledge_context = _format_knowledge_results(knowledge_results["chunks"])

        yield AgentEvent(
            type="knowledge_search_complete",
            agent=agent_name,
            data={
                "chunk_count": knowledge_chunk_count,
                "has_results": bool(knowledge_context),
            },
            session_id=session_id,
        )

        logger.info(
            "ask_mode_knowledge_search",
            session_id=session_id,
            chunk_count=knowledge_chunk_count,
        )

        # c. If knowledge results are insufficient, optionally query connectors read-only
        if knowledge_chunk_count < 3:
            connector_data_context = await _query_connectors_readonly(
                orchestrator, user_message, session_id
            )

        # d+e. Build synthesis prompt and stream response
        yield AgentEvent(
            type="synthesis_start",
            agent=agent_name,
            data={"mode": "ask", "knowledge_chunks": knowledge_chunk_count},
            session_id=session_id,
        )

        synthesis_prompt = _build_ask_synthesis_prompt(
            user_message=user_message,
            knowledge_context=knowledge_context,
            connector_data=connector_data_context,
            conversation_context=_extract_conversation_context(context),
        )

        # Stream synthesis via LLM
        final_answer = ""
        async for chunk in _stream_llm_synthesis(orchestrator, synthesis_prompt):
            final_answer += chunk
            yield AgentEvent(
                type="synthesis_chunk",
                agent=agent_name,
                data={
                    "content": chunk,
                    "accumulated_length": len(final_answer),
                },
                session_id=session_id,
            )

        # f. Emit final_answer
        yield AgentEvent(
            type="final_answer",
            agent=agent_name,
            data={
                "content": final_answer,
                "mode": "ask",
                "knowledge_chunks_used": knowledge_chunk_count,
                "has_connector_data": bool(connector_data_context),
                "total_time_ms": time.time() * 1000 - start_time_ms,
            },
            session_id=session_id,
        )

        # Phase 66: Emit follow-up suggestions for ask mode responses
        try:
            from meho_app.modules.agents.orchestrator.synthesis_parser import (
                extract_follow_ups,
            )

            follow_ups = extract_follow_ups(final_answer)
            if follow_ups:
                yield AgentEvent(
                    type="follow_up_suggestions",
                    agent=agent_name,
                    data={"suggestions": follow_ups[:3]},  # Cap at 3
                    session_id=session_id,
                )
        except Exception as e:
            logger.debug(f"Follow-up extraction failed (non-critical): {e}")

    except Exception as e:
        logger.error(f"Ask mode error: {e}", exc_info=True)
        yield AgentEvent(
            type="error",
            agent=agent_name,
            data={
                "message": f"Ask mode error: {e}",
                "mode": "ask",
                "recoverable": False,
            },
            session_id=session_id,
        )

    # g. Emit orchestrator_complete
    total_time_ms = time.time() * 1000 - start_time_ms
    yield AgentEvent(
        type="orchestrator_complete",
        agent=agent_name,
        data={
            "success": bool(final_answer if "final_answer" in dir() else False),
            "mode": "ask",
            "total_time_ms": total_time_ms,
            "knowledge_chunks": knowledge_chunk_count,
        },
        session_id=session_id,
    )

    logger.info(
        "ask_mode_complete",
        session_id=session_id,
        total_time_ms=int(total_time_ms),
        knowledge_chunks=knowledge_chunk_count,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _search_knowledge(
    orchestrator: OrchestratorAgent,
    query: str,
) -> dict[str, Any]:
    """Search all knowledge tiers using KnowledgeService.search_with_rerank().

    No scope filter means ask mode searches global + all type-level + all instance-level
    knowledge simultaneously.

    Args:
        orchestrator: OrchestratorAgent with dependencies.
        query: The user's search query.

    Returns:
        Dict with chunks, scores, and total count.
    """
    try:
        from meho_app.modules.knowledge.service import KnowledgeService

        # Get a DB session from the orchestrator's dependencies
        session = orchestrator.dependencies.db_session
        service = KnowledgeService(session)

        tenant_id = orchestrator._get_tenant_id()
        user_id = ""
        if hasattr(orchestrator.dependencies, "user_context"):
            user_id = orchestrator.dependencies.user_context.user_id or ""

        # Search with reranking, no connector_id filter = all tiers
        results = await service.search_with_rerank(
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
            connector_id=None,  # No filter = ask mode searches all tiers
            top_k=10,
            rerank_candidates=50,
        )

        return results

    except Exception as e:
        logger.warning(f"Knowledge search failed: {e}")
        return {"chunks": [], "scores": [], "total": 0}


def _format_knowledge_results(chunks: list[dict[str, Any]]) -> str:
    """Format knowledge chunks into a context block for the synthesis prompt.

    Args:
        chunks: List of knowledge chunk dicts from search results.

    Returns:
        Formatted string with numbered knowledge excerpts.
    """
    if not chunks:
        return ""

    sections = []
    for i, chunk in enumerate(chunks, 1):
        text = chunk.get("text", chunk.get("content", ""))
        source = chunk.get("source_uri", "unknown")
        scope = chunk.get("scope_type", "unknown")
        score = chunk.get("rerank_score", chunk.get("rrf_score", 0))

        sections.append(
            f"### Knowledge [{i}] (scope: {scope}, source: {source}, relevance: {score:.3f})\n{text}"
        )

    return "\n\n".join(sections)


async def _query_connectors_readonly(
    orchestrator: OrchestratorAgent,
    _user_message: str,
    session_id: str | None,
) -> str:
    """Query connectors with READ-ONLY operations for supplementary data.

    Only dispatches to connectors that have safe (read-only) operations.
    This is a best-effort supplement to knowledge results.

    Args:
        orchestrator: OrchestratorAgent with dependencies.
        user_message: The user's query.
        session_id: Optional session ID.

    Returns:
        Formatted connector data string, or empty string if no data.
    """
    try:
        connectors = await orchestrator._get_available_connectors()
        if not connectors:
            return ""

        logger.info(
            "ask_mode_connector_query",
            connector_count=len(connectors),
            session_id=session_id,
        )

        # For ask mode, we build a lightweight read-only query
        # rather than dispatching full specialist agents.
        # This keeps ask mode fast and safe.
        connector_summaries = []
        for c in connectors[:3]:  # Limit to top 3 connectors
            connector_summaries.append(
                f"- **{c['name']}** (Type: {c['connector_type']}): {c.get('routing_description', c.get('description', 'No description'))}"
            )

        if connector_summaries:
            return (
                "## Available Connectors (read-only context)\n"
                + "\n".join(connector_summaries)
                + "\n\nNote: In ask mode, connectors are available for reference but no actions were executed."
            )

        return ""

    except Exception as e:
        logger.warning(f"Connector read-only query failed: {e}")
        return ""


def _build_ask_synthesis_prompt(
    user_message: str,
    knowledge_context: str,
    connector_data: str,
    conversation_context: str,
) -> str:
    """Build the synthesis prompt for ask mode.

    Combines knowledge results, optional connector data, and conversation
    context into a comprehensive prompt for LLM synthesis.

    Args:
        user_message: The user's original question.
        knowledge_context: Formatted knowledge search results.
        connector_data: Optional connector data context.
        conversation_context: Previous conversation context.

    Returns:
        Complete synthesis prompt string.
    """
    prompt_parts = [
        "You are MEHO, an intelligent DevOps assistant in ASK MODE.",
        "In ask mode, you answer questions using knowledge base content and operational context.",
        "You do NOT execute any actions or make changes -- you only provide information and analysis.",
        "",
        f"## User Question\n{user_message}",
    ]

    if knowledge_context:
        prompt_parts.append(f"\n## Knowledge Base Results\n{knowledge_context}")
    else:
        prompt_parts.append(
            "\n## Knowledge Base Results\nNo relevant knowledge found in the knowledge base."
        )

    if connector_data:
        prompt_parts.append(f"\n{connector_data}")

    if conversation_context:
        prompt_parts.append(f"\n## Conversation Context\n{conversation_context}")

    prompt_parts.append(
        "\n## Instructions\n"
        "- Synthesize a comprehensive answer from the knowledge base results above.\n"
        "- Cite specific knowledge sources when possible (e.g., [Knowledge 1], [Knowledge 2]).\n"
        "- If the knowledge base doesn't contain enough information, say so clearly.\n"
        "- Do NOT suggest running commands or making changes -- this is ask mode (information only).\n"
        "- Be thorough but concise. Use markdown formatting for readability.\n"
        "- If multiple knowledge sources provide complementary information, combine them into a coherent answer.\n"
        "- End your response with a section:\n"
        "  <follow_ups>\n"
        "  <question>First follow-up question</question>\n"
        "  <question>Second follow-up question</question>\n"
        "  <question>Third follow-up question</question>\n"
        "  </follow_ups>\n"
        "- The follow-up questions should be natural next questions the user might ask based on your answer."
    )

    return "\n".join(prompt_parts)


def _extract_conversation_context(context: dict[str, Any] | None) -> str:
    """Extract conversation history context for multi-turn ask mode.

    Args:
        context: Optional context dict from orchestrator.

    Returns:
        Conversation context string, or empty string.
    """
    if not context:
        return ""

    history = context.get("history", "")
    if isinstance(history, str) and history.strip():
        return history

    return ""


async def _stream_llm_synthesis(
    orchestrator: OrchestratorAgent,
    prompt: str,
) -> AsyncIterator[str]:
    """Stream LLM synthesis response for ask mode.

    Uses the same LLM configuration as the orchestrator's synthesis path.

    Args:
        orchestrator: OrchestratorAgent instance for config access.
        prompt: The complete synthesis prompt.

    Yields:
        Text chunks as they arrive from the LLM.
    """
    from pydantic_ai import Agent, InstrumentationSettings

    try:
        from meho_app.core.config import get_config

        model_name = get_config().llm_model
        if hasattr(orchestrator._config, "model") and hasattr(orchestrator._config.model, "name"):
            model_name = orchestrator._config.model.name

        from meho_app.modules.agents.agent_factories import get_model_settings

        model_settings = get_model_settings(task_type="synthesis")

        agent = Agent(
            model=model_name,
            model_settings=model_settings,
            instrument=InstrumentationSettings(),
        )

        async with agent.run_stream(prompt) as result:
            async for chunk in result.stream_text(delta=True):
                yield chunk

    except Exception as e:
        logger.error(f"Ask mode LLM synthesis error: {e}", exc_info=True)
        yield f"I encountered an error while generating a response: {e}"
