# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test configuration helper that bypasses .env file.

For E2E tests where .env file might not be accessible.
Sets environment variables programmatically.
"""

import os
from contextlib import contextmanager


class TestConfig:
    """Test configuration that sets env vars programmatically"""

    DEFAULT_TEST_CONFIG = {  # noqa: RUF012 -- mutable default is intentional test state
        # Database (use correct credentials from docker-compose.test.yml)
        "DATABASE_URL": "postgresql+asyncpg://meho:password@localhost:5432/meho_test",
        # VECTOR_DB_URL removed - Qdrant replaced by pgvector (Session 15)
        # Object Storage (MinIO)
        "OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "OBJECT_STORAGE_BUCKET": "meho-test",
        "OBJECT_STORAGE_ACCESS_KEY": "minioadmin",
        "OBJECT_STORAGE_SECRET_KEY": "minioadmin",
        # Cache
        "REDIS_URL": "redis://localhost:6379/0",
        # MESSAGE_BROKER_URL removed - RabbitMQ removed (Session 35)
        # Security (valid Fernet key - 32 bytes base64-encoded)
        "CREDENTIAL_ENCRYPTION_KEY": "jN2FQq7mFgpzEUwKj9fP3yYzq8vH5xR4sT6uV7wX8zA=",
        # API - JWT_SECRET_KEY is loaded from .env (not set here)
        # This ensures integration tests use the same secret as Docker containers
        # Environment
        "ENVIRONMENT": "test",
        "ENV": "test",
    }

    @classmethod
    def set_test_env(cls, overrides: dict[str, str] | None = None) -> None:
        """
        Set test environment variables.

        Args:
            overrides: Optional dict to override default test config
        """
        config = cls.DEFAULT_TEST_CONFIG.copy()
        if overrides:
            config.update(overrides)

        for key, value in config.items():
            os.environ[key] = value

    @classmethod
    @contextmanager
    def test_env_context(cls, overrides: dict[str, str] | None = None):  # noqa: PT028 -- intentional default value in fixture
        """
        Context manager that temporarily sets test env vars.

        Usage:
            with TestConfig.test_env_context():
                # Test code here
                # Environment variables set
            # Environment restored

        Args:
            overrides: Optional dict to override default test config
        """
        # Save original env
        original_env = os.environ.copy()

        try:
            # Set test env
            cls.set_test_env(overrides)
            yield
        finally:
            # Restore original env
            os.environ.clear()
            os.environ.update(original_env)

    @classmethod
    def get_test_database_url(cls) -> str:
        """Get test database URL"""
        return cls.DEFAULT_TEST_CONFIG["DATABASE_URL"]


def setup_test_environment(anthropic_api_key: str | None = None) -> None:
    """
    Setup complete test environment for E2E tests.

    Call this at the start of E2E tests to ensure all env vars are set.

    Args:
        anthropic_api_key: Optional Anthropic API key (if not in env already)
    """
    TestConfig.set_test_env()

    # Set Anthropic key if provided
    if anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key

    # Verify Anthropic key is set
    if not os.getenv("ANTHROPIC_API_KEY"):
        import warnings

        warnings.warn(
            "ANTHROPIC_API_KEY not set. E2E tests that use LLM will fail. "
            "Set it with: export ANTHROPIC_API_KEY='sk-ant-...'",
            stacklevel=2,
        )
