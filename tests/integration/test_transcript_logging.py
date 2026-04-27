# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for transcript event logging.

TASK-187: Complete Transcript Event Logging

Tests the end-to-end flow of event capture:
- HTTP calls via GenericHTTPClient
- Context variable propagation
- Event persistence to TranscriptCollector
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.agents.persistence.event_context import (
    set_transcript_collector,
)
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_collector():
    """Create a mock TranscriptCollector."""
    collector = MagicMock()
    collector.transcript_id = uuid4()
    collector.session_id = uuid4()
    collector.add = AsyncMock()

    # create_operation_event returns an event object
    mock_event = MagicMock()
    mock_event.event_type = "operation_call"
    collector.create_operation_event = MagicMock(return_value=mock_event)

    return collector


@pytest.fixture
def mock_connector():
    """Create a mock Connector object."""
    connector = MagicMock()
    connector.id = uuid4()
    connector.name = "Test Connector"
    connector.base_url = "https://api.example.com"
    connector.auth_type = "API_KEY"
    connector.auth_config = {"api_key": "secret-key", "header_name": "X-API-Key"}
    return connector


@pytest.fixture
def mock_endpoint():
    """Create a mock EndpointDescriptor object."""
    endpoint = MagicMock()
    endpoint.method = "GET"
    endpoint.path = "/api/v1/resources"
    return endpoint


# =============================================================================
# HTTP Client Event Emission Tests
# =============================================================================


