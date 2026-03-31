# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for Phase 5 observability features.

Tests export endpoints, rate limiting, and retention endpoints
using mocked database and Redis connections.

Part of TASK-186: Deep Observability & Introspection System.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    user = MagicMock()
    user.user_id = str(uuid4())
    user.tenant_id = uuid4()
    user.email = "test@example.com"
    return user


@pytest.fixture
def mock_transcript():
    """Create a mock transcript model."""
    transcript = MagicMock()
    transcript.id = uuid4()
    transcript.session_id = uuid4()
    transcript.created_at = datetime.now(tz=UTC)
    transcript.completed_at = datetime.now(tz=UTC)
    transcript.status = "completed"
    transcript.user_query = "List all VMs"
    transcript.agent_type = "react"
    transcript.total_llm_calls = 3
    transcript.total_operation_calls = 2
    transcript.total_sql_queries = 0
    transcript.total_tool_calls = 2
    transcript.total_tokens = 500
    transcript.total_cost_usd = 0.001
    transcript.total_duration_ms = 2500.0
    transcript.deleted_at = None
    return transcript


@pytest.fixture
def mock_event():
    """Create a mock event model."""
    event = MagicMock()
    event.id = uuid4()
    event.session_id = uuid4()
    event.timestamp = datetime.now(tz=UTC)
    event.type = "thought"
    event.summary = "Planning next action"
    event.duration_ms = 150.0
    event.step_number = 1
    event.node_name = "reason"
    event.agent_name = "react"
    event.parent_event_id = None
    event.details = {
        "model": "gpt-4.1-mini",
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }
    return event


class TestExportEndpoints:
    """Tests for export functionality."""

    @pytest.fixture
    def app_with_mocks(self, mock_user, mock_transcript, mock_event):
        """Create app with mocked dependencies."""
        with (
            patch("meho_app.api.observability.router_export.get_limiter") as mock_limiter,
            patch("meho_app.core.rate_limiting.create_limiter") as mock_create,
            patch("meho_app.core.rate_limiting.get_config") as mock_config,
        ):
            # Create a no-op limiter
            limiter = MagicMock()
            limiter.limit = lambda *args, **kwargs: lambda f: f
            mock_limiter.return_value = limiter
            mock_create.return_value = limiter

            # Mock config
            config = MagicMock()
            config.redis_url = "redis://localhost:6379"
            config.rate_limit_transcript = "60/minute"
            config.rate_limit_export = "5/minute"
            mock_config.return_value = config

            from meho_app.api.observability import router

            app = FastAPI()
            app.include_router(router, prefix="/api")

            return app

    def test_export_format_enum_values(self):
        """Test ExportFormat enum has expected values."""
        from meho_app.api.observability.schemas import ExportFormat

        assert ExportFormat.JSON.value == "json"
        assert ExportFormat.CSV.value == "csv"


class TestRetentionEndpoints:
    """Tests for retention management endpoints."""

    def test_retention_stats_response_model(self):
        """Test RetentionStatsResponse model structure."""
        from meho_app.api.observability.schemas import RetentionStatsResponse

        response = RetentionStatsResponse(
            total_transcripts=100,
            active_transcripts=80,
            soft_deleted_transcripts=15,
            pending_hard_delete=5,
            oldest_active_timestamp=datetime.now(tz=UTC),
            oldest_soft_deleted_timestamp=None,
            retention_days=30,
            grace_days=7,
        )

        assert response.total_transcripts == 100
        assert response.retention_days == 30

    def test_cleanup_result_response_model(self):
        """Test CleanupResultResponse model structure."""
        from meho_app.api.observability.schemas import CleanupResultResponse

        response = CleanupResultResponse(
            soft_deleted_count=10,
            hard_deleted_count=5,
            errors=[],
            message="Cleanup completed successfully",
        )

        assert response.soft_deleted_count == 10
        assert response.message == "Cleanup completed successfully"


class TestBulkExportRequest:
    """Tests for BulkExportRequest model."""

    def test_bulk_export_request_defaults(self):
        """Test BulkExportRequest has correct defaults."""
        from meho_app.api.observability.schemas import BulkExportRequest

        request = BulkExportRequest()

        assert request.session_ids is None
        assert request.since is None
        assert request.until is None
        assert request.event_types is None
        assert request.include_details is True
        assert request.max_sessions == 10

    def test_bulk_export_request_with_filters(self):
        """Test BulkExportRequest with filters."""
        from meho_app.api.observability.schemas import BulkExportRequest

        since = datetime.now(tz=UTC) - timedelta(days=7)
        until = datetime.now(tz=UTC)

        request = BulkExportRequest(
            session_ids=["abc-123"],
            since=since,
            until=until,
            event_types=["thought", "action"],
            include_details=False,
            max_sessions=50,
        )

        assert request.session_ids == ["abc-123"]
        assert request.since == since
        assert request.event_types == ["thought", "action"]
        assert request.include_details is False
        assert request.max_sessions == 50


