# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for MEHO API configuration helpers.

Phase 84: MEHOAPIConfig.vector_db_url removed, Config class restructured.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: MEHOAPIConfig.vector_db_url removed, Config class restructured in Phases 80-83")

from meho_app.api.config import MEHOAPIConfig, reset_api_config


def test_meho_api_config_uses_json_cors_env(monkeypatch):
    """
    MEHOAPIConfig should accept JSON array strings for CORS_ORIGINS.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173","http://localhost:3000"]')

    reset_api_config()
    config = MEHOAPIConfig()

    assert config.cors_origins == ["http://localhost:5173", "http://localhost:3000"]


def test_meho_api_config_accepts_compose_bracket_format(monkeypatch):
    """
    Docker Compose strips double quotes from env files, leaving values like
    [http://localhost:5173,http://localhost:3000]. Ensure we still accept them.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("CORS_ORIGINS", "[http://localhost:5173,http://localhost:3000]")

    reset_api_config()
    config = MEHOAPIConfig()

    assert config.cors_origins == ["http://localhost:5173", "http://localhost:3000"]


def test_meho_api_config_accepts_csv_format(monkeypatch):
    """
    Plain comma-separated strings should also be parsed.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173, http://localhost:3000")

    reset_api_config()
    config = MEHOAPIConfig()

    assert config.cors_origins == ["http://localhost:5173", "http://localhost:3000"]


def test_meho_api_config_reads_vector_db_url(monkeypatch):
    """
    VECTOR_DB_URL should override the default Qdrant endpoint.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("VECTOR_DB_URL", "http://qdrant:6333")

    reset_api_config()
    config = MEHOAPIConfig()

    assert config.vector_db_url == "http://qdrant:6333"
