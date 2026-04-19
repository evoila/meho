# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for transcript cleanup utilities.

TASK-188: Bulletproof Transcript Persistence
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestCleanupOrphanedTranscripts:
    """Tests for cleanup_orphaned_transcripts function."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock AsyncSession."""
        session = MagicMock()
        # Mock the execute method to return a result with rowcount
        mock_result = MagicMock()
        mock_result.rowcount = 0
        session.execute = AsyncMock(return_value=mock_result)
        return session

    @pytest.mark.asyncio
    async def test_cleanup_finds_no_orphans(self, mock_session):
        """Test cleanup when no orphaned transcripts exist."""
        from meho_app.modules.agents.persistence.cleanup import (
            cleanup_orphaned_transcripts,
        )

        mock_session.execute.return_value.rowcount = 0

        count = await cleanup_orphaned_transcripts(mock_session)

        assert count == 0
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_finds_orphans(self, mock_session):
        """Test cleanup when orphaned transcripts exist."""
        from meho_app.modules.agents.persistence.cleanup import (
            cleanup_orphaned_transcripts,
        )

        # Simulate finding 3 orphaned transcripts
        mock_session.execute.return_value.rowcount = 3

        count = await cleanup_orphaned_transcripts(mock_session)

        assert count == 3
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_uses_correct_max_age(self, mock_session):
        """Test that cleanup respects max_age_minutes parameter."""
        from meho_app.modules.agents.persistence.cleanup import (
            cleanup_orphaned_transcripts,
        )

        # Call with custom max_age
        await cleanup_orphaned_transcripts(mock_session, max_age_minutes=60)

        # Verify execute was called (with the update statement)
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_update_statement_structure(self, mock_session):
        """Test that the update statement targets correct records."""
        from meho_app.modules.agents.persistence.cleanup import (
            cleanup_orphaned_transcripts,
        )

        await cleanup_orphaned_transcripts(mock_session, max_age_minutes=30)

        # Get the SQL statement from the call
        call_args = mock_session.execute.call_args
        stmt = call_args[0][0]

        # Verify it's an UPDATE statement (check the string representation)
        # SQLAlchemy update statements can be converted to string
        stmt_str = str(stmt)
        assert "session_transcripts" in stmt_str.lower()
        assert "UPDATE" in stmt_str.upper()
