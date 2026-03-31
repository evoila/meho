# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology batch processor.

Tests BatchProcessor background processing logic.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.topology.auto_discovery.base import (
    ExtractedEntity,
    ExtractedRelationship,
)
from meho_app.modules.topology.auto_discovery.processor import (
    BatchProcessor,
    get_batch_processor,
    get_processor_instance,
    reset_batch_processor,
)
from meho_app.modules.topology.auto_discovery.queue import (
    DiscoveryMessage,
    DiscoveryQueue,
)


class TestBatchProcessor:
    """Tests for BatchProcessor."""

    @pytest.fixture
    def mock_queue(self):
        """Create mock discovery queue."""
        queue = MagicMock(spec=DiscoveryQueue)
        queue.pop_batch = AsyncMock(return_value=[])
        return queue

    @pytest.fixture
    def mock_session_maker(self):
        """Create mock session maker."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        session_maker = MagicMock(return_value=mock_session)
        return session_maker

    @pytest.fixture
    def processor(self, mock_queue, mock_session_maker):
        """Create BatchProcessor instance."""
        reset_batch_processor()
        return BatchProcessor(
            queue=mock_queue,
            session_maker=mock_session_maker,
            batch_size=10,
            interval_seconds=1,
        )

    @pytest.fixture
    def sample_message(self):
        """Create sample discovery message."""
        return DiscoveryMessage(
            entities=[
                ExtractedEntity(
                    name="web-01",
                    description="A VM",
                    connector_id="12345678-1234-1234-1234-123456789012",
                )
            ],
            relationships=[
                ExtractedRelationship(
                    from_entity_name="web-01",
                    to_entity_name="esxi-01",
                    relationship_type="runs_on",
                )
            ],
            tenant_id="tenant-1",
        )

    def test_init(self, processor, mock_queue, mock_session_maker):
        """Test processor initialization."""
        assert processor.queue is mock_queue
        assert processor.session_maker is mock_session_maker
        assert processor.batch_size == 10
        assert processor.interval_seconds == 1
        assert processor.is_running is False

    def test_stats_initial(self, processor):
        """Test initial statistics."""
        stats = processor.stats

        assert stats["entities_processed"] == 0
        assert stats["relationships_processed"] == 0
        assert stats["messages_processed"] == 0
        assert stats["errors"] == 0
        assert stats["running"] is False

    def test_reset_stats(self, processor):
        """Test resetting statistics."""
        processor._entities_processed = 10
        processor._messages_processed = 5

        processor.reset_stats()

        assert processor.stats["entities_processed"] == 0
        assert processor.stats["messages_processed"] == 0

    @pytest.mark.asyncio
    async def test_start_stop(self, processor):
        """Test starting and stopping processor."""
        assert processor.is_running is False

        await processor.start()
        assert processor.is_running is True

        # Give it a moment to start
        await asyncio.sleep(0.1)

        await processor.stop(timeout=2.0)
        assert processor.is_running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self, processor):
        """Test starting processor that's already running."""
        await processor.start()

        # Should log warning but not fail
        await processor.start()

        await processor.stop(timeout=2.0)

    @pytest.mark.asyncio
    async def test_stop_not_running(self, processor):
        """Test stopping processor that's not running."""
        # Should log warning but not fail
        await processor.stop()

    @pytest.mark.asyncio
    async def test_process_batch_empty(self, processor, mock_queue):
        """Test processing empty batch."""
        mock_queue.pop_batch.return_value = []

        count = await processor._process_batch()

        assert count == 0
        mock_queue.pop_batch.assert_called_once_with(10)

    @pytest.mark.asyncio
    async def test_process_message(self, processor, sample_message):
        """Test processing a single message."""
        # Mock TopologyService
        mock_store_result = MagicMock()
        mock_store_result.stored = True
        mock_store_result.entities_created = 1
        mock_store_result.relationships_created = 1
        mock_store_result.message = "Stored successfully"

        with patch("meho_app.modules.topology.service.TopologyService") as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_store_result)
            MockService.return_value = mock_service_instance

            await processor._process_message(sample_message)

        # Check statistics updated
        assert processor._entities_processed == 1
        assert processor._relationships_processed == 1

    @pytest.mark.asyncio
    async def test_process_batch_with_messages(self, processor, mock_queue, sample_message):
        """Test processing batch with messages."""
        mock_queue.pop_batch.return_value = [sample_message]

        # Mock TopologyService
        mock_store_result = MagicMock()
        mock_store_result.stored = True
        mock_store_result.entities_created = 1
        mock_store_result.relationships_created = 1
        mock_store_result.message = "Stored successfully"

        with patch("meho_app.modules.topology.service.TopologyService") as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_store_result)
            MockService.return_value = mock_service_instance

            count = await processor._process_batch()

        assert count == 1
        assert processor._messages_processed == 1

    @pytest.mark.asyncio
    async def test_process_message_error_handling(self, processor, sample_message):
        """Test error handling during message processing."""
        with patch("meho_app.modules.topology.service.TopologyService") as MockService:
            MockService.side_effect = Exception("Database error")

            # Should not raise, just increment error counter
            with pytest.raises(Exception):  # noqa: B017, PT011 -- testing broad exception raise is intentional
                await processor._process_message(sample_message)

    @pytest.mark.asyncio
    async def test_process_one(self, processor, mock_queue, sample_message):
        """Test processing a single message immediately."""
        mock_queue.pop_batch.return_value = [sample_message]

        # Mock TopologyService
        mock_store_result = MagicMock()
        mock_store_result.stored = True
        mock_store_result.entities_created = 1
        mock_store_result.relationships_created = 0
        mock_store_result.message = "Stored"

        with patch("meho_app.modules.topology.service.TopologyService") as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_store_result)
            MockService.return_value = mock_service_instance

            result = await processor.process_one()

        assert result is True

    @pytest.mark.asyncio
    async def test_process_one_empty_queue(self, processor, mock_queue):
        """Test process_one with empty queue."""
        mock_queue.pop_batch.return_value = []

        result = await processor.process_one()

        assert result is False

    def test_trigger_not_running(self, processor):
        """Test trigger when processor is not running."""
        # Should be a no-op, not raise
        processor.trigger()
        # Event should not be set since processor is not running
        assert not processor._wakeup_event.is_set()

    @pytest.mark.asyncio
    async def test_trigger_when_running(self, processor):
        """Test trigger sets wakeup event when processor is running."""
        await processor.start()
        assert processor.is_running

        # Trigger should set the event
        processor.trigger()
        assert processor._wakeup_event.is_set()

        await processor.stop(timeout=2.0)

    @pytest.mark.asyncio
    async def test_immediate_wakeup_on_trigger(self, processor, mock_queue, sample_message):
        """Test that trigger causes immediate processing instead of waiting."""
        # Track when batch processing is called
        processing_times = []
        original_process_batch = processor._process_batch

        async def tracked_process_batch():
            processing_times.append(asyncio.get_event_loop().time())
            return await original_process_batch()

        processor._process_batch = tracked_process_batch
        mock_queue.pop_batch.return_value = []

        # Start processor with long interval
        processor.interval_seconds = 10  # Would normally wait 10 seconds
        await processor.start()

        # Wait a bit for first processing
        await asyncio.sleep(0.1)
        initial_count = len(processing_times)

        # Trigger should cause immediate processing
        trigger_time = asyncio.get_event_loop().time()
        processor.trigger()

        # Wait a short time - should process immediately, not wait 10s
        await asyncio.sleep(0.2)

        await processor.stop(timeout=2.0)

        # Should have processed again after trigger
        assert len(processing_times) > initial_count
        # The processing after trigger should happen quickly (< 1s), not after 10s
        if len(processing_times) > initial_count:
            post_trigger_time = processing_times[initial_count]
            assert post_trigger_time - trigger_time < 1.0


