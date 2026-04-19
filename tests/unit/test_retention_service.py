# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for TranscriptRetentionService.

Tests retention policies, soft-delete, and hard-delete logic
without requiring a real database connection.

Part of TASK-186: Deep Observability & Introspection System.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


class TestRetentionStats:
    """Tests for RetentionStats dataclass."""

    def test_retention_stats_creation(self):
        """Test RetentionStats can be created with all fields."""
        from meho_app.modules.agents.persistence.retention_service import RetentionStats

        stats = RetentionStats(
            total_transcripts=100,
            active_transcripts=80,
            soft_deleted_transcripts=15,
            pending_hard_delete=5,
            oldest_active_timestamp=datetime.now(tz=UTC),
            oldest_soft_deleted_timestamp=datetime.now(tz=UTC) - timedelta(days=10),
        )

        assert stats.total_transcripts == 100
        assert stats.active_transcripts == 80
        assert stats.soft_deleted_transcripts == 15
        assert stats.pending_hard_delete == 5

    def test_retention_stats_with_none_timestamps(self):
        """Test RetentionStats with None timestamps."""
        from meho_app.modules.agents.persistence.retention_service import RetentionStats

        stats = RetentionStats(
            total_transcripts=0,
            active_transcripts=0,
            soft_deleted_transcripts=0,
            pending_hard_delete=0,
            oldest_active_timestamp=None,
            oldest_soft_deleted_timestamp=None,
        )

        assert stats.oldest_active_timestamp is None
        assert stats.oldest_soft_deleted_timestamp is None


class TestCleanupResult:
    """Tests for CleanupResult dataclass."""

    def test_cleanup_result_creation(self):
        """Test CleanupResult can be created."""
        from meho_app.modules.agents.persistence.retention_service import CleanupResult

        result = CleanupResult(
            soft_deleted_count=10,
            hard_deleted_count=5,
            errors=[],
        )

        assert result.soft_deleted_count == 10
        assert result.hard_deleted_count == 5
        assert result.errors == []

    def test_cleanup_result_with_errors(self):
        """Test CleanupResult with errors."""
        from meho_app.modules.agents.persistence.retention_service import CleanupResult

        result = CleanupResult(
            soft_deleted_count=0,
            hard_deleted_count=0,
            errors=["Database error", "Connection timeout"],
        )

        assert len(result.errors) == 2


class TestTranscriptRetentionService:
    """Tests for TranscriptRetentionService."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()
        return session

    @pytest.fixture
    def mock_config(self):
        """Mock configuration."""
        with patch("meho_app.modules.agents.persistence.retention_service.get_config") as mock:
            config = MagicMock()
            config.transcript_retention_days = 30
            config.transcript_grace_days = 7
            mock.return_value = config
            yield mock

    @pytest.mark.asyncio
    def test_service_initialization(self, mock_session, mock_config):
        """Test service initializes with config values."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        service = TranscriptRetentionService(mock_session)

        assert service.session == mock_session
        assert service.retention_days == 30
        assert service.grace_days == 7

    @pytest.mark.asyncio
    async def test_soft_delete_returns_count(self, mock_session, mock_config):
        """Test soft_delete_old_transcripts returns count."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        # Mock returning 5 deleted IDs
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [uuid4() for _ in range(5)]
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        count = await service.soft_delete_old_transcripts()

        assert count == 5
        mock_session.execute.assert_called_once()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_soft_delete_with_custom_retention(self, mock_session, mock_config):
        """Test soft_delete with custom retention days."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        count = await service.soft_delete_old_transcripts(retention_days=15)

        assert count == 0

    @pytest.mark.asyncio
    async def test_hard_delete_returns_count(self, mock_session, mock_config):
        """Test hard_delete_soft_deleted returns count."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        # Mock returning 3 IDs to delete
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [uuid4() for _ in range(3)]
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        count = await service.hard_delete_soft_deleted()

        assert count == 3

    @pytest.mark.asyncio
    async def test_hard_delete_no_records(self, mock_session, mock_config):
        """Test hard_delete when no records to delete."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        count = await service.hard_delete_soft_deleted()

        assert count == 0
        # Should only call execute once (for the SELECT)
        assert mock_session.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_run_cleanup_calls_both(self, mock_session, mock_config):
        """Test run_cleanup calls both soft and hard delete."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        result = await service.run_cleanup()

        assert result.soft_deleted_count == 0
        assert result.hard_deleted_count == 0
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_run_cleanup_captures_errors(self, mock_session, mock_config):
        """Test run_cleanup captures and returns errors."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        # Make soft_delete fail
        mock_session.execute.side_effect = Exception("Database error")

        service = TranscriptRetentionService(mock_session)
        result = await service.run_cleanup()

        assert len(result.errors) >= 1
        assert "Soft-delete failed" in result.errors[0]

    @pytest.mark.asyncio
    async def test_get_retention_stats(self, mock_session, mock_config):
        """Test get_retention_stats returns stats."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        # Mock scalar results for count queries
        mock_result = MagicMock()
        mock_result.scalar.return_value = 10
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        stats = await service.get_retention_stats()

        assert stats.total_transcripts == 10
        assert mock_session.execute.call_count >= 4  # At least 4 queries

    @pytest.mark.asyncio
    async def test_restore_transcript_success(self, mock_session, mock_config):
        """Test restore_transcript returns True on success."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        transcript_id = uuid4()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = transcript_id
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        result = await service.restore_transcript(transcript_id)

        assert result is True
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_transcript_not_found(self, mock_session, mock_config):
        """Test restore_transcript returns False when not found."""
        from meho_app.modules.agents.persistence.retention_service import (
            TranscriptRetentionService,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        service = TranscriptRetentionService(mock_session)
        result = await service.restore_transcript(uuid4())

        assert result is False
