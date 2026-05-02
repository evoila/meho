# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Model configuration by type.

Each model family has its own parameter class.
ModelConfig auto-detects the right params based on model name.

Example:
    >>> config = ModelConfig.from_dict({"name": "anthropic:claude-opus-4-6"})
    >>> config.model_type
    'instruct'
    >>> config.get_pydantic_ai_kwargs()
    {'temperature': 0.0, 'top_p': 1.0, 'max_tokens': 4096}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

# ─────────────────────────────────────────────────────────────────────────────
# Model Type Definitions
# ─────────────────────────────────────────────────────────────────────────────

ModelType = Literal["instruct", "reasoning"]

# Known model patterns -> type mapping (longest patterns first for matching)
MODEL_TYPE_PATTERNS: dict[str, ModelType] = {
    # Anthropic (all instruct, adaptive thinking handled separately)
    "claude-opus-4": "instruct",
    "claude-sonnet-4": "instruct",
    "claude-3.5-sonnet": "instruct",
    "claude-3-opus": "instruct",
    "claude-3-sonnet": "instruct",
    "claude-3-haiku": "instruct",
    "claude-3.5": "instruct",
    "claude-3": "instruct",
    # OpenAI (Phase 82: Multi-LLM Support)
    "gpt-4o": "instruct",
    "gpt-4o-mini": "instruct",
    "gpt-4-turbo": "instruct",
    "gpt-4": "instruct",
    "gpt-3.5": "instruct",
    "o1": "reasoning",
    "o1-mini": "reasoning",
    "o3": "reasoning",
    "o3-mini": "reasoning",
    "o4-mini": "reasoning",
    # Ollama common models (Phase 82: Multi-LLM Support)
    "qwen2.5": "instruct",
    "qwen3": "instruct",
    "llama3": "instruct",
    "llama3.1": "instruct",
    "deepseek-r1": "reasoning",
    "deepseek-v3": "instruct",
    "gemma2": "instruct",
    "mistral": "instruct",
    "mixtral": "instruct",
    # Embeddings handled via separate Voyage AI pipeline, not this config
}


def detect_model_type(model_name: str) -> ModelType:
    """Detect model type from model name.

    Args:
        model_name: Full model name (e.g., "anthropic:claude-opus-4-6")

    Returns:
        ModelType: "instruct" or "reasoning"
    """
    # Extract model part after provider prefix
    name = model_name.split(":")[-1].lower()

    # Check patterns (longest match first)
    for pattern, model_type in sorted(
        MODEL_TYPE_PATTERNS.items(),
        key=lambda x: -len(x[0]),
    ):
        if pattern in name:
            return model_type

    # Default to instruct
    return "instruct"


# ─────────────────────────────────────────────────────────────────────────────
# Parameter Classes (dataclasses)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BaseModelParams(ABC):
    """Base class for model parameters."""

    @abstractmethod
    def to_pydantic_ai_kwargs(self) -> dict[str, Any]:
        """Convert to kwargs for PydanticAI Agent."""
        ...


@dataclass
class InstructParams(BaseModelParams):
    """Parameters for instruct/chat models.

    Used by: Claude Opus 4.6, Claude Sonnet 4.6, Claude 3.x series, etc.
    """

    temperature: float = 0.0
    top_p: float = 1.0
    max_output_tokens: int = 4096

    def to_pydantic_ai_kwargs(self) -> dict[str, Any]:
        """Convert to kwargs for PydanticAI Agent.

        Returns:
            Dictionary of kwargs for PydanticAI.
        """
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_output_tokens,
        }


@dataclass
class ReasoningParams(BaseModelParams):
    """Parameters for reasoning models (chain-of-thought).

    Architecture pattern for models that use internal reasoning instead
    of temperature/top_p. Kept extensible for future provider support.
    """

    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    max_completion_tokens: int = 16384  # Includes reasoning + output

    def to_pydantic_ai_kwargs(self) -> dict[str, Any]:
        """Convert to kwargs for PydanticAI Agent.

        Returns:
            Dictionary of kwargs for PydanticAI.
        """
        return {
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_completion_tokens,
        }


