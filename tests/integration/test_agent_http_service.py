# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for Agent HTTP Service.

Tests the agent service HTTP endpoints directly (without BFF layer).
Note: Workflow endpoints have been removed - now only testing chat sessions.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_db_session():
    """Mock database session"""
    session = AsyncMock()
    return session


@pytest.fixture
def client(monkeypatch):
    """Test client for agent service"""
    # Set fake API key for dependency injection
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    # Import here to avoid import issues
    from fastapi import FastAPI

    from meho_app.modules.agents.routes import router

    app = FastAPI()
    app.include_router(router)

    return TestClient(app)


@pytest.mark.integration
def test_health_endpoint(client):
    """Test health check endpoint"""
    response = client.get("/agent/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "meho-agent"
