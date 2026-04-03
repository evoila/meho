# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for event context module.

Tests the context variable pattern for transcript collector propagation.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.agents.persistence.event_context import (
    get_transcript_collector,
    has_transcript_collector,
    set_transcript_collector,
)


class TestEventContext:
    """Tests for event context utilities."""

    def test_default_is_none(self):
        """Context starts as None with no collector set."""
        # Ensure clean state
        set_transcript_collector(None)

        assert get_transcript_collector() is None
        assert has_transcript_collector() is False

    def test_set_and_get_collector(self):
        """Can set and retrieve a collector from context."""
        mock_collector = MagicMock()

        try:
            set_transcript_collector(mock_collector)

            assert get_transcript_collector() is mock_collector
            assert has_transcript_collector() is True
        finally:
            # Clean up
            set_transcript_collector(None)

    def test_clear_collector(self):
        """Setting None clears the collector from context."""
        mock_collector = MagicMock()
        set_transcript_collector(mock_collector)

        # Verify it's set
        assert has_transcript_collector() is True

        # Clear it
        set_transcript_collector(None)

        assert get_transcript_collector() is None
        assert has_transcript_collector() is False


class TestEventContextAsyncIsolation:
    """Tests for context variable isolation in async code."""

    @pytest.mark.asyncio
    async def test_context_persists_across_await(self):
        """Context variable persists across await points in same task."""
        mock_collector = MagicMock()

        try:
            set_transcript_collector(mock_collector)

            # Simulate async work
            await asyncio.sleep(0.001)

            # Still has the collector
            assert get_transcript_collector() is mock_collector
            assert has_transcript_collector() is True
        finally:
            set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_context_isolated_between_tasks(self):
        """Context variable is isolated per async task."""
        collector1 = MagicMock(name="collector1")
        collector2 = MagicMock(name="collector2")

        results = {}

        async def task1():
            set_transcript_collector(collector1)
            await asyncio.sleep(0.01)  # Let task2 run
            results["task1"] = get_transcript_collector()

        async def task2():
            set_transcript_collector(collector2)
            await asyncio.sleep(0.01)  # Let task1 run
            results["task2"] = get_transcript_collector()

        try:
            # Run tasks concurrently
            await asyncio.gather(task1(), task2())

            # Each task should see its own collector
            assert results["task1"] is collector1
            assert results["task2"] is collector2
        finally:
            set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_child_task_inherits_context(self):
        """Child tasks inherit parent's context at creation time."""
        parent_collector = MagicMock(name="parent")

        child_result = {}

        async def child_task():
            # Should see parent's collector (inherited at creation)
            child_result["collector"] = get_transcript_collector()

        try:
            set_transcript_collector(parent_collector)

            # Create child task while parent has collector set
            task = asyncio.create_task(child_task())
            await task

            # Child inherited parent's context
            assert child_result["collector"] is parent_collector
        finally:
            set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_no_collector_gracefully_handled(self):
        """Code can safely check for collector when none is set."""
        set_transcript_collector(None)

        # This pattern should work without errors
        collector = get_transcript_collector()
        if collector:
            await collector.add(MagicMock())  # Would only run if collector exists

        # No errors, and we can check the flag
        assert not has_transcript_collector()


class TestEventContextWithMockCollector:
    """Tests using a mock transcript collector."""

    @pytest.fixture
    def mock_collector(self):
        """Create a mock TranscriptCollector."""
        collector = MagicMock()
        collector.transcript_id = uuid4()
        collector.session_id = uuid4()
        collector.add = AsyncMock()
        collector.create_operation_event = MagicMock(return_value=MagicMock())
        return collector

    @pytest.mark.asyncio
    async def test_pattern_for_http_event_emission(self, mock_collector):
        """Demonstrates the pattern used in HTTP client for event emission."""
        try:
            set_transcript_collector(mock_collector)

            # This is the pattern used in GenericHTTPClient
            collector = get_transcript_collector()
            if collector:
                event = collector.create_operation_event(
                    summary="GET /api/test -> 200",
                    method="GET",
                    url="http://example.com/api/test",
                    status_code=200,
                    duration_ms=150.0,
                )
                await collector.add(event)

            # Verify the pattern worked
            mock_collector.create_operation_event.assert_called_once()
            mock_collector.add.assert_called_once()
        finally:
            set_transcript_collector(None)

    @pytest.mark.asyncio
    async def test_pattern_when_no_collector(self, mock_collector):
        """When no collector is set, event emission is skipped gracefully."""
        # Ensure no collector is set
        set_transcript_collector(None)

        # This is the pattern used in GenericHTTPClient
        collector = get_transcript_collector()
        if collector:
            event = collector.create_operation_event(
                summary="GET /api/test -> 200",
                method="GET",
                url="http://example.com/api/test",
                status_code=200,
                duration_ms=150.0,
            )
            await collector.add(event)

        # Nothing was called on the mock (since it wasn't in context)
        mock_collector.create_operation_event.assert_not_called()
        mock_collector.add.assert_not_called()


