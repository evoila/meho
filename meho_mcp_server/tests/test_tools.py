# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for MCP tool handlers.

Part of TASK-186: Deep Observability & Introspection System.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# Import tools
from meho_mcp_server.tools.sessions import list_sessions, get_transcript, get_summary
from meho_mcp_server.tools.llm import get_llm_calls
from meho_mcp_server.tools.sql import get_sql_queries
from meho_mcp_server.tools.http import get_operation_calls
from meho_mcp_server.tools.events import get_event_details, search_events
from meho_mcp_server.tools.explain import explain_session


@pytest.fixture
def mock_response():
    """Create mock HTTP response."""
    response = MagicMock()
    response.json.return_value = {"sessions": []}
    response.raise_for_status = MagicMock()
    return response


class TestListSessions:
    """Tests for list_sessions tool."""

    @pytest.mark.asyncio
    async def test_list_sessions_default_params(self, mock_response):
        """Should call API with default parameters."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await list_sessions()

            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert call_args[1]["params"]["limit"] == 10

    @pytest.mark.asyncio
    async def test_list_sessions_with_status_filter(self, mock_response):
        """Should pass status filter to API."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await list_sessions(status="completed", limit=5)

            call_args = mock_client.get.call_args
            assert call_args[1]["params"]["status"] == "completed"
            assert call_args[1]["params"]["limit"] == 5

    @pytest.mark.asyncio
    async def test_list_sessions_returns_json(self, mock_response):
        """Should return JSON string."""
        mock_response.json.return_value = {
            "sessions": [{"session_id": "abc-123"}],
            "total": 1,
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await list_sessions()

            data = json.loads(result)
            assert data["sessions"][0]["session_id"] == "abc-123"


class TestGetTranscript:
    """Tests for get_transcript tool."""

    @pytest.mark.asyncio
    async def test_get_transcript_basic(self, mock_response):
        """Should call transcript API."""
        mock_response.json.return_value = {
            "session_id": "abc-123",
            "events": [],
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_transcript(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "abc-123/transcript" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_get_transcript_compact_mode(self, mock_response):
        """Should return compact format when requested."""
        mock_response.json.return_value = {
            "session_id": "abc-123",
            "summary": {"total_llm_calls": 2},
            "events": [
                {"id": "e1", "type": "thought", "summary": "test", "timestamp": "2026-01-01"},
            ],
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await get_transcript(session_id="abc-123", compact=True)

            data = json.loads(result)
            # Compact mode should strip details
            assert "details" not in data["events"][0]


class TestGetSummary:
    """Tests for get_summary tool."""

    @pytest.mark.asyncio
    async def test_get_summary(self, mock_response):
        """Should call summary API."""
        mock_response.json.return_value = {
            "session_id": "abc-123",
            "total_tokens": 500,
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_summary(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "abc-123/summary" in call_args[0][0]


class TestGetLLMCalls:
    """Tests for get_llm_calls tool."""

    @pytest.mark.asyncio
    async def test_get_llm_calls(self, mock_response):
        """Should call LLM calls API."""
        mock_response.json.return_value = [
            {"id": "e1", "type": "llm_call"},
        ]

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_llm_calls(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "llm-calls" in call_args[0][0]


class TestGetSQLQueries:
    """Tests for get_sql_queries tool."""

    @pytest.mark.asyncio
    async def test_get_sql_queries(self, mock_response):
        """Should call SQL queries API."""
        mock_response.json.return_value = [
            {"id": "e1", "type": "sql_query"},
        ]

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_sql_queries(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "sql-queries" in call_args[0][0]


class TestGetOperationCalls:
    """Tests for get_operation_calls tool."""

    @pytest.mark.asyncio
    async def test_get_operation_calls(self, mock_response):
        """Should call operation calls API."""
        mock_response.json.return_value = [
            {"id": "e1", "type": "operation_call"},
        ]

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_operation_calls(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "operation-calls" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_get_operation_calls_with_filter(self, mock_response):
        """Should pass status filter."""
        mock_response.json.return_value = []

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_operation_calls(session_id="abc-123", status_filter="error")

            call_args = mock_client.get.call_args
            assert call_args[1]["params"]["status_filter"] == "error"


class TestGetEventDetails:
    """Tests for get_event_details tool."""

    @pytest.mark.asyncio
    async def test_get_event_details(self, mock_response):
        """Should call event details API."""
        mock_response.json.return_value = {
            "id": "event-123",
            "type": "thought",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await get_event_details(
                event_id="event-123",
                session_id="session-123",
            )

            call_args = mock_client.get.call_args
            assert "event-123" in call_args[0][0]


class TestSearchEvents:
    """Tests for search_events tool."""

    @pytest.mark.asyncio
    async def test_search_events(self, mock_response):
        """Should call search API."""
        mock_response.json.return_value = {
            "query": "error",
            "results": [],
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await search_events(query="error")

            call_args = mock_client.get.call_args
            assert call_args[1]["params"]["query"] == "error"


class TestExplainSession:
    """Tests for explain_session tool."""

    @pytest.mark.asyncio
    async def test_explain_session(self, mock_response):
        """Should call explain API."""
        mock_response.json.return_value = {
            "session_id": "abc-123",
            "focus": "overview",
            "explanation": "Session overview...",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await explain_session(session_id="abc-123")

            call_args = mock_client.get.call_args
            assert "explain" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_explain_session_with_focus(self, mock_response):
        """Should pass focus parameter."""
        mock_response.json.return_value = {
            "focus": "errors",
            "explanation": "Error analysis...",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await explain_session(session_id="abc-123", focus="errors")

            call_args = mock_client.get.call_args
            assert call_args[1]["params"]["focus"] == "errors"