class TestHTTPClientEventEmission:
    """Tests for GenericHTTPClient emitting events via context."""

    @pytest.fixture(autouse=True)
    def cleanup_context(self):
        """Ensure context is clean before and after each test."""
        set_transcript_collector(None)
        yield
        set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_http_call_emits_event_when_collector_in_context(
        self, mock_collector, mock_connector, mock_endpoint
    ):
        """HTTP calls emit events when collector is in context."""
        client = GenericHTTPClient(timeout=10.0)

        # Set collector in context
        set_transcript_collector(mock_collector)

        # Mock the HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "test"}

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            # Make the call
            _status, _data = await client.call_endpoint(
                connector=mock_connector,
                endpoint=mock_endpoint,
            )

        # Verify event was created and added
        mock_collector.create_operation_event.assert_called_once()
        mock_collector.add.assert_called_once()

        # Verify event details
        call_args = mock_collector.create_operation_event.call_args
        assert call_args.kwargs["method"] == "GET"
        assert "api.example.com" in call_args.kwargs["url"]
        assert call_args.kwargs["status_code"] == 200
        assert call_args.kwargs["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_http_call_skips_event_when_no_collector(self, mock_connector, mock_endpoint):
        """HTTP calls don't error when no collector in context."""
        client = GenericHTTPClient(timeout=10.0)

        # Ensure no collector in context
        set_transcript_collector(None)

        # Mock the HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "test"}

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            # Make the call - should not raise
            status, data = await client.call_endpoint(
                connector=mock_connector,
                endpoint=mock_endpoint,
            )

        assert status == 200
        assert data == {"data": "test"}

    @pytest.mark.asyncio
    async def test_headers_are_sanitized_in_event(
        self, mock_collector, mock_connector, mock_endpoint
    ):
        """Sensitive headers are redacted in the event."""
        client = GenericHTTPClient(timeout=10.0)
        set_transcript_collector(mock_collector)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            await client.call_endpoint(
                connector=mock_connector,
                endpoint=mock_endpoint,
            )

        # Get the headers that were passed to create_operation_event
        call_args = mock_collector.create_operation_event.call_args
        headers = call_args.kwargs["headers"]

        # API key should be redacted
        if "X-API-Key" in headers:
            assert headers["X-API-Key"] == "***"

    @pytest.mark.asyncio
    async def test_large_payload_is_truncated(self, mock_collector, mock_connector, mock_endpoint):
        """Large response bodies are truncated."""
        client = GenericHTTPClient(timeout=10.0)
        set_transcript_collector(mock_collector)

        # Create a large response
        large_data = {"items": ["x" * 100 for _ in range(100)]}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = large_data

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            await client.call_endpoint(
                connector=mock_connector,
                endpoint=mock_endpoint,
            )

        # Get the response body that was logged
        call_args = mock_collector.create_operation_event.call_args
        response_body = call_args.kwargs["response_body"]

        # Should be truncated
        assert len(response_body) <= 2020  # 2000 + "... [truncated]"
        if len(json.dumps(large_data)) > 2000:
            assert "[truncated]" in response_body


# =============================================================================
# Header Sanitization Tests
# =============================================================================


class TestHeaderSanitization:
    """Tests for the header sanitization utility."""

    def test_authorization_header_redacted(self):
        """Authorization header values are replaced with ***."""
        client = GenericHTTPClient()
        headers = {
            "Authorization": "Bearer secret-token",
            "Content-Type": "application/json",
        }

        sanitized = client._sanitize_headers(headers)

        assert sanitized["Authorization"] == "***"
        assert sanitized["Content-Type"] == "application/json"

    def test_api_key_header_redacted(self):
        """X-API-Key header values are replaced with ***."""
        client = GenericHTTPClient()
        headers = {
            "X-API-Key": "my-secret-key",
            "Accept": "application/json",
        }

        sanitized = client._sanitize_headers(headers)

        assert sanitized["X-API-Key"] == "***"
        assert sanitized["Accept"] == "application/json"

    def test_cookie_header_redacted(self):
        """Cookie header values are replaced with ***."""
        client = GenericHTTPClient()
        headers = {
            "Cookie": "session=abc123",
            "Content-Length": "100",
        }

        sanitized = client._sanitize_headers(headers)

        assert sanitized["Cookie"] == "***"
        assert sanitized["Content-Length"] == "100"

    def test_case_insensitive_redaction(self):
        """Header redaction is case-insensitive."""
        client = GenericHTTPClient()
        headers = {
            "AUTHORIZATION": "secret",
            "x-api-key": "secret",
            "Cookie": "secret",
        }

        sanitized = client._sanitize_headers(headers)

        assert sanitized["AUTHORIZATION"] == "***"
        assert sanitized["x-api-key"] == "***"
        assert sanitized["Cookie"] == "***"


# =============================================================================
# Payload Truncation Tests
# =============================================================================


class TestPayloadTruncation:
    """Tests for the payload truncation utility."""

    def test_small_payload_unchanged(self):
        """Small payloads are not truncated."""
        client = GenericHTTPClient()
        data = {"name": "test", "value": 123}

        result = client._truncate_payload(data)

        assert json.loads(result) == data
        assert "[truncated]" not in result

    def test_large_payload_truncated(self):
        """Large payloads are truncated with marker."""
        client = GenericHTTPClient()
        # Create a payload larger than 2000 characters
        data = {"items": ["x" * 100 for _ in range(50)]}

        result = client._truncate_payload(data)

        assert len(result) <= 2020
        assert "[truncated]" in result

    def test_none_returns_empty_string(self):
        """None input returns empty string."""
        client = GenericHTTPClient()

        result = client._truncate_payload(None)

        assert result == ""

    def test_string_payload(self):
        """String payloads are handled correctly."""
        client = GenericHTTPClient()
        data = "This is a simple text response"

        result = client._truncate_payload(data)

        assert result == data


# =============================================================================
# Error Case Tests
# =============================================================================


class TestHTTPClientErrorEvents:
    """Tests for HTTP error event emission."""

    @pytest.fixture(autouse=True)
    def cleanup_context(self):
        """Ensure context is clean before and after each test."""
        set_transcript_collector(None)
        yield
        set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_timeout_emits_error_event(self, mock_collector, mock_connector, mock_endpoint):
        """Timeout errors emit events with error details in summary."""
        import httpx

        client = GenericHTTPClient(timeout=10.0)
        set_transcript_collector(mock_collector)

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.side_effect = httpx.TimeoutException("timeout")
            mock_httpx.return_value.__aenter__.return_value = mock_client

            # Should raise UpstreamApiError
            from meho_app.core.errors import UpstreamApiError

            with pytest.raises(UpstreamApiError) as exc:
                await client.call_endpoint(
                    connector=mock_connector,
                    endpoint=mock_endpoint,
                )

            assert exc.value.status_code == 504

        # Event should still have been emitted before the raise
        mock_collector.create_operation_event.assert_called_once()
        call_args = mock_collector.create_operation_event.call_args
        assert call_args.kwargs["status_code"] == 504
        # Error is included in the summary
        assert "ERROR" in call_args.kwargs["summary"]
        assert "timeout" in call_args.kwargs["summary"].lower()

    @pytest.mark.asyncio
    async def test_request_error_emits_event(self, mock_collector, mock_connector, mock_endpoint):
        """Request errors emit events with error details in summary."""
        import httpx

        client = GenericHTTPClient(timeout=10.0)
        set_transcript_collector(mock_collector)

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.request.side_effect = httpx.RequestError("connection failed")
            mock_httpx.return_value.__aenter__.return_value = mock_client

            from meho_app.core.errors import UpstreamApiError

            with pytest.raises(UpstreamApiError) as exc:
                await client.call_endpoint(
                    connector=mock_connector,
                    endpoint=mock_endpoint,
                )

            assert exc.value.status_code == 503

        # Event should have been emitted
        mock_collector.create_operation_event.assert_called_once()
        call_args = mock_collector.create_operation_event.call_args
        assert call_args.kwargs["status_code"] == 503
        # Error is included in the summary
        assert "ERROR" in call_args.kwargs["summary"]
        assert "connection failed" in call_args.kwargs["summary"]


# =============================================================================
# Knowledge Search Event Tests (Phase 2)
# =============================================================================


class TestKnowledgeSearchEventEmission:
    """Tests for knowledge search event emission via context."""

    @pytest.fixture(autouse=True)
    def cleanup_context(self):
        """Ensure context is clean before and after each test."""
        set_transcript_collector(None)
        yield
        set_transcript_collector(None)

    @pytest.fixture
    def mock_meho_deps(self):
        """Create mock MEHO dependencies for search."""
        deps = MagicMock()
        deps.search_docs = AsyncMock(
            return_value=[
                {"text": "VM documentation...", "source_uri": "docs/vm.md", "tags": ["vm"]},
                {"text": "Container docs...", "source_uri": "docs/k8s.md", "tags": ["k8s"]},
            ]
        )
        deps.search_knowledge = AsyncMock(
            return_value=[
                {"text": "API endpoint...", "source_uri": "specs/api.yaml", "tags": ["api"]},
            ]
        )
        return deps

    @pytest.fixture
    def mock_graph_deps(self, mock_meho_deps):
        """Create mock graph dependencies."""
        from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps

        deps = MagicMock(spec=MEHOGraphDeps)
        deps.meho_deps = mock_meho_deps
        deps.user_id = str(uuid4())
        deps.tenant_id = str(uuid4())
        deps.session_id = str(uuid4())
        return deps

    @pytest.mark.asyncio
    async def test_knowledge_search_emits_event_with_collector(
        self, mock_collector, mock_graph_deps
    ):
        """Knowledge search emits event when collector is in context."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            search_knowledge_handler,
        )

        set_transcript_collector(mock_collector)

        # Mock create_knowledge_search_event
        mock_event = MagicMock()
        mock_collector.create_knowledge_search_event = MagicMock(return_value=mock_event)

        await search_knowledge_handler(mock_graph_deps, {"query": "virtual machines"})

        # Event should have been created and added
        mock_collector.create_knowledge_search_event.assert_called_once()
        mock_collector.add.assert_called_once_with(mock_event)

    @pytest.mark.asyncio
    async def test_knowledge_search_skips_event_without_collector(self, mock_graph_deps):
        """Knowledge search does not error when no collector in context."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            search_knowledge_handler,
        )

        # No collector in context
        set_transcript_collector(None)

        # Should not raise
        result = await search_knowledge_handler(mock_graph_deps, {"query": "pods"})

        # Result should still be returned
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed) >= 1

    @pytest.mark.asyncio
    async def test_knowledge_search_event_captures_results(self, mock_collector, mock_graph_deps):
        """Knowledge search event includes result snippets."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            search_knowledge_handler,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_knowledge_search_event = MagicMock(return_value=mock_event)

        await search_knowledge_handler(mock_graph_deps, {"query": "test"})

        call_args = mock_collector.create_knowledge_search_event.call_args
        result_snippets = call_args.kwargs.get("result_snippets")

        assert result_snippets is not None
        assert len(result_snippets) <= 3  # Max 3 snippets for preview

    @pytest.mark.asyncio
    async def test_knowledge_search_event_captures_type(self, mock_collector, mock_graph_deps):
        """Knowledge search event includes search type (docs vs hybrid)."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            search_knowledge_handler,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_knowledge_search_event = MagicMock(return_value=mock_event)

        # Search without include_apis (docs only)
        await search_knowledge_handler(mock_graph_deps, {"query": "test"})

        call_args = mock_collector.create_knowledge_search_event.call_args
        search_type = call_args.kwargs.get("search_type")

        assert search_type == "docs"

    @pytest.mark.asyncio
    async def test_knowledge_search_event_captures_duration(self, mock_collector, mock_graph_deps):
        """Knowledge search event includes duration in ms."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            search_knowledge_handler,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_knowledge_search_event = MagicMock(return_value=mock_event)

        await search_knowledge_handler(mock_graph_deps, {"query": "test"})

        call_args = mock_collector.create_knowledge_search_event.call_args
        duration_ms = call_args.kwargs.get("duration_ms")

        assert duration_ms is not None
        assert duration_ms >= 0


# =============================================================================
# Topology Lookup Event Tests (Phase 2)
# =============================================================================


class TestTopologyLookupEventEmission:
    """Tests for topology lookup event emission via _log_result()."""

    @pytest.fixture(autouse=True)
    def cleanup_context(self):
        """Ensure context is clean before and after each test."""
        set_transcript_collector(None)
        yield
        set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_topology_lookup_emits_event_with_collector(self, mock_collector):
        """Topology lookup emits event when collector is in context."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        set_transcript_collector(mock_collector)

        # Mock create_topology_lookup_event
        mock_event = MagicMock()
        mock_collector.create_topology_lookup_event = MagicMock(return_value=mock_event)

        node = TopologyLookupNode()

        # Call _log_result which now emits transcript events
        import time

        await node._log_result(
            result_info={"user_message": "test query"},
            start_time=time.perf_counter() - 0.05,
            extracted_entities=["test-entity"],
            found_entities=[{"name": "test-entity", "type": "VM"}],
        )

        # Event should have been created
        mock_collector.create_topology_lookup_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_topology_lookup_skips_event_without_collector(self):
        """Topology lookup does not error when no collector in context."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        # No collector in context
        set_transcript_collector(None)

        node = TopologyLookupNode()

        # Should not raise
        import time

        await node._log_result(
            result_info={"user_message": "test"},
            start_time=time.perf_counter() - 0.01,
            extracted_entities=["test"],
            found_entities=[],
        )
        # No assertion needed - just verifying no exception

    @pytest.mark.asyncio
    async def test_topology_lookup_event_found_entity(self, mock_collector):
        """Topology lookup event captures found=True when entities are found."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_topology_lookup_event = MagicMock(return_value=mock_event)

        node = TopologyLookupNode()
        import time

        await node._log_result(
            result_info={"user_message": "DEV-gameflow-db"},
            start_time=time.perf_counter() - 0.045,
            extracted_entities=["DEV-gameflow-db"],
            found_entities=[{"name": "DEV-gameflow-db", "type": "VM", "connector_type": "proxmox"}],
        )

        call_args = mock_collector.create_topology_lookup_event.call_args
        assert call_args.kwargs["found"] is True
        assert call_args.kwargs["query"] == "DEV-gameflow-db"

    @pytest.mark.asyncio
    async def test_topology_lookup_event_not_found(self, mock_collector):
        """Topology lookup event handles case when entity not found."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_topology_lookup_event = MagicMock(return_value=mock_event)

        node = TopologyLookupNode()
        import time

        await node._log_result(
            result_info={"user_message": "unknown-entity"},
            start_time=time.perf_counter() - 0.02,
            extracted_entities=["unknown-entity"],
            found_entities=[],  # Not found
        )

        call_args = mock_collector.create_topology_lookup_event.call_args
        assert call_args.kwargs["found"] is False
        assert call_args.kwargs["query"] == "unknown-entity"

    @pytest.mark.asyncio
    async def test_topology_lookup_event_captures_duration(self, mock_collector):
        """Topology lookup event includes duration in ms."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        set_transcript_collector(mock_collector)
        mock_event = MagicMock()
        mock_collector.create_topology_lookup_event = MagicMock(return_value=mock_event)

        node = TopologyLookupNode()
        import time

        await node._log_result(
            result_info={"user_message": "test"},
            start_time=time.perf_counter() - 0.12345,
            extracted_entities=["test"],
            found_entities=[],
        )

        call_args = mock_collector.create_topology_lookup_event.call_args
        assert call_args.kwargs["duration_ms"] > 100  # ~123ms
