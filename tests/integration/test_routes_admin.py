# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for Admin API routes.

TASK-77: Externalize Prompts & Models
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


# Mock auth for tests
@pytest.fixture
def mock_auth():
    """Mock authentication for admin routes."""
    from meho_app.core.auth_context import UserContext

    user = UserContext(
        user_id="test-user",
        tenant_id="test-tenant",
        roles=["admin"],
        session_id="test-session",
    )

    with patch("meho_api.auth.get_current_user", return_value=user):
        yield user


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()

    with patch("meho_api.routes_admin.get_agent_session", return_value=session):
        yield session


@pytest.fixture
def mock_repository():
    """Mock TenantConfigRepository."""

    repo = AsyncMock()

    # Default: no config exists
    repo.get_config = AsyncMock(return_value=None)
    repo.get_installation_context = AsyncMock(return_value=None)
    repo.get_audit_log = AsyncMock(return_value=[])

    with patch("meho_api.routes_admin.get_repository", return_value=repo):
        yield repo


class TestGetConfig:
    """Tests for GET /api/admin/config."""

    @pytest.mark.asyncio
    async def test_get_config_empty(self, mock_auth, mock_db_session, mock_repository):
        """Test getting config when none exists."""
        from meho_app.api.service import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/admin/config", headers={"Authorization": "Bearer test-token"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "test-tenant"
        assert data.get("installation_context") is None

    @pytest.mark.asyncio
    async def test_get_config_with_data(self, mock_auth, mock_db_session, mock_repository):
        """Test getting config with existing data."""
        from meho_app.api.service import app

        from meho_app.modules.agents.models import TenantAgentConfig

        # Mock existing config
        existing_config = MagicMock(spec=TenantAgentConfig)
        existing_config.tenant_id = "test-tenant"
        existing_config.installation_context = "Test context for Acme Corp"
        existing_config.model_override = "openai:gpt-4.1"
        existing_config.temperature_override = {"value": 0.5}
        existing_config.features = {"feature_x": True}
        existing_config.updated_by = "admin"
        existing_config.updated_at = datetime.now(tz=UTC)
        existing_config.created_at = datetime.now(tz=UTC)

        mock_repository.get_config = AsyncMock(return_value=existing_config)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/admin/config", headers={"Authorization": "Bearer test-token"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["installation_context"] == "Test context for Acme Corp"
        assert data["model_override"] == "openai:gpt-4.1"
        assert data["temperature_override"] == 0.5


class TestUpdateConfig:
    """Tests for PUT /api/admin/config."""

    @pytest.mark.asyncio
    async def test_update_config_success(self, mock_auth, mock_db_session, mock_repository):
        """Test updating config successfully."""
        from meho_app.api.service import app

        from meho_app.modules.agents.models import TenantAgentConfig

        # Mock create_or_update result
        new_config = MagicMock(spec=TenantAgentConfig)
        new_config.tenant_id = "test-tenant"
        new_config.installation_context = "New context"
        new_config.model_override = None
        new_config.temperature_override = None
        new_config.features = {}
        new_config.updated_by = "test-user"
        new_config.updated_at = datetime.now(tz=UTC)
        new_config.created_at = datetime.now(tz=UTC)

        mock_repository.create_or_update = AsyncMock(return_value=new_config)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/admin/config",
                headers={"Authorization": "Bearer test-token"},
                json={"installation_context": "New context"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["installation_context"] == "New context"

    @pytest.mark.asyncio
    async def test_update_config_invalid_model(self, mock_auth, mock_db_session, mock_repository):
        """Test updating config with invalid model."""
        from meho_app.api.service import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/admin/config",
                headers={"Authorization": "Bearer test-token"},
                json={"model_override": "invalid:model"},
            )

        assert response.status_code == 400
        assert "Invalid model" in response.json()["detail"]


class TestGetAllowedModels:
    """Tests for GET /api/admin/models."""

    @pytest.mark.asyncio
    async def test_get_allowed_models(self, mock_auth):
        """Test getting list of allowed models."""
        from meho_app.api.service import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/admin/models", headers={"Authorization": "Bearer test-token"}
            )

        assert response.status_code == 200
        data = response.json()
        assert "allowed_models" in data
        assert len(data["allowed_models"]) > 0

        # Check structure
        model = data["allowed_models"][0]
        assert "id" in model
        assert "name" in model
        assert "provider" in model
        assert "recommended" in model


class TestPromptPreview:
    """Tests for GET /api/admin/prompt/preview."""

    @pytest.mark.asyncio
    async def test_prompt_preview(self, mock_auth, mock_db_session, mock_repository):
        """Test getting prompt preview."""
        from meho_app.api.service import app

        # Mock AgentConfig.load
        mock_config = MagicMock()
        mock_config.model.name = "openai:gpt-4.1-mini"
        mock_config.model.temperature = 0.7
        mock_config.tenant_context = None
        mock_config.prompt_sources.base = "config/prompts/base_system_prompt.md"
        mock_config.prompt_sources.tools = None
        mock_config.prompt_sources.safety = None
        mock_config.runtime_prompt = None

        with (
            patch(
                "meho_api.routes_admin.AgentConfig.load", new=AsyncMock(return_value=mock_config)
            ),
            patch("meho_api.routes_admin.PromptBuilder") as MockBuilder,
        ):
            mock_builder = MagicMock()
            mock_builder.build = AsyncMock(return_value="Test system prompt content")
            MockBuilder.return_value = mock_builder

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/admin/prompt/preview", headers={"Authorization": "Bearer test-token"}
                )

        assert response.status_code == 200
        data = response.json()
        assert "system_prompt" in data
        assert "character_count" in data
        assert "model" in data


class TestAuditLog:
    """Tests for GET /api/admin/config/audit."""

    @pytest.mark.asyncio
    async def test_get_audit_log_empty(self, mock_auth, mock_db_session, mock_repository):
        """Test getting empty audit log."""
        from meho_app.api.service import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/admin/config/audit", headers={"Authorization": "Bearer test-token"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "test-tenant"
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_get_audit_log_with_entries(self, mock_auth, mock_db_session, mock_repository):
        """Test getting audit log with entries."""
        from meho_app.api.service import app

        from meho_app.modules.agents.models import TenantAgentConfigAudit

        # Mock audit entries
        entry = MagicMock(spec=TenantAgentConfigAudit)
        entry.field_changed = "installation_context"
        entry.old_value = "Old value"
        entry.new_value = "New value"
        entry.changed_by = "admin"
        entry.changed_at = datetime.now(tz=UTC)

        mock_repository.get_audit_log = AsyncMock(return_value=[entry])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/admin/config/audit", headers={"Authorization": "Bearer test-token"}
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["field_changed"] == "installation_context"
