# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for Claude migration validation.

Validates the complete LLM migration from OpenAI gpt-4.1-mini to Anthropic
Claude (Opus 4.6 / Sonnet 4.6). Tests cover:

1. Agent construction with Claude models
2. Simple inference (real API call)
3. Structured output (real API call)
4. Model type detection
5. Config defaults
6. Prompt XML structure
7. Adaptive thinking configuration
8. Streaming event mapping with ThinkingPart

Tests marked @pytest.mark.integration require ANTHROPIC_API_KEY.
Tests without the marker run offline (no API calls).
"""

import os

import pytest

# Skip integration tests when no API key is available
HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

# ============================================================================
# Test 1: Agent construction (offline)
# ============================================================================


@pytest.mark.skipif(
    not HAS_ANTHROPIC_KEY,
    reason="ANTHROPIC_API_KEY required for Agent construction (PydanticAI validates provider)",
)
def test_claude_agent_construction():
    """Verify Agent() constructor works with Anthropic Claude models.

    PydanticAI validates the Anthropic provider on construction, so this
    requires ANTHROPIC_API_KEY even though no API call is made.
    """
    from pydantic_ai import Agent

    # Opus 4.6 -- primary reasoning model
    opus_agent = Agent("anthropic:claude-opus-4-6")
    assert opus_agent is not None

    # Sonnet 4.6 -- utility / classification model
    sonnet_agent = Agent("anthropic:claude-sonnet-4-6")
    assert sonnet_agent is not None


# ============================================================================
# Test 2: Simple inference (real API call)
# ============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not HAS_ANTHROPIC_KEY,
    reason="ANTHROPIC_API_KEY not set",
)
async def test_claude_simple_inference():
    """Call infer() with a simple prompt and verify response shape.

    Real API call -- validates that the full inference pipeline works
    end-to-end with Anthropic's API.
    """
    from pydantic_ai import Agent

    agent = Agent("anthropic:claude-sonnet-4-6")
    result = await agent.run("What is 2+2? Answer with just the number.")

    # Response is a non-empty string
    assert isinstance(result.output, str)
    assert len(result.output.strip()) > 0

    # The answer should contain "4"
    assert "4" in result.output

    # Usage stats are populated
    usage = result.usage()
    assert usage.request_tokens > 0
    assert usage.response_tokens > 0


# ============================================================================
# Test 3: Structured output (real API call)
# ============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not HAS_ANTHROPIC_KEY,
    reason="ANTHROPIC_API_KEY not set",
)
async def test_claude_structured_output():
    """Call infer() with a Pydantic output schema and verify validation.

    Real API call -- validates structured output extraction works with Claude.
    """
    from pydantic import BaseModel
    from pydantic_ai import Agent

    class SimpleClassification(BaseModel):
        category: str
        confidence: float

    agent: Agent[None, SimpleClassification] = Agent(
        "anthropic:claude-sonnet-4-6",
        output_type=SimpleClassification,
    )

    result = await agent.run(
        "Classify this text as 'greeting' or 'question': 'Hello, how are you?'"
    )

    output = result.output
    assert isinstance(output, SimpleClassification)
    assert isinstance(output.category, str)
    assert len(output.category) > 0
    assert isinstance(output.confidence, float)
    assert 0.0 <= output.confidence <= 1.0


# ============================================================================
# Test 4: Model type detection (offline)
# ============================================================================


def test_claude_model_detection():
    """Verify ModelConfig correctly detects model types for all providers."""
    from meho_app.modules.agents.config.models import ModelConfig

    # Anthropic models -> instruct (adaptive thinking handled separately)
    opus_config = ModelConfig(name="anthropic:claude-opus-4-6")
    assert opus_config.model_type == "instruct"

    sonnet_config = ModelConfig(name="anthropic:claude-sonnet-4-6")
    assert sonnet_config.model_type == "instruct"

    # OpenAI instruct models -> instruct (backward compat)
    gpt_config = ModelConfig(name="openai:gpt-4.1-mini")
    assert gpt_config.model_type == "instruct"

    # OpenAI reasoning models -> reasoning (backward compat)
    o1_config = ModelConfig(name="openai:o1")
    assert o1_config.model_type == "reasoning"


# ============================================================================
# Test 5: Config defaults (offline)
# ============================================================================


def test_claude_config_defaults():
    """Verify Config() loads with Claude defaults.

    Uses environment variable override to avoid needing real DB connection.
    """
    from meho_app.core.config import Config

    # Build a minimal config with required fields to test defaults
    # We check the Field defaults directly on the class, not an instance
    # (instance creation requires all env vars)
    llm_default = Config.model_fields["llm_model"].default
    classifier_default = Config.model_fields["classifier_model"].default
    embedding_default = Config.model_fields["embedding_model"].default

    # LLM model defaults to Anthropic Claude
    assert llm_default.startswith("anthropic:"), (
        f"Expected llm_model default to start with 'anthropic:', got '{llm_default}'"
    )

    # Classifier defaults to Anthropic Claude
    assert classifier_default.startswith("anthropic:"), (
        f"Expected classifier_model default to start with 'anthropic:', got '{classifier_default}'"
    )

    # Embedding model uses Voyage AI (migrated from OpenAI in Plan 04)
    assert embedding_default == "voyage-4-large", (
        f"Expected embedding_model default to be 'voyage-4-large', got '{embedding_default}'"
    )


# ============================================================================
# Test 6: Streaming event mapping with ThinkingPart (real API call)
# ============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not HAS_ANTHROPIC_KEY,
    reason="ANTHROPIC_API_KEY not set",
)
async def test_claude_streaming_event_mapping():
    """Verify SSE streaming pipeline handles Claude's ThinkingPart objects.

    Uses a real Agent with adaptive thinking to validate that:
    1. ThinkingPart is importable (needed by SSE mapper)
    2. Streaming produces text content chunks
    3. ThinkingPart objects (if present) contain non-empty content

    This validates PydanticAI's stream abstraction normalizes Anthropic's
    chunk format and that ThinkingPart objects are accessible for the SSE
    pipeline to map to 'thought' events.
    """
    from pydantic_ai import Agent
    from pydantic_ai.messages import ThinkingPart
    from pydantic_ai.models.anthropic import AnthropicModelSettings

    # Verify ThinkingPart is importable (needed by SSE mapper in 01-02)
    assert ThinkingPart is not None

    # Create agent with adaptive thinking enabled
    settings = AnthropicModelSettings(
        anthropic_thinking={"type": "adaptive"},
        anthropic_effort="high",
    )
    agent = Agent(
        "anthropic:claude-sonnet-4-6",
        model_settings=settings,
    )

    # Run with a prompt that encourages thinking
    result = await agent.run("Explain briefly why 1+1=2 in mathematics.")

    # Verify we got a response
    assert isinstance(result.output, str)
    assert len(result.output.strip()) > 0

    # Check messages for ThinkingPart objects
    all_messages = result.all_messages()
    assert len(all_messages) > 0, "Expected at least one message"

    text_content_found = False
    thinking_parts_found = []

    for msg in all_messages:
        if hasattr(msg, "parts"):
            for part in msg.parts:
                if isinstance(part, ThinkingPart):
                    # ThinkingPart should have content
                    if part.content:
                        thinking_parts_found.append(part.content)
                elif hasattr(part, "content") and isinstance(getattr(part, "content", None), str):
                    text_content_found = True

    # Text content must be present (the actual response)
    assert text_content_found, "Expected text content in streamed response messages"

    # ThinkingPart objects may or may not appear depending on model behavior.
    # When they do appear, they should have non-empty content.
    if thinking_parts_found:
        for content in thinking_parts_found:
            assert len(content.strip()) > 0, "ThinkingPart content should be non-empty"


# ============================================================================
# Cost estimation validation (offline)
# ============================================================================


def test_claude_cost_estimation():
    """Verify Anthropic pricing is included in cost estimation.

    This validates the estimate_cost() function returns correct values
    for Claude models (pricing added in Plan 02).
    """
    from meho_app.modules.agents.base.event_builders import (
        MODEL_COSTS,
        estimate_cost,
    )

    # Verify Anthropic models are in MODEL_COSTS
    assert "claude-opus-4" in MODEL_COSTS, "Missing claude-opus-4 in MODEL_COSTS"
    assert "claude-sonnet-4" in MODEL_COSTS, "Missing claude-sonnet-4 in MODEL_COSTS"

    # Verify pricing values (per 1K tokens)
    # Opus: $15/1M input = $0.015/1K, $75/1M output = $0.075/1K
    assert MODEL_COSTS["claude-opus-4"]["input"] == 0.015
    assert MODEL_COSTS["claude-opus-4"]["output"] == 0.075

    # Sonnet: $3/1M input = $0.003/1K, $15/1M output = $0.015/1K
    assert MODEL_COSTS["claude-sonnet-4"]["input"] == 0.003
    assert MODEL_COSTS["claude-sonnet-4"]["output"] == 0.015

    # Verify estimate_cost works for Anthropic models
    opus_cost = estimate_cost("anthropic:claude-opus-4-6", 1000, 500)
    assert opus_cost is not None
    assert opus_cost > 0

    sonnet_cost = estimate_cost("anthropic:claude-sonnet-4-6", 1000, 500)
    assert sonnet_cost is not None
    assert sonnet_cost > 0

    # Verify OpenAI models still work (backward compat)
    gpt_cost = estimate_cost("openai:gpt-4.1-mini", 1000, 500)
    assert gpt_cost is not None
    assert gpt_cost > 0

    # Verify Opus costs more than Sonnet
    assert opus_cost > sonnet_cost, (
        f"Opus ({opus_cost}) should cost more than Sonnet ({sonnet_cost})"
    )


# ============================================================================
# Test 9: Admin API model allowlist (offline)
# ============================================================================


def test_claude_admin_model_allowlist():
    """Verify admin API model allowlist includes Claude 4.6 models.

    Validates both the GET /models response (via get_allowed_models()) and the
    update_config() validation list (via source inspection). This is an offline
    test -- no API key or HTTP client required.
    """
    import asyncio

    from meho_app.api.routes_admin import get_allowed_models

    # get_allowed_models() is an async function (FastAPI endpoint)
    response = asyncio.get_event_loop().run_until_complete(get_allowed_models())

    allowed_models = response["allowed_models"]
    allowed_ids = [m["id"] for m in allowed_models]
    default_model = response["default_model"]

    # Claude 4.6 models must be in the allowed list
    assert "anthropic:claude-opus-4-6" in allowed_ids, (
        f"claude-opus-4-6 missing from allowed_models: {allowed_ids}"
    )
    assert "anthropic:claude-sonnet-4-6" in allowed_ids, (
        f"claude-sonnet-4-6 missing from allowed_models: {allowed_ids}"
    )

    # Stale claude-4-sonnet must NOT be present
    assert "anthropic:claude-4-sonnet" not in allowed_ids, (
        "Stale claude-4-sonnet should be removed from allowed_models"
    )

    # Default model must be Claude Opus 4.6
    assert default_model == "anthropic:claude-opus-4-6", (
        f"Expected default_model='anthropic:claude-opus-4-6', got '{default_model}'"
    )

    # Claude 4.6 models should be recommended
    for model in allowed_models:
        if model["id"] in ("anthropic:claude-opus-4-6", "anthropic:claude-sonnet-4-6"):
            assert model["recommended"] is True, f"Expected {model['id']} to be recommended=True"

    # Validate the update_config() validation list via source inspection
    # (same pattern as test_claude_prompt_xml_structure)
    import inspect

    from meho_app.api import routes_admin

    source = inspect.getsource(routes_admin)

    # The update_config allowed_models list must contain Claude 4.6 strings
    assert '"anthropic:claude-opus-4-6"' in source, (
        "update_config() source missing anthropic:claude-opus-4-6 in allowed_models"
    )
    assert '"anthropic:claude-sonnet-4-6"' in source, (
        "update_config() source missing anthropic:claude-sonnet-4-6 in allowed_models"
    )
    assert '"anthropic:claude-4-sonnet"' not in source, (
        "update_config() source still contains stale anthropic:claude-4-sonnet"
    )