class TestTranscriptCollectorKnowledgeSearchEvents:
    """Tests for knowledge search event creation in TranscriptCollector."""

    @pytest.fixture
    def mock_service(self):
        """Create a mock TranscriptService."""
        service = AsyncMock()
        service.batch_add_events = AsyncMock()
        return service

    @pytest.fixture
    def collector(self, mock_service):
        """Create a TranscriptCollector with mock service."""
        from meho_app.modules.agents.persistence.transcript_collector import (
            TranscriptCollector,
        )

        return TranscriptCollector(
            transcript_id=uuid4(),
            session_id=uuid4(),
            service=mock_service,
        )

    def test_create_knowledge_search_event_fields(self, collector):
        """Knowledge search event includes all specified fields."""
        event = collector.create_knowledge_search_event(
            summary="Search: 'virtual machines' -> 5 results",
            query="virtual machines",
            search_type="hybrid",
            results_count=5,
            top_scores=[0.95, 0.88, 0.82],
            result_snippets=[
                {"content": "VM documentation...", "source": "docs/vm.md"},
                {"content": "VM management...", "source": "docs/manage.md"},
            ],
            duration_ms=150.5,
            node_name="search_knowledge",
        )

        assert event.type == "knowledge_search"
        assert event.summary == "Search: 'virtual machines' -> 5 results"
        assert event.details.search_query == "virtual machines"
        assert event.details.search_type == "hybrid"
        assert event.details.search_scores == [0.95, 0.88, 0.82]
        assert len(event.details.search_results) == 2
        assert event.node_name == "search_knowledge"

    def test_create_knowledge_search_event_minimal(self, collector):
        """Knowledge search event works with only required fields."""
        event = collector.create_knowledge_search_event(
            summary="Search: 'pods' -> 0 results",
            query="pods",
            search_type="semantic",
            results_count=0,
        )

        assert event.type == "knowledge_search"
        assert event.details.search_query == "pods"
        assert event.details.search_type == "semantic"
        assert event.details.search_results is None
        assert event.details.search_scores is None

    @pytest.mark.asyncio
    async def test_update_stats_knowledge_search(self, collector):
        """Stats are updated when knowledge search event is added."""
        event = collector.create_knowledge_search_event(
            summary="Search test",
            query="test query",
            search_type="hybrid",
            results_count=3,
        )

        assert collector._knowledge_searches == 0

        await collector.add(event)

        assert collector._knowledge_searches == 1


class TestTranscriptCollectorTopologyLookupEvents:
    """Tests for topology lookup event creation in TranscriptCollector."""

    @pytest.fixture
    def mock_service(self):
        """Create a mock TranscriptService."""
        service = AsyncMock()
        service.batch_add_events = AsyncMock()
        return service

    @pytest.fixture
    def collector(self, mock_service):
        """Create a TranscriptCollector with mock service."""
        from meho_app.modules.agents.persistence.transcript_collector import (
            TranscriptCollector,
        )

        return TranscriptCollector(
            transcript_id=uuid4(),
            session_id=uuid4(),
            service=mock_service,
        )

    def test_create_topology_lookup_event_fields(self, collector):
        """Topology lookup event includes all specified fields when entity found."""
        event = collector.create_topology_lookup_event(
            summary="Topology lookup: found 1 entity",
            query="DEV-gameflow-db",
            found=True,
            entity_type="VM",
            entity_name="DEV-gameflow-db",
            connector_type="proxmox",
            chain_length=3,
            same_as_count=1,
            possibly_related_count=2,
            duration_ms=45.0,
            node_name="topology_lookup",
        )

        assert event.type == "topology_lookup"
        assert event.summary == "Topology lookup: found 1 entity"
        assert event.details.entities_extracted == ["DEV-gameflow-db"]
        assert event.details.entities_found is not None
        assert len(event.details.entities_found) == 1
        assert event.details.entities_found[0]["name"] == "DEV-gameflow-db"
        assert event.details.entities_found[0]["type"] == "VM"
        assert event.details.entities_found[0]["connector_type"] == "proxmox"
        assert event.node_name == "topology_lookup"

    def test_create_topology_lookup_event_not_found(self, collector):
        """Topology lookup event handles case when entity not found."""
        event = collector.create_topology_lookup_event(
            summary="Topology lookup: no entities found",
            query="unknown-entity",
            found=False,
            duration_ms=20.0,
        )

        assert event.type == "topology_lookup"
        assert event.details.entities_extracted == ["unknown-entity"]
        assert event.details.entities_found is None  # Not found

    @pytest.mark.asyncio
    async def test_update_stats_topology_lookup(self, collector):
        """Stats are updated when topology lookup event is added."""
        event = collector.create_topology_lookup_event(
            summary="Lookup test",
            query="test-entity",
            found=True,
            entity_name="test-entity",
        )

        assert collector._topology_lookups == 0

        await collector.add(event)

        assert collector._topology_lookups == 1
