"""Tests for Config class provider validation edge cases.

Phase 84: Config.llm_provider, Config.openai_api_key removed in Phase 82.
Provider is now encoded in model name prefix (anthropic:, openai:, ollama:).
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: Config.llm_provider/openai_api_key removed in Phase 82, provider encoded in model name prefix")


class TestConfigProviderEdgeCases:
    """Edge cases for provider configuration."""

    def test_anthropic_api_key_not_required_for_openai(self, monkeypatch):
        """Can create Config with openai provider and no anthropic key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="openai",
            openai_api_key="sk-test",
            anthropic_api_key=None,
        )
        assert config.anthropic_api_key is None
        assert config.llm_provider == "openai"

    def test_ollama_default_model_is_qwen(self):
        """Ollama provider defaults to qwen2.5:32b model (D-08)."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="ollama",
            ollama_base_url="http://localhost:11434/v1",
        )
        assert "qwen2.5:32b" in config.llm_model

    def test_heavy_roles_get_heavy_model(self):
        """Heavy reasoning roles get the heavy model variant."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="openai",
            openai_api_key="sk-test",
        )
        # Heavy roles should get gpt-4o
        assert config.llm_model == "openai:gpt-4o"
        assert config.interpreter_model == "openai:gpt-4o"
        # Utility roles should get gpt-4o-mini
        assert config.classifier_model == "openai:gpt-4o-mini"
        assert config.data_extractor_model == "openai:gpt-4o-mini"

    def test_openai_api_key_not_required_for_ollama(self):
        """Can create Config with ollama provider and no openai key."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="ollama",
            ollama_base_url="http://localhost:11434/v1",
        )
        assert config.openai_api_key is None
        assert config.llm_provider == "ollama"

    def test_provider_case_sensitivity(self):
        """Provider value is case-sensitive -- uppercase rejected."""
        from meho_app.core.config import Config

        with pytest.raises(ValueError, match="must be"):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="Anthropic",
            )
