# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for observability API routes.

TASK-186 Phase 2: API Endpoints

Tests for /api/observability endpoints for session transcripts,
event details, and cross-session search.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_app.api.auth import get_current_user
from meho_app.api.observability import router
from meho_app.core.auth_context import UserContext
from meho_app.database import get_db_session

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def app():
    """Create a test FastAPI application with observability routes."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def mock_session():
    """Create a mock async database session."""
    session = AsyncMock()
    return session


@pytest.fixture
def regular_user():
    """Create a regular user context."""
    return UserContext(
        user_id="user-123",
        tenant_id="tenant-a",
        roles=["user"],
        groups=[],
    )


@pytest.fixture
def other_tenant_user():
    """Create a user from a different tenant."""
    return UserContext(
        user_id="user-456",
        tenant_id="tenant-b",
        roles=["user"],
        groups=[],
    )


@pytest.fixture
def sample_session_id():
    """Create a sample session UUID."""
    return uuid4()


@pytest.fixture
def sample_transcript_id():
    """Create a sample transcript UUID."""
    return uuid4()


@pytest.fixture
def sample_event_id():
    """Create a sample event UUID."""
    return uuid4()


def create_mock_transcript(session_id, tenant_id="tenant-a"):
    """Create a mock transcript model."""
    transcript = MagicMock()
    transcript.id = uuid4()
    transcript.session_id = session_id
    transcript.status = "completed"
    transcript.created_at = datetime.now(tz=UTC) - timedelta(hours=1)
    transcript.completed_at = datetime.now(tz=UTC)
    transcript.total_llm_calls = 3
    transcript.total_operation_calls = 2
    transcript.total_sql_queries = 1
    transcript.total_tool_calls = 4
    transcript.total_tokens = 2500
    transcript.total_cost_usd = 0.0025
    transcript.total_duration_ms = 5000.0
    transcript.user_query = "List all VMs"
    transcript.agent_type = "react"
    return transcript


def create_mock_event(session_id, event_type="thought"):
    """Create a mock event model."""
    event = MagicMock()
    event.id = uuid4()
    event.session_id = session_id
    event.timestamp = datetime.now(tz=UTC)
    event.type = event_type
    event.summary = f"Test {event_type} event"
    event.details = {
        "llm_prompt": "You are MEHO..." if event_type == "thought" else None,
        "llm_response": "I will analyze..." if event_type == "thought" else None,
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "estimated_cost_usd": 0.0001,
        }
        if event_type == "thought"
        else None,
    }
    event.parent_event_id = None
    event.step_number = 1
    event.node_name = "reason"
    event.agent_name = "react_agent"
    event.duration_ms = 1200.0
    return event


def create_mock_chat_session(session_id, tenant_id="tenant-a"):
    """Create a mock chat session model."""
    session = MagicMock()
    session.id = session_id
    session.tenant_id = tenant_id
    session.created_at = datetime.now(tz=UTC)
    return session


def create_test_client(app, user, session):
    """Create a test client with mocked dependencies."""

    async def mock_get_db_session():
        yield session

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db_session] = mock_get_db_session
    return TestClient(app)


# =============================================================================
# List Sessions Tests
# =============================================================================


class TestListSessions:
    """Tests for GET /api/observability/sessions."""

    def test_list_sessions_empty(self, app, regular_user, mock_session):
        """Should return empty list when no sessions exist."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        # Mock count query
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        mock_session.execute.side_effect = [count_result, mock_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/sessions")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["sessions"] == []
        assert data["offset"] == 0
        assert data["limit"] == 20

    def test_list_sessions_with_pagination(self, app, regular_user, mock_session):
        """Should support pagination parameters."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        count_result = MagicMock()
        count_result.scalar.return_value = 0
        mock_session.execute.side_effect = [count_result, mock_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/sessions?limit=10&offset=5")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["offset"] == 5
        assert data["limit"] == 10

    def test_list_sessions_with_status_filter(self, app, regular_user, mock_session):
        """Should filter by status."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        count_result = MagicMock()
        count_result.scalar.return_value = 0
        mock_session.execute.side_effect = [count_result, mock_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/sessions?status=completed")

        # Assert
        assert response.status_code == 200


# =============================================================================
# Get Transcript Tests
# =============================================================================


class TestGetTranscript:
    """Tests for GET /api/observability/sessions/{session_id}/transcript."""

    def test_get_transcript_success(self, app, regular_user, mock_session, sample_session_id):
        """Should return full transcript with events."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_transcript = create_mock_transcript(sample_session_id)
        mock_events = [
            create_mock_event(sample_session_id, "thought"),
            create_mock_event(sample_session_id, "action"),
        ]

        # Mock session lookup
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        # Mock transcript lookup
        transcript_result = MagicMock()
        transcript_result.scalar_one_or_none.return_value = mock_transcript

        # Mock events lookup
        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = mock_events

        mock_session.execute.side_effect = [
            session_result,  # _verify_session_access
            transcript_result,  # get_transcript
            events_result,  # get_events
        ]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/transcript")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == str(sample_session_id)
        assert "summary" in data
        assert "events" in data
        assert data["summary"]["total_llm_calls"] == 3
        assert len(data["events"]) == 2

    def test_get_transcript_not_found(self, app, regular_user, mock_session, sample_session_id):
        """Should return 404 when session not found."""
        # Arrange
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = None

        mock_session.execute.return_value = session_result

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/transcript")

        # Assert
        assert response.status_code == 404

    def test_get_transcript_access_denied(
        self, app, other_tenant_user, mock_session, sample_session_id
    ):
        """Should return 403 when accessing another tenant's session."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        mock_session.execute.return_value = session_result

        client = create_test_client(app, other_tenant_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/transcript")

        # Assert
        assert response.status_code == 403
        assert "denied" in response.json()["detail"].lower()

    def test_get_transcript_invalid_session_id(self, app, regular_user, mock_session):
        """Should return 400 for invalid session ID format."""
        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/sessions/invalid-uuid/transcript")

        # Assert
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    def test_get_transcript_latest(self, app, regular_user, mock_session):
        """Should resolve 'latest' to most recent session."""
        # Arrange
        latest_session_id = uuid4()
        mock_chat_session = create_mock_chat_session(latest_session_id, "tenant-a")
        mock_transcript = create_mock_transcript(latest_session_id)

        # Mock latest session lookup
        latest_result = MagicMock()
        latest_result.scalar_one_or_none.return_value = mock_chat_session

        # Mock session verify lookup
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        # Mock transcript lookup
        transcript_result = MagicMock()
        transcript_result.scalar_one_or_none.return_value = mock_transcript

        # Mock events lookup
        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            latest_result,  # _resolve_session_id (latest)
            session_result,  # _verify_session_access
            transcript_result,  # get_transcript
            events_result,  # get_events
        ]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/sessions/latest/transcript")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == str(latest_session_id)


# =============================================================================
# Get Summary Tests
# =============================================================================


class TestGetSummary:
    """Tests for GET /api/observability/sessions/{session_id}/summary."""

    def test_get_summary_success(self, app, regular_user, mock_session, sample_session_id):
        """Should return session summary only."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_transcript = create_mock_transcript(sample_session_id)

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        transcript_result = MagicMock()
        transcript_result.scalar_one_or_none.return_value = mock_transcript

        mock_session.execute.side_effect = [session_result, transcript_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/summary")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == str(sample_session_id)
        assert data["total_llm_calls"] == 3
        assert data["total_tokens"] == 2500
        assert data["status"] == "completed"


# =============================================================================
# Get Event Details Tests
# =============================================================================


class TestGetEventDetails:
    """Tests for GET /api/observability/sessions/{session_id}/events/{event_id}."""

    def test_get_event_success(
        self, app, regular_user, mock_session, sample_session_id, sample_event_id
    ):
        """Should return full event details."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_event = create_mock_event(sample_session_id, "thought")
        mock_event.id = sample_event_id
        mock_event.session_id = sample_session_id

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        event_result = MagicMock()
        event_result.scalar_one_or_none.return_value = mock_event

        mock_session.execute.side_effect = [session_result, event_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(
            f"/api/observability/sessions/{sample_session_id}/events/{sample_event_id}"
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(sample_event_id)
        assert data["type"] == "thought"

    def test_get_event_not_found(
        self, app, regular_user, mock_session, sample_session_id, sample_event_id
    ):
        """Should return 404 when event not found."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        event_result = MagicMock()
        event_result.scalar_one_or_none.return_value = None

        mock_session.execute.side_effect = [session_result, event_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(
            f"/api/observability/sessions/{sample_session_id}/events/{sample_event_id}"
        )

        # Assert
        assert response.status_code == 404


# =============================================================================
# Get LLM Calls Tests
# =============================================================================


class TestGetLLMCalls:
    """Tests for GET /api/observability/sessions/{session_id}/llm-calls."""

    def test_get_llm_calls_success(self, app, regular_user, mock_session, sample_session_id):
        """Should return all LLM call events."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_events = [
            create_mock_event(sample_session_id, "thought"),
            create_mock_event(sample_session_id, "thought"),
        ]

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = mock_events

        mock_session.execute.side_effect = [session_result, events_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/llm-calls")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert all(e["type"] == "thought" for e in data)


# =============================================================================
# Get HTTP Calls Tests
# =============================================================================


class TestGetOperationCalls:
    """Tests for GET /api/observability/sessions/{session_id}/operation-calls."""

    def test_get_operation_calls_success(self, app, regular_user, mock_session, sample_session_id):
        """Should return all operation call events."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_event = create_mock_event(sample_session_id, "operation_call")
        mock_event.details = {
            "http_method": "GET",
            "http_url": "https://api.example.com/vms",
            "http_status_code": 200,
        }

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = [mock_event]

        mock_session.execute.side_effect = [session_result, events_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/operation-calls")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1


# =============================================================================
# Get SQL Queries Tests
# =============================================================================


class TestGetSQLQueries:
    """Tests for GET /api/observability/sessions/{session_id}/sql-queries."""

    def test_get_sql_queries_success(self, app, regular_user, mock_session, sample_session_id):
        """Should return all SQL query events."""
        # Arrange
        mock_chat_session = create_mock_chat_session(sample_session_id, "tenant-a")
        mock_event = create_mock_event(sample_session_id, "sql_query")
        mock_event.details = {
            "sql_query": "SELECT * FROM vms WHERE cluster = $1",
            "sql_parameters": {"$1": "production"},
            "sql_row_count": 10,
        }

        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = mock_chat_session

        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = [mock_event]

        mock_session.execute.side_effect = [session_result, events_result]

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get(f"/api/observability/sessions/{sample_session_id}/sql-queries")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1


# =============================================================================
# Search Events Tests
# =============================================================================


class TestSearchEvents:
    """Tests for GET /api/observability/search."""

    def test_search_events_success(self, app, regular_user, mock_session):
        """Should search events across sessions."""
        # Arrange
        session_id = uuid4()
        mock_event = create_mock_event(session_id, "thought")
        mock_event.summary = "Analyzing VM inventory request"

        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = [mock_event]

        mock_session.execute.return_value = events_result

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/search?query=VM+inventory")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "VM inventory"
        assert len(data["results"]) == 1

    def test_search_events_with_type_filter(self, app, regular_user, mock_session):
        """Should filter search by event type."""
        # Arrange
        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = []

        mock_session.execute.return_value = events_result

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/search?query=test&event_type=operation_call")

        # Assert
        assert response.status_code == 200

    def test_search_events_with_time_filter(self, app, regular_user, mock_session):
        """Should filter search by time range."""
        # Arrange
        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = []

        mock_session.execute.return_value = events_result

        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/observability/search?query=test&since_minutes=120")

        # Assert
        assert response.status_code == 200
