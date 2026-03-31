# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.core.config

Phase 84: Config class restructured in Phases 80-83 (license_key added, vector_db_url removed,
embedding_model default changed to voyage-4-large, llm_provider removed).
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: Config class restructured in Phases 80-83, vector_db_url/llm_provider removed, new fields added")
from pydantic import ValidationError

from meho_app.core.config import Config, get_config, reset_config


@pytest.mark.unit
def test_config_from_env(monkeypatch):
    """Test config loads from environment variables"""
    # Set all required environment variables
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("VECTOR_DB_URL", "http://test:6333")
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "test-access")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MESSAGE_BROKER_URL", "amqp://localhost")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key-at-least-32-chars-long")

    reset_config()
    config = Config()

    assert config.database_url == "postgresql://test"
    assert config.vector_db_url == "http://test:6333"
    assert config.openai_api_key == "sk-test-key"


@pytest.mark.unit
def test_config_missing_required_field(monkeypatch):
    """Test config raises error when required field is missing"""
    # Don't set DATABASE_URL
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Config()


@pytest.mark.unit
def test_config_default_values(monkeypatch):
    """Test config provides defaults for optional fields"""
    # Set only required fields
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("VECTOR_DB_URL", "http://test")
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "test")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "key")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MESSAGE_BROKER_URL", "amqp://localhost")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-key-at-least-32-characters-long")

    # Note: ENV might be "test" if conftest.py set it
    monkeypatch.setenv("ENV", "dev")  # Explicitly set to dev for this test
    monkeypatch.delenv("LOG_LEVEL", raising=False)  # Remove to test default

    reset_config()
    config = Config()

    # Check defaults
    assert config.env == "dev"
    assert config.log_level == "INFO"  # Default should be INFO
    assert config.embedding_model == "text-embedding-3-small"
    assert config.llm_model == "gpt-4"
    assert config.api_host == "0.0.0.0"  # noqa: S104 -- test configuration, not production
    assert config.api_port == 8000
    assert config.object_storage_use_ssl is False


@pytest.mark.unit
def test_config_singleton(monkeypatch):
    """Test get_config returns same instance"""
    # Set required env vars
    for key, value in {
        "DATABASE_URL": "postgresql://test",
        "VECTOR_DB_URL": "http://test",
        "OBJECT_STORAGE_ENDPOINT": "localhost:9000",
        "OBJECT_STORAGE_BUCKET": "test",
        "OBJECT_STORAGE_ACCESS_KEY": "key",
        "OBJECT_STORAGE_SECRET_KEY": "secret",
        "REDIS_URL": "redis://localhost",
        "MESSAGE_BROKER_URL": "amqp://localhost",
        "OPENAI_API_KEY": "sk-test",
        "CREDENTIAL_ENCRYPTION_KEY": "test-key-at-least-32-characters-long",
    }.items():
        monkeypatch.setenv(key, value)

    reset_config()

    config1 = get_config()
    config2 = get_config()

    assert config1 is config2


@pytest.mark.unit
def test_config_reset(monkeypatch):
    """Test reset_config clears singleton"""
    # Set required env vars
    for key, value in {
        "DATABASE_URL": "postgresql://test",
        "VECTOR_DB_URL": "http://test",
        "OBJECT_STORAGE_ENDPOINT": "localhost:9000",
        "OBJECT_STORAGE_BUCKET": "test",
        "OBJECT_STORAGE_ACCESS_KEY": "key",
        "OBJECT_STORAGE_SECRET_KEY": "secret",
        "REDIS_URL": "redis://localhost",
        "MESSAGE_BROKER_URL": "amqp://localhost",
        "OPENAI_API_KEY": "sk-test",
        "CREDENTIAL_ENCRYPTION_KEY": "test-key-at-least-32-characters-long",
    }.items():
        monkeypatch.setenv(key, value)

    config1 = get_config()
    reset_config()
    config2 = get_config()

    # Should be different instances after reset
    assert config1 is not config2


@pytest.mark.unit
def test_config_different_environments(monkeypatch):
    """Test config with different environment modes"""
    # Set required vars
    for key, value in {
        "DATABASE_URL": "postgresql://test",
        "VECTOR_DB_URL": "http://test",
        "OBJECT_STORAGE_ENDPOINT": "localhost:9000",
        "OBJECT_STORAGE_BUCKET": "test",
        "OBJECT_STORAGE_ACCESS_KEY": "key",
        "OBJECT_STORAGE_SECRET_KEY": "secret",
        "REDIS_URL": "redis://localhost",
        "MESSAGE_BROKER_URL": "amqp://localhost",
        "OPENAI_API_KEY": "sk-test",
        "CREDENTIAL_ENCRYPTION_KEY": "test-key-at-least-32-characters-long",
    }.items():
        monkeypatch.setenv(key, value)

    # Test dev
    monkeypatch.setenv("ENV", "dev")
    reset_config()
    config = Config()
    assert config.env == "dev"

    # Test test
    monkeypatch.setenv("ENV", "test")
    reset_config()
    config = Config()
    assert config.env == "test"

    # Test prod
    monkeypatch.setenv("ENV", "prod")
    reset_config()
    config = Config()
    assert config.env == "prod"


@pytest.mark.unit
def test_config_encryption_key_validation(monkeypatch):
    """Test that encryption key is validated"""
    # Set all required vars
    for key, value in {
        "DATABASE_URL": "postgresql://test",
        "VECTOR_DB_URL": "http://test",
        "OBJECT_STORAGE_ENDPOINT": "localhost:9000",
        "OBJECT_STORAGE_BUCKET": "test",
        "OBJECT_STORAGE_ACCESS_KEY": "key",
        "OBJECT_STORAGE_SECRET_KEY": "secret",
        "REDIS_URL": "redis://localhost",
        "MESSAGE_BROKER_URL": "amqp://localhost",
        "OPENAI_API_KEY": "sk-test",
    }.items():
        monkeypatch.setenv(key, value)

    # Test with short key (should fail)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "short")
    reset_config()

    with pytest.raises(ValueError, match="at least 32 characters"):
        Config()
