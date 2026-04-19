# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for AgentConfig.

Tests for remaining exports (AgentConfig, ModelConfig, DataReductionConfig, get_agent_config).
PromptBuilder and build_system_prompt were removed in Phase 84.
"""

import pytest

from meho_app.modules.agents.agent_config import (
    AgentConfig,
    DataReductionConfig,
    ModelConfig,
    get_agent_config,
)


class TestModelConfig:
    """Tests for ModelConfig."""

    def test_default_values(self):
        """Test ModelConfig has sensible defaults."""
        config = ModelConfig(name="openai:gpt-4.1-mini")

        assert config.name == "openai:gpt-4.1-mini"
        assert config.temperature == pytest.approx(0.7)
        assert config.max_tokens == 4096

    def test_custom_values(self):
        """Test ModelConfig accepts custom values."""
        config = ModelConfig(name="openai:gpt-4.1", temperature=0.5, max_tokens=8192)

        assert config.name == "openai:gpt-4.1"
        assert config.temperature == pytest.approx(0.5)
        assert config.max_tokens == 8192

    def test_temperature_validation(self):
        """Test temperature bounds validation."""
        # Valid values
        ModelConfig(name="test", temperature=0.0)
        ModelConfig(name="test", temperature=2.0)

        # Invalid values
        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            ModelConfig(name="test", temperature=-0.1)

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            ModelConfig(name="test", temperature=2.1)


class TestDataReductionConfig:
    """Tests for DataReductionConfig."""

    def test_default_values(self):
        """Test DataReductionConfig defaults."""
        config = DataReductionConfig()

        assert config.auto_reduce_threshold == 50
        assert config.auto_reduce_size_kb == 50


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @pytest.mark.asyncio
    async def test_get_agent_config(self):
        """Test get_agent_config convenience function."""
        config = await get_agent_config()

        assert isinstance(config, AgentConfig)
        assert config.model is not None
