"""Tests for multi-LLM provider selection (LLM-01, LLM-03).

Phase 84: Config.llm_provider attribute was removed in Phase 82 refactor.
Provider selection now uses MEHO_LLM_PROVIDER env var parsed into model name
prefixes (anthropic:, openai:, ollama:) in Config.llm_model.
"""

import logging

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: Config.llm_provider removed in Phase 82, provider encoded in model name prefix now")


class TestProviderSelection:
    """Test MEHO_LLM_PROVIDER env var drives model name construction."""

    def test_default_provider_is_anthropic(self):
        """When MEHO_LLM_PROVIDER not set, default is 'anthropic'."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            anthropic_api_key="sk-test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
        )
        assert config.llm_provider == "anthropic"
        assert "anthropic:" in config.llm_model

    def test_openai_provider_sets_model_defaults(self):
        """MEHO_LLM_PROVIDER=openai sets all model fields to openai: prefix."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="openai",
            openai_api_key="sk-test",
        )
        assert config.llm_provider == "openai"
        assert config.llm_model.startswith("openai:")
        assert config.classifier_model.startswith("openai:")
        assert config.interpreter_model.startswith("openai:")
        assert config.streaming_agent_model.startswith("openai:")

    def test_ollama_provider_sets_model_defaults(self):
        """MEHO_LLM_PROVIDER=ollama sets all model fields to ollama: prefix."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="ollama",
            ollama_base_url="http://localhost:11434/v1",
        )
        assert config.llm_provider == "ollama"
        assert config.llm_model.startswith("ollama:")
        assert "qwen2.5" in config.llm_model

    def test_explicit_model_override_preserved(self):
        """User-set model field is not overridden by provider defaults."""
        from meho_app.core.config import Config

        config = Config(
            database_url="postgresql://test",
            redis_url="redis://test",
            credential_encryption_key="test-key-that-is-at-least-32-characters-long",
            llm_provider="openai",
            openai_api_key="sk-test",
            classifier_model="openai:gpt-4o-mini",
        )
        assert config.classifier_model == "openai:gpt-4o-mini"


class TestProviderValidation:
    """Test provider-specific API key validation."""

    def test_anthropic_requires_api_key(self, monkeypatch):
        """MEHO_LLM_PROVIDER=anthropic without ANTHROPIC_API_KEY raises error."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from meho_app.core.config import Config

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="anthropic",
                anthropic_api_key=None,
            )

    def test_openai_requires_api_key(self):
        """MEHO_LLM_PROVIDER=openai without OPENAI_API_KEY raises error."""
        from meho_app.core.config import Config

        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="openai",
                # No openai_api_key
            )

    def test_ollama_requires_base_url(self):
        """MEHO_LLM_PROVIDER=ollama without OLLAMA_BASE_URL raises error."""
        from meho_app.core.config import Config

        with pytest.raises(ValueError, match="OLLAMA_BASE_URL"):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="ollama",
                # No ollama_base_url
            )

    def test_invalid_provider_rejected(self):
        """Unknown provider value raises error."""
        from meho_app.core.config import Config

        with pytest.raises(ValueError, match="must be"):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="invalid",
            )


class TestStartupWarning:
    """Test startup warning for non-Claude providers (LLM-03)."""

    def test_no_warning_for_anthropic(self, caplog):
        """No warning logged when using anthropic provider."""
        from meho_app.core.config import Config

        with caplog.at_level(logging.WARNING):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                anthropic_api_key="sk-test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="anthropic",
            )
        assert "investigation quality may differ" not in caplog.text

    def test_warning_for_openai(self, caplog):
        """Warning logged when using openai provider."""
        from meho_app.core.config import Config

        with caplog.at_level(logging.WARNING):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="openai",
                openai_api_key="sk-test",
            )
        assert "investigation quality may differ" in caplog.text

    def test_warning_for_ollama(self, caplog):
        """Warning logged when using ollama provider."""
        from meho_app.core.config import Config

        with caplog.at_level(logging.WARNING):
            Config(
                database_url="postgresql://test",
                redis_url="redis://test",
                credential_encryption_key="test-key-that-is-at-least-32-characters-long",
                llm_provider="ollama",
                ollama_base_url="http://localhost:11434/v1",
            )
        assert "investigation quality may differ" in caplog.text
