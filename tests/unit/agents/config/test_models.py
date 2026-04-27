# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ModelConfig and model parameter classes.

These tests verify:
1. Model type detection works correctly
2. InstructParams and ReasoningParams have correct defaults
3. ModelConfig.from_dict() parses YAML-like input
4. get_pydantic_ai_kwargs() returns correct params for each type
"""

from __future__ import annotations

import pytest

from meho_app.modules.agents.config import (
    InstructParams,
    ModelConfig,
    ReasoningParams,
    detect_model_type,
)
from meho_app.modules.agents.config.models import AdaptiveThinkingParams


class TestDetectModelType:
    """Tests for detect_model_type function."""

    def test_detect_gpt4o_mini_as_instruct(self) -> None:
        """gpt-4.1-mini should be detected as instruct."""
        assert detect_model_type("openai:gpt-4.1-mini") == "instruct"
        assert detect_model_type("gpt-4.1-mini") == "instruct"

    def test_detect_gpt4o_as_instruct(self) -> None:
        """gpt-4.1 should be detected as instruct."""
        assert detect_model_type("openai:gpt-4.1") == "instruct"

    def test_detect_gpt41_as_instruct(self) -> None:
        """gpt-4.1 should be detected as instruct."""
        assert detect_model_type("openai:gpt-4.1") == "instruct"

    def test_detect_claude_as_instruct(self) -> None:
        """Claude models should be detected as instruct."""
        assert detect_model_type("anthropic:claude-3-opus") == "instruct"
        assert detect_model_type("anthropic:claude-3.5-sonnet") == "instruct"
        assert detect_model_type("claude-3-haiku") == "instruct"

    def test_detect_o1_as_reasoning(self) -> None:
        """o1 models should be detected as reasoning."""
        assert detect_model_type("openai:o1") == "reasoning"
        assert detect_model_type("openai:o1-mini") == "reasoning"
        assert detect_model_type("openai:o1-preview") == "reasoning"

    def test_detect_o3_as_reasoning(self) -> None:
        """o3 models should be detected as reasoning."""
        assert detect_model_type("openai:o3") == "reasoning"
        assert detect_model_type("openai:o3-mini") == "reasoning"

    def test_detect_unknown_defaults_to_instruct(self) -> None:
        """Unknown models should default to instruct."""
        assert detect_model_type("some-unknown-model") == "instruct"
        assert detect_model_type("custom:my-model") == "instruct"

    def test_detect_is_case_insensitive(self) -> None:
        """Detection should be case insensitive."""
        assert detect_model_type("openai:GPT-4.1-MINI") == "instruct"
        assert detect_model_type("OPENAI:O1") == "reasoning"


class TestInstructParams:
    """Tests for InstructParams dataclass."""

    def test_default_values(self) -> None:
        """InstructParams should have correct defaults."""
        params = InstructParams()
        assert params.temperature == pytest.approx(0.0)
        assert params.top_p == pytest.approx(1.0)
        assert params.max_output_tokens == 4096

    def test_custom_values(self) -> None:
        """InstructParams should accept custom values."""
        params = InstructParams(temperature=0.5, top_p=0.9, max_output_tokens=8192)
        assert params.temperature == pytest.approx(0.5)
        assert params.top_p == pytest.approx(0.9)
        assert params.max_output_tokens == 8192

    def test_to_pydantic_ai_kwargs(self) -> None:
        """to_pydantic_ai_kwargs should return correct dict."""
        params = InstructParams(temperature=0.2, top_p=0.95, max_output_tokens=4096)
        kwargs = params.to_pydantic_ai_kwargs()

        assert kwargs["temperature"] == pytest.approx(0.2)
        assert kwargs["top_p"] == pytest.approx(0.95)
        assert kwargs["max_tokens"] == 4096


class TestReasoningParams:
    """Tests for ReasoningParams dataclass."""

    def test_default_values(self) -> None:
        """ReasoningParams should have correct defaults."""
        params = ReasoningParams()
        assert params.reasoning_effort == "medium"
        assert params.max_completion_tokens == 16384

    def test_custom_values(self) -> None:
        """ReasoningParams should accept custom values."""
        params = ReasoningParams(reasoning_effort="high", max_completion_tokens=32000)
        assert params.reasoning_effort == "high"
        assert params.max_completion_tokens == 32000

    def test_to_pydantic_ai_kwargs(self) -> None:
        """to_pydantic_ai_kwargs should return correct dict."""
        params = ReasoningParams(reasoning_effort="high", max_completion_tokens=20000)
        kwargs = params.to_pydantic_ai_kwargs()

        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["max_completion_tokens"] == 20000
        # Should NOT have temperature or top_p
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs


class TestAdaptiveThinkingParams:
    """Tests for AdaptiveThinkingParams dataclass."""

    def test_default_values(self) -> None:
        """AdaptiveThinkingParams should have correct defaults."""
        params = AdaptiveThinkingParams()
        assert params.effort == "high"
        assert params.max_output_tokens == 16384

    def test_to_pydantic_ai_kwargs(self) -> None:
        """to_pydantic_ai_kwargs should return correct dict."""
        params = AdaptiveThinkingParams(
            effort="medium",
            max_output_tokens=8000,
        )
        kwargs = params.to_pydantic_ai_kwargs()

        assert kwargs["thinking"]["type"] == "adaptive"
        assert kwargs["effort"] == "medium"
        assert kwargs["max_tokens"] == 8000


class TestModelConfig:
    """Tests for ModelConfig class."""

    def test_create_with_name_only(self) -> None:
        """ModelConfig should work with just name."""
        config = ModelConfig(name="openai:gpt-4.1-mini")
        assert config.name == "openai:gpt-4.1-mini"
        assert config.model_type == "instruct"

    def test_model_type_property(self) -> None:
        """model_type should be auto-detected."""
        instruct_config = ModelConfig(name="openai:gpt-4.1")
        assert instruct_config.model_type == "instruct"

        reasoning_config = ModelConfig(name="openai:o1")
        assert reasoning_config.model_type == "reasoning"

    def test_is_reasoning_property(self) -> None:
        """is_reasoning should return True for reasoning models."""
        assert ModelConfig(name="openai:o1").is_reasoning is True
        assert ModelConfig(name="openai:gpt-4.1").is_reasoning is False


class TestModelConfigFromDict:
    """Tests for ModelConfig.from_dict()."""

    def test_from_dict_with_string(self) -> None:
        """from_dict should accept string (model name only)."""
        config = ModelConfig.from_dict("openai:gpt-4.1-mini")
        assert config.name == "openai:gpt-4.1-mini"
        assert config.temperature is None  # Uses default from params

    def test_from_dict_with_dict(self) -> None:
        """from_dict should accept dictionary."""
        config = ModelConfig.from_dict(
            {
                "name": "openai:gpt-4.1-mini",
                "temperature": 0.3,
                "max_output_tokens": 8000,
            }
        )
        assert config.name == "openai:gpt-4.1-mini"
        assert config.temperature == pytest.approx(0.3)
        assert config.max_output_tokens == 8000

    def test_from_dict_with_reasoning_model(self) -> None:
        """from_dict should work with reasoning model config."""
        config = ModelConfig.from_dict(
            {
                "name": "openai:o1",
                "reasoning_effort": "high",
                "max_output_tokens": 32000,
            }
        )
        assert config.name == "openai:o1"
        assert config.reasoning_effort == "high"
        assert config.model_type == "reasoning"


class TestModelConfigGetPydanticAiKwargs:
    """Tests for get_pydantic_ai_kwargs()."""

    def test_instruct_model_kwargs(self) -> None:
        """get_pydantic_ai_kwargs should return instruct params."""
        config = ModelConfig.from_dict(
            {
                "name": "openai:gpt-4.1-mini",
                "temperature": 0.2,
            }
        )
        kwargs = config.get_pydantic_ai_kwargs()

        assert kwargs["temperature"] == pytest.approx(0.2)
        assert kwargs["top_p"] == pytest.approx(1.0)
        assert kwargs["max_tokens"] == 4096

    def test_instruct_model_default_kwargs(self) -> None:
        """get_pydantic_ai_kwargs should use defaults when not specified."""
        config = ModelConfig.from_dict("openai:gpt-4.1-mini")
        kwargs = config.get_pydantic_ai_kwargs()

        assert kwargs["temperature"] == pytest.approx(0.0)
        assert kwargs["top_p"] == pytest.approx(1.0)
        assert kwargs["max_tokens"] == 4096

    def test_reasoning_model_kwargs(self) -> None:
        """get_pydantic_ai_kwargs should return reasoning params."""
        config = ModelConfig.from_dict(
            {
                "name": "openai:o1",
                "reasoning_effort": "high",
                "max_output_tokens": 32000,
            }
        )
        kwargs = config.get_pydantic_ai_kwargs()

        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["max_completion_tokens"] == 32000
        # Should NOT have temperature
        assert "temperature" not in kwargs

    def test_reasoning_model_default_kwargs(self) -> None:
        """get_pydantic_ai_kwargs should use reasoning defaults."""
        config = ModelConfig.from_dict("openai:o1")
        kwargs = config.get_pydantic_ai_kwargs()

        assert kwargs["reasoning_effort"] == "medium"
        assert kwargs["max_completion_tokens"] == 16384


class TestModelConfigToDict:
    """Tests for to_dict() method."""

    def test_to_dict_minimal(self) -> None:
        """to_dict should include only name for minimal config."""
        config = ModelConfig(name="openai:gpt-4.1-mini")
        d = config.to_dict()
        assert d == {"name": "openai:gpt-4.1-mini"}

    def test_to_dict_with_overrides(self) -> None:
        """to_dict should include specified overrides."""
        config = ModelConfig(
            name="openai:gpt-4.1-mini",
            temperature=0.5,
            max_output_tokens=8000,
        )
        d = config.to_dict()
        assert d["name"] == "openai:gpt-4.1-mini"
        assert d["temperature"] == pytest.approx(0.5)
        assert d["max_output_tokens"] == 8000
        assert "top_p" not in d  # Not specified


class TestModelConfigImports:
    """Tests for module imports."""

    def test_importable_from_config(self) -> None:
        """Classes should be importable from config module."""
        from meho_app.modules.agents.config import (
            InstructParams,
            ModelConfig,
            ReasoningParams,
            detect_model_type,
        )

        assert ModelConfig is not None
        assert InstructParams is not None
        assert ReasoningParams is not None
        assert detect_model_type is not None
