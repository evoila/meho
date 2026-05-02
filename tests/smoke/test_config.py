# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke test: Configuration validation.

Ensures that configuration can be loaded and has required fields.
"""

import os


def test_core_config_loads():
    """Test that core config can be loaded"""
    from meho_app.core.config import get_config

    config = get_config()

    # Check essential fields exist
    assert hasattr(config, "log_level")
    assert hasattr(config, "env")


def test_api_config_loads():
    """Test that API config can be loaded"""
    from meho_app.api.config import get_api_config

    config = get_api_config()

    # Check essential fields
    assert hasattr(config, "agent_service_url")
    assert hasattr(config, "knowledge_service_url")
    assert hasattr(config, "openapi_service_url")


def test_knowledge_config_has_required_fields():
    """Test that knowledge service config has required fields"""
    from meho_app.core.config import get_config

    config = get_config()

    # These are required for knowledge service to function
    assert hasattr(config, "database_url")
    assert hasattr(config, "anthropic_api_key")

    # These can be None but must exist
    assert hasattr(config, "object_storage_endpoint")


def test_required_env_vars_documented():
    """
    Test that we can detect missing required environment variables.

    This doesn't fail if they're missing (test env might not have them),
    but documents what's expected.
    """
    required_vars = [
        "DATABASE_URL",
    ]

    optional_vars = [
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
        "ANTHROPIC_API_KEY",  # Optional for community mode
    ]

    # Test passes - this documents what environment variables are checked
    # All required vars must be present, optional vars are documented
    assert all(var in os.environ or var in optional_vars for var in required_vars + optional_vars)


def test_config_types_correct():
    """Test that config values have correct types"""
    from meho_app.core.config import get_config

    config = get_config()

    # log_level should be string
    if hasattr(config, "log_level"):
        assert isinstance(config.log_level, str)

    # env should be string
    if hasattr(config, "env"):
        assert isinstance(config.env, str)


def test_database_url_format():
    """Test that database URL is in correct format (if present)"""
    from meho_app.core.config import get_config

    config = get_config()

    if hasattr(config, "database_url") and config.database_url:
        db_url = config.database_url
        # Should start with postgresql://
        assert db_url.startswith("postgresql://") or db_url.startswith("postgresql+asyncpg://"), (
            f"Database URL should use postgresql:// scheme, got: {db_url[:20]}..."
        )
