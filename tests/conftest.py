# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Root conftest.py for all tests.

SLIM: env setup, LLM guard, and logging only.
Layer-specific fixtures live in layer-specific conftest files:
  - tests/unit/conftest.py       -- mock-only fixtures
  - tests/integration/conftest.py -- db_session, tenant, API client fixtures
  - tests/e2e/conftest.py        -- real services, LLM override helpers
"""

# CRITICAL: Prevent accidental LLM calls in ALL tests.
# Must be set before any agent imports that could cache the value.
import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

import os
from pathlib import Path

import pytest

# Setup test environment FIRST (before any imports that load Config)
from tests.support.test_config import TestConfig

TestConfig.set_test_env()

# Load test environment variables
from dotenv import load_dotenv  # noqa: E402 -- conditional/deferred import for test setup

# Load from project root .env first (for ANTHROPIC_API_KEY and other secrets)
# Then test-specific settings override (e.g., test database URL)
# This ensures tests use test infrastructure but have access to API keys
project_root_env = Path(__file__).parent.parent / ".env"
if project_root_env.exists():
    load_dotenv(project_root_env)

# Load test-specific overrides (these take precedence)
test_env_file = Path(__file__).parent / ".env.test"
if test_env_file.exists():
    load_dotenv(test_env_file, override=True)


# ============================================================================
# Logging Configuration
# ============================================================================


@pytest.fixture(scope="session", autouse=True)
def configure_test_logging():
    """Configure logging for tests"""
    import logging

    # Set WARNING level for tests (reduce noise)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")

    # Suppress noisy loggers
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.ERROR)


# ============================================================================
# Test Environment Setup
# ============================================================================


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up test environment variables - MUST run before any imports!"""
    # CRITICAL: Set these BEFORE any code tries to load config
    os.environ["ENV"] = "test"
    os.environ["LOG_LEVEL"] = "WARNING"

    # FORCE test database configuration (override .env)
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
    # VECTOR_DB_URL removed - using pgvector in PostgreSQL (Session 15 migration)
    os.environ["OBJECT_STORAGE_ENDPOINT"] = "localhost:9000"
    os.environ["OBJECT_STORAGE_BUCKET"] = "test"
    os.environ["OBJECT_STORAGE_ACCESS_KEY"] = "minioadmin"
    os.environ["OBJECT_STORAGE_SECRET_KEY"] = "minioadmin"
    os.environ["REDIS_URL"] = "redis://localhost:6379"
    os.environ["CREDENTIAL_ENCRYPTION_KEY"] = (
        "IhtR4iZA6r7dV0h2KwHKD9Z8RztSGGwOJG_CrXFQ7Zw="  # Valid Fernet key
    )

    # Reset config cache to force reload with test values
    from meho_app.core.config import reset_config

    reset_config()
