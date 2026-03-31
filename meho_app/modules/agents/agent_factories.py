# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent factory functions for creating PydanticAI agents.

Provides lazy-loaded agent instances for different LLM tasks.
Each agent is configured with appropriate model and system prompt.

Multi-provider support (Phase 82):
- get_model_settings(task_type) is the ONLY place that constructs
  provider-specific model settings. Non-Anthropic providers get None.
- All agent factories use get_model_settings() -- no inline
  AnthropicModelSettings anywhere else in the codebase.
"""

from typing import Any, Literal

from pydantic_ai import Agent, InstrumentationSettings

from meho_app.modules.agents.output_schemas import ConnectorDetermination, DataSummary

# Use logs event mode to prevent oversized OTEL span attributes
_instrument = InstrumentationSettings()

TaskType = Literal["classifier", "interpreter", "extractor", "synthesis", "specialist", "inference"]

# Anthropic effort mapping per task type (D-04)
_ANTHROPIC_EFFORT: dict[TaskType, str] = {
    "classifier": "low",
    "interpreter": "high",
    "extractor": "low",
    "synthesis": "high",
    "specialist": "low",
    "inference": "low",
}


def get_model_settings(task_type: TaskType = "inference") -> Any:
    """Build provider-specific model settings.

    Returns AnthropicModelSettings for Anthropic provider, None for others.
    This is the ONLY place that imports AnthropicModelSettings (D-05).

    Args:
        task_type: The agent task type, determines effort level for Anthropic.

    Returns:
        AnthropicModelSettings for Anthropic, None for OpenAI/Ollama.
    """
    from meho_app.core.config import get_config

    config = get_config()

    if config.llm_provider != "anthropic":
        return None

    from pydantic_ai.models.anthropic import AnthropicModelSettings

    effort = _ANTHROPIC_EFFORT.get(task_type, "low")

    # Base settings for all Anthropic agents
    settings_kwargs: dict[str, Any] = {
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": effort,
        "anthropic_cache_instructions": True,
    }

    # D-06, D-07: specialist uses all 3 cache points (multi-turn ReAct loop).
    # Other agents (classifier, interpreter, extractor) are single-turn -- no benefit.
    if task_type == "specialist":
        settings_kwargs["anthropic_cache_tool_definitions"] = True
        settings_kwargs["anthropic_cache_messages"] = True

    return AnthropicModelSettings(**settings_kwargs)


def create_classifier_agent() -> Agent:
    """
    Create the connector classifier agent.

    This agent analyzes queries and matches them to available connectors/systems.
    Returns structured output with connector ID, confidence, and reasoning.

    Uses effort='low' for Anthropic -- fast utility classification task.
    """
    from meho_app.core.config import get_config

    config = get_config()
    model_name = config.classifier_model

    kwargs: dict[str, Any] = {
        "output_type": ConnectorDetermination,
        "instructions": "You are a system classifier. Analyze queries and match them to available connectors/systems.",
        "instrument": _instrument,
    }

    model_settings = get_model_settings(task_type="classifier")
    if model_settings is not None:
        kwargs["model_settings"] = model_settings

    return Agent(model_name, **kwargs)


def create_interpreter_agent() -> Agent:
    """
    Create the results interpreter agent.

    This agent interprets API responses and search results,
    providing natural language answers to user questions.

    Uses effort='high' for Anthropic -- reasoning task that benefits from
    deeper thinking to produce clear, actionable operator answers.
    """
    from meho_app.core.config import get_config

    config = get_config()
    model_name = config.interpreter_model

    kwargs: dict[str, Any] = {
        "instructions": "You are MEHO, an expert infrastructure diagnostics agent. Interpret search results and API responses with the precision of a senior SRE. Show specific data values, flag issues, and provide actionable context.",
        "instrument": _instrument,
    }

    model_settings = get_model_settings(task_type="interpreter")
    if model_settings is not None:
        kwargs["model_settings"] = model_settings

    return Agent(model_name, **kwargs)


def create_data_extractor_agent() -> Agent:
    """
    Create the data extraction agent.

    This agent extracts and summarizes relevant information from large API responses.
    Returns structured output with summary, critical items, and statistics.

    Uses effort='low' for Anthropic -- utility extraction task.
    """
    from meho_app.core.config import get_config

    config = get_config()
    model_name = config.data_extractor_model

    kwargs: dict[str, Any] = {
        "output_type": DataSummary,
        "instructions": "You are a data extraction expert. Extract and summarize relevant information from large API responses.",
        "instrument": _instrument,
    }

    model_settings = get_model_settings(task_type="extractor")
    if model_settings is not None:
        kwargs["model_settings"] = model_settings

    return Agent(model_name, **kwargs)


class AgentManager:
    """
    Manages lazy-loaded agent instances.

    Provides singleton-like access to agent instances, creating them on first use.
    This avoids creating agents until they're actually needed.
    """

    _classifier_agent: Agent | None = None
    _interpreter_agent: Agent | None = None
    _data_extractor_agent: Agent | None = None

    @classmethod
    def get_classifier_agent(cls) -> Agent:
        """Get or create the classifier agent."""
        if cls._classifier_agent is None:
            cls._classifier_agent = create_classifier_agent()
        return cls._classifier_agent

    @classmethod
    def get_interpreter_agent(cls) -> Agent:
        """Get or create the interpreter agent."""
        if cls._interpreter_agent is None:
            cls._interpreter_agent = create_interpreter_agent()
        return cls._interpreter_agent

    @classmethod
    def get_data_extractor_agent(cls) -> Agent:
        """Get or create the data extractor agent."""
        if cls._data_extractor_agent is None:
            cls._data_extractor_agent = create_data_extractor_agent()
        return cls._data_extractor_agent

    @classmethod
    def reset(cls) -> None:
        """Reset all cached agents. Useful for testing."""
        cls._classifier_agent = None
        cls._interpreter_agent = None
        cls._data_extractor_agent = None
