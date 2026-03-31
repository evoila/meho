# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event Builder Utilities for Deep Observability.

This module contains helper functions for creating and serializing events,
including cost estimation and message serialization.

These utilities are used by:
- EventFactory for creating DetailedEvents
- TranscriptCollector for computing stats
- EventEmitter for detailed event emission
"""

from __future__ import annotations

from typing import Any

# Cost estimation constants (approximate, per 1K tokens)
# Anthropic pricing: https://docs.anthropic.com/en/docs/about-claude/models
MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-opus-4": {"input": 0.015, "output": 0.075},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-3.5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005},
}


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimate the cost of an LLM call with Anthropic cache pricing.

    Cache-aware: cache reads cost 10% of input price, cache writes cost
    125%. Non-cached input tokens cost 100%.

    Args:
        model: Model name (e.g., "claude-opus-4").
        prompt_tokens: Total input tokens (includes cached).
        completion_tokens: Number of output tokens.
        cache_read_tokens: Tokens served from Anthropic prompt cache.
        cache_write_tokens: Tokens written to Anthropic prompt cache.

    Returns:
        Estimated cost in USD, or None if model not found.
    """
    model_base = model.lower()
    for known_model in MODEL_COSTS:
        if known_model in model_base:
            costs = MODEL_COSTS[known_model]
            regular_input = prompt_tokens - cache_read_tokens - cache_write_tokens
            input_cost = (
                (regular_input / 1000) * costs["input"]
                + (cache_read_tokens / 1000) * costs["input"] * 0.1
                + (cache_write_tokens / 1000) * costs["input"] * 1.25
            )
            output_cost = (completion_tokens / 1000) * costs["output"]
            return round(input_cost + output_cost, 6)
    return None


def serialize_pydantic_messages(messages: list) -> list[dict[str, Any]]:
    """Serialize PydanticAI message objects to JSON-compatible dicts.

    Uses the existing message_serialization module to handle PydanticAI's
    dataclass-based message structures (ModelRequest, ModelResponse, etc.).

    Args:
        messages: List of PydanticAI message objects from result.new_messages()
                  or result.all_messages().

    Returns:
        List of JSON-serializable dictionaries.

    Example:
        >>> result = await agent.run("Hello")
        >>> messages = serialize_pydantic_messages(result.new_messages())
    """
    from meho_app.modules.agents.message_serialization import serialize_message_list

    try:
        return serialize_message_list(messages)
    except Exception:
        # Fallback: convert each message to string representation
        result = []
        for msg in messages:
            try:
                result.append({"raw": str(msg)})
            except Exception:
                result.append({"error": "Failed to serialize message"})
        return result