@dataclass
class AdaptiveThinkingParams(BaseModelParams):
    """Parameters for Claude adaptive thinking (Opus 4.6+).

    Used by: Anthropic Claude models with adaptive thinking.
    Replaces the deprecated ExtendedThinkingParams (budget_tokens approach).
    """

    effort: Literal["low", "medium", "high"] = "high"
    max_output_tokens: int = 16384

    def to_pydantic_ai_kwargs(self) -> dict[str, Any]:
        """Convert to kwargs for PydanticAI Agent.

        Returns:
            Dictionary of kwargs for PydanticAI.
        """
        return {
            "thinking": {"type": "adaptive"},
            "effort": self.effort,
            "max_tokens": self.max_output_tokens,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main ModelConfig
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ModelConfig:
    """Complete model configuration.

    Auto-detects model type and provides appropriate defaults.
    Parameters can be overridden via YAML config.

    Example:
        >>> config = ModelConfig.from_dict({"name": "anthropic:claude-opus-4-6"})
        >>> kwargs = config.get_pydantic_ai_kwargs()
        >>> agent = Agent(config.name, **kwargs)
    """

    name: str  # e.g., "anthropic:claude-opus-4-6"

    # Optional overrides (if None, use defaults for model type)
    _params: BaseModelParams | None = field(default=None, repr=False)

    # Override fields (from YAML)
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    effort: Literal["low", "medium", "high"] | None = None

    def __post_init__(self) -> None:
        """Build params after init."""
        self._params = self._build_params()

    @property
    def model_type(self) -> ModelType:
        """Detected model type."""
        return detect_model_type(self.name)

    @property
    def is_reasoning(self) -> bool:
        """Is this a reasoning model?"""
        return self.model_type == "reasoning"

    @property
    def params(self) -> BaseModelParams:
        """Get the parameter object."""
        if self._params is None:
            self._params = self._build_params()
        return self._params

    def _build_params(self) -> BaseModelParams:
        """Build appropriate params based on model type."""
        model_type = self.model_type

        if model_type == "reasoning":
            return ReasoningParams(
                reasoning_effort=self.reasoning_effort or "medium",
                max_completion_tokens=self.max_output_tokens or 16384,
            )
        else:  # instruct
            return InstructParams(
                temperature=self.temperature if self.temperature is not None else 0.0,
                top_p=self.top_p if self.top_p is not None else 1.0,
                max_output_tokens=self.max_output_tokens or 4096,
            )

    def get_pydantic_ai_kwargs(self) -> dict[str, Any]:
        """Get kwargs for PydanticAI Agent constructor.

        Returns:
            Dictionary of kwargs to pass to PydanticAI Agent.
        """
        return self.params.to_pydantic_ai_kwargs()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> ModelConfig:
        """Create from dictionary (YAML config) or string (model name only).

        Args:
            data: Either a string (model name) or dict with config.

        Returns:
            Configured ModelConfig instance.
        """
        if isinstance(data, str):
            # Simple string: just model name
            return cls(name=data)
        return cls(
            name=data["name"],
            temperature=data.get("temperature"),
            top_p=data.get("top_p"),
            max_output_tokens=data.get("max_output_tokens"),
            reasoning_effort=data.get("reasoning_effort"),
            effort=data.get("effort"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation of the config.
        """
        result: dict[str, Any] = {"name": self.name}
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.top_p is not None:
            result["top_p"] = self.top_p
        if self.max_output_tokens is not None:
            result["max_output_tokens"] = self.max_output_tokens
        if self.reasoning_effort is not None:
            result["reasoning_effort"] = self.reasoning_effort
        if self.effort is not None:
            result["effort"] = self.effort
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Quick Reference
# ─────────────────────────────────────────────────────────────────────────────
"""
Model Type Detection:

+---------------------+-------------+---------------------------------+
| Model               | Type        | Parameters                      |
+---------------------+-------------+---------------------------------+
| claude-opus-4-6     | instruct    | temperature, top_p, max_tokens  |
| claude-sonnet-4-6   | instruct    | temperature, top_p, max_tokens  |
| claude-3-opus       | instruct    | temperature, top_p, max_tokens  |
| claude-3.5-sonnet   | instruct    | temperature, top_p, max_tokens  |
| claude-3-haiku      | instruct    | temperature, top_p, max_tokens  |
| gpt-4o              | instruct    | temperature, top_p, max_tokens  |
| gpt-4o-mini         | instruct    | temperature, top_p, max_tokens  |
| o1 / o3             | reasoning   | reasoning_effort, max_tokens    |
| qwen2.5             | instruct    | temperature, top_p, max_tokens  |
| llama3 / llama3.1   | instruct    | temperature, top_p, max_tokens  |
| deepseek-r1         | reasoning   | reasoning_effort, max_tokens    |
| deepseek-v3         | instruct    | temperature, top_p, max_tokens  |
| mistral / mixtral   | instruct    | temperature, top_p, max_tokens  |
+---------------------+-------------+---------------------------------+

YAML Examples:

# Simple (model name resolved from LLM_MODEL env var)
model:
  temperature: 0.0

# With explicit model override
model:
  name: "anthropic:claude-opus-4-6"
  temperature: 0.2
  max_output_tokens: 8192
"""