class TestRateLimitingSetup:
    """Tests for rate limiting configuration."""

    def test_rate_limit_config_values(self):
        """Test rate limit config values exist."""
        # Just verify the config fields exist by importing
        with patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379",
                "OPENAI_API_KEY": "test-key",
                "OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "OBJECT_STORAGE_BUCKET": "test",
                "OBJECT_STORAGE_ACCESS_KEY": "test",
                "OBJECT_STORAGE_SECRET_KEY": "test",
                "CREDENTIAL_ENCRYPTION_KEY": "a" * 44,
            },
        ):
            from meho_app.core.config import Config

            config = Config()

            assert hasattr(config, "rate_limit_transcript")
            assert hasattr(config, "rate_limit_search")
            assert hasattr(config, "rate_limit_export")
            assert hasattr(config, "rate_limit_cleanup")

    def test_retention_config_values(self):
        """Test retention config values exist."""
        with patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379",
                "OPENAI_API_KEY": "test-key",
                "OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "OBJECT_STORAGE_BUCKET": "test",
                "OBJECT_STORAGE_ACCESS_KEY": "test",
                "OBJECT_STORAGE_SECRET_KEY": "test",
                "CREDENTIAL_ENCRYPTION_KEY": "a" * 44,
            },
        ):
            from meho_app.core.config import Config

            config = Config()

            assert hasattr(config, "transcript_retention_days")
            assert hasattr(config, "transcript_grace_days")
            assert config.transcript_retention_days == 30
            assert config.transcript_grace_days == 7


class TestRateLimitKeyExtraction:
    """Tests for rate limit key extraction."""

    def test_get_rate_limit_key_with_user(self):
        """Test key extraction with authenticated user."""
        from meho_app.core.rate_limiting import get_rate_limit_key

        request = MagicMock()
        user = MagicMock()
        user.user_id = "user-123"
        request.state.user = user

        key = get_rate_limit_key(request)

        assert key == "user:user-123"

    def test_get_rate_limit_key_without_user(self):
        """Test key extraction falls back to IP."""
        from meho_app.core.rate_limiting import get_rate_limit_key

        request = MagicMock()
        request.state = MagicMock(spec=[])  # No user attribute
        request.client.host = "192.168.1.1"

        # Need to mock get_remote_address
        with patch("meho_app.core.rate_limiting.get_remote_address") as mock_ip:
            mock_ip.return_value = "192.168.1.1"
            key = get_rate_limit_key(request)

        assert key == "ip:192.168.1.1"

    def test_get_rate_limit_key_user_no_id(self):
        """Test key extraction when user has no user_id."""
        from meho_app.core.rate_limiting import get_rate_limit_key

        request = MagicMock()
        user = MagicMock(spec=[])  # No user_id attribute
        request.state.user = user

        with patch("meho_app.core.rate_limiting.get_remote_address") as mock_ip:
            mock_ip.return_value = "10.0.0.1"
            key = get_rate_limit_key(request)

        assert key == "ip:10.0.0.1"


class TestPaginationParameters:
    """Tests for pagination parameter support."""

    def test_llm_calls_endpoint_has_offset(self):
        """Verify get_llm_calls accepts offset parameter."""
        import inspect

        from meho_app.api.observability.router_events import get_llm_calls

        sig = inspect.signature(get_llm_calls)
        params = list(sig.parameters.keys())

        assert "offset" in params
        assert "limit" in params

    def test_operation_calls_endpoint_has_offset(self):
        """Verify get_operation_calls accepts offset parameter."""
        import inspect

        from meho_app.api.observability.router_events import get_operation_calls

        sig = inspect.signature(get_operation_calls)
        params = list(sig.parameters.keys())

        assert "offset" in params
        assert "limit" in params

    def test_sql_queries_endpoint_has_offset(self):
        """Verify get_sql_queries accepts offset parameter."""
        import inspect

        from meho_app.api.observability.router_events import get_sql_queries

        sig = inspect.signature(get_sql_queries)
        params = list(sig.parameters.keys())

        assert "offset" in params
        assert "limit" in params
