# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
LLM-powered summary generation for orchestrator skills.

Generates concise 3-4 sentence summaries that are injected into the
orchestrator's system prompt to help it decide when to load the full
skill content.
"""

from __future__ import annotations

import asyncio

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "Generate a concise summary (3-4 sentences) describing when to use this "
    "skill, what systems it covers, and what investigation patterns it enables. "
    "The summary will be injected into an AI orchestrator's system prompt to "
    "help it decide when to load the full skill content. Be specific about "
    "the skill's capabilities."
)


async def generate_skill_summary(skill_name: str, skill_content: str) -> str:
    """Generate a concise summary of an orchestrator skill using LLM.

    Uses PydanticAI with Sonnet 4.6 (same lightweight LLM pattern as
    event prompt generation and NL-to-cron conversion).

    Args:
        skill_name: The name of the skill.
        skill_content: Full markdown content of the skill.

    Returns:
        A 3-4 sentence summary string. Falls back to a simple template
        if LLM generation fails or times out.
    """
    fallback = f"Orchestrator skill: {skill_name}. Load for details."

    try:
        from pydantic_ai import Agent

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
        )

        result = await asyncio.wait_for(
            agent.run(f"Skill name: {skill_name}\n\nSkill content:\n{skill_content}"),
            timeout=30.0,
        )
        summary = str(result.output).strip()
        if summary:
            return summary
        return fallback

    except TimeoutError:
        logger.warning(f"Skill summary generation timed out for '{skill_name}', using fallback")
        return fallback
    except Exception as e:
        logger.warning(f"Skill summary generation failed for '{skill_name}': {e}, using fallback")
        return fallback