class TestBatchProcessorSingleton:
    """Tests for batch processor singleton management."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_batch_processor()

    @pytest.mark.asyncio
    async def test_get_processor_requires_queue(self):
        """Test that get_batch_processor requires queue on first call."""
        with pytest.raises(ValueError, match="queue is required"):
            await get_batch_processor()

    @pytest.mark.asyncio
    async def test_get_processor_requires_session_maker(self):
        """Test that get_batch_processor requires session_maker on first call."""
        queue = MagicMock(spec=DiscoveryQueue)

        with pytest.raises(ValueError, match="session_maker is required"):
            await get_batch_processor(queue=queue)

    @pytest.mark.asyncio
    async def test_get_processor_creates_instance(self):
        """Test that get_batch_processor creates instance."""
        queue = MagicMock(spec=DiscoveryQueue)
        session_maker = MagicMock()

        processor = await get_batch_processor(
            queue=queue,
            session_maker=session_maker,
        )

        assert processor is not None
        assert isinstance(processor, BatchProcessor)

    @pytest.mark.asyncio
    async def test_get_processor_returns_same_instance(self):
        """Test that get_batch_processor returns singleton."""
        queue = MagicMock(spec=DiscoveryQueue)
        session_maker = MagicMock()

        processor1 = await get_batch_processor(
            queue=queue,
            session_maker=session_maker,
        )
        processor2 = await get_batch_processor()

        assert processor1 is processor2

    @pytest.mark.asyncio
    async def test_reset_processor(self):
        """Test resetting processor singleton."""
        queue = MagicMock(spec=DiscoveryQueue)
        session_maker = MagicMock()

        processor1 = await get_batch_processor(
            queue=queue,
            session_maker=session_maker,
        )
        reset_batch_processor()
        processor2 = await get_batch_processor(
            queue=queue,
            session_maker=session_maker,
        )

        assert processor1 is not processor2

    def test_get_processor_instance_none(self):
        """Test get_processor_instance returns None when not initialized."""
        reset_batch_processor()
        assert get_processor_instance() is None

    @pytest.mark.asyncio
    async def test_get_processor_instance_returns_instance(self):
        """Test get_processor_instance returns the singleton."""
        queue = MagicMock(spec=DiscoveryQueue)
        session_maker = MagicMock()

        processor = await get_batch_processor(
            queue=queue,
            session_maker=session_maker,
        )

        instance = get_processor_instance()
        assert instance is processor

        reset_batch_processor()
        assert get_processor_instance() is None
