# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for get_model_settings factory (LLM-02)."""

from unittest.mock import MagicMock, patch

from meho_app.modules.agents.agent_factories import get_model_settings


class TestGetModelSettings:
    """Test centralized model settings factory."""

    def test_anthropic_returns_anthropic_model_settings(self):
        """Anthropic provider returns AnthropicModelSettings instance."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="classifier")
            assert settings is not None
            # AnthropicModelSettings is a TypedDict -- use dict access
            assert settings.get("anthropic_cache_instructions") is True

    def test_openai_returns_none(self):
        """OpenAI provider returns None (no special settings needed)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "openai"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="classifier")
            assert settings is None

    def test_ollama_returns_none(self):
        """Ollama provider returns None (no special settings needed)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "ollama"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="classifier")
            assert settings is None

    def test_classifier_gets_low_effort(self):
        """Classifier task type gets effort='low' for Anthropic."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="classifier")
            assert settings.get("anthropic_effort") == "low"

    def test_interpreter_gets_high_effort(self):
        """Interpreter task type gets effort='high' for Anthropic."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="interpreter")
            assert settings.get("anthropic_effort") == "high"

    def test_synthesis_gets_high_effort(self):
        """Synthesis task type gets effort='high' for Anthropic."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="synthesis")
            assert settings.get("anthropic_effort") == "high"

    def test_specialist_gets_low_effort(self):
        """Specialist task type gets effort='low' for Anthropic."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="specialist")
            assert settings.get("anthropic_effort") == "low"

    def test_no_anthropic_import_for_non_anthropic(self):
        """Non-Anthropic provider never imports AnthropicModelSettings."""
        mock_config = MagicMock()
        mock_config.llm_provider = "openai"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            # Should return None without touching AnthropicModelSettings
            result = get_model_settings(task_type="inference")
            assert result is None

    def test_all_task_types_return_cache_instructions(self):
        """All task types for Anthropic include cache_instructions=True."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            for task in (
                "classifier",
                "interpreter",
                "extractor",
                "synthesis",
                "specialist",
                "inference",
            ):
                settings = get_model_settings(task_type=task)
                assert settings.get("anthropic_cache_instructions") is True, (
                    f"Cache not set for {task}"
                )

    # Phase 89.1: Cache optimization tests (D-06, D-07, D-08)

    def test_specialist_has_cache_tool_definitions(self):
        """Specialist task type includes anthropic_cache_tool_definitions=True (D-06)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="specialist")
            assert settings.get("anthropic_cache_tool_definitions") is True

    def test_specialist_has_cache_messages(self):
        """Specialist task type includes anthropic_cache_messages=True (D-07)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="specialist")
            assert settings.get("anthropic_cache_messages") is True

    def test_classifier_no_cache_tool_definitions(self):
        """Classifier (single-turn) should NOT have cache_tool_definitions (D-08)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="classifier")
            # Should be absent or False
            assert not settings.get("anthropic_cache_tool_definitions")

    def test_interpreter_no_cache_messages(self):
        """Interpreter (single-turn) should NOT have cache_messages (D-08)."""
        mock_config = MagicMock()
        mock_config.llm_provider = "anthropic"

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            settings = get_model_settings(task_type="interpreter")
            # Should be absent or False
            assert not settings.get("anthropic_cache_messages")
