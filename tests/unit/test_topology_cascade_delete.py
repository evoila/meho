# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology cascade delete on connector deletion.

Tests that:
1. TopologyRepository.delete_entities_by_connector removes entities for a connector
2. TopologyRepository.delete_orphaned_entities removes entities with invalid connector_ids
3. TopologyService.delete_entities_for_connector integrates with repository
4. ConnectorService.delete_connector calls topology cleanup
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


class TestTopologyRepositoryDeleteByConnector:
    """Unit tests for TopologyRepository.delete_entities_by_connector."""

    @pytest.mark.asyncio
    async def test_delete_entities_by_connector_returns_count(self):
        """Verify delete_entities_by_connector returns the number of deleted entities."""
        from meho_app.modules.topology.repository import TopologyRepository

        # Create mock session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)
        connector_id = uuid4()

        count = await repo.delete_entities_by_connector(connector_id)

        assert count == 5
        assert mock_session.execute.called

    @pytest.mark.asyncio
    async def test_delete_entities_by_connector_returns_zero_for_no_entities(self):
        """Verify delete_entities_by_connector returns 0 when no entities exist."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)
        connector_id = uuid4()

        count = await repo.delete_entities_by_connector(connector_id)

        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_entities_by_connector_handles_none_rowcount(self):
        """Verify delete_entities_by_connector handles None rowcount."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)
        connector_id = uuid4()

        count = await repo.delete_entities_by_connector(connector_id)

        assert count == 0


class TestTopologyRepositoryDeleteOrphaned:
    """Unit tests for TopologyRepository.delete_orphaned_entities."""

    @pytest.mark.asyncio
    async def test_delete_orphaned_entities_with_valid_connectors(self):
        """Verify delete_orphaned_entities filters by valid connector IDs."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 3
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)
        valid_ids = [uuid4(), uuid4()]

        count = await repo.delete_orphaned_entities(valid_ids)

        assert count == 3
        assert mock_session.execute.called

    @pytest.mark.asyncio
    async def test_delete_orphaned_entities_with_empty_list(self):
        """Verify delete_orphaned_entities handles empty valid connector list."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 10
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)

        count = await repo.delete_orphaned_entities([])

        # With no valid connectors, all entities with connector_id should be deleted
        assert count == 10


class TestTopologyServiceDeleteEntitiesForConnector:
    """Unit tests for TopologyService.delete_entities_for_connector."""

    @pytest.mark.asyncio
    async def test_delete_entities_for_connector_calls_repository(self):
        """Verify delete_entities_for_connector delegates to repository."""
        from meho_app.modules.topology.service import TopologyService

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        service = TopologyService(mock_session)

        # Mock the repository method
        service.repository.delete_entities_by_connector = AsyncMock(return_value=7)

        connector_id = uuid4()
        count = await service.delete_entities_for_connector(connector_id)

        assert count == 7
        service.repository.delete_entities_by_connector.assert_called_once_with(connector_id)
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_entities_calls_repository(self):
        """Verify cleanup_orphaned_entities delegates to repository."""
        from meho_app.modules.topology.service import TopologyService

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        service = TopologyService(mock_session)
        service.repository.delete_orphaned_entities = AsyncMock(return_value=4)

        valid_ids = [uuid4(), uuid4()]
        count = await service.cleanup_orphaned_entities(valid_ids)

        assert count == 4
        service.repository.delete_orphaned_entities.assert_called_once_with(valid_ids)
        mock_session.commit.assert_called_once()


class TestConnectorServiceDeleteWithTopologyCleanup:
    """Unit tests for ConnectorService.delete_connector topology cleanup."""

    @pytest.mark.asyncio
    async def test_delete_connector_cleans_up_topology(self):
        """Verify delete_connector calls topology cleanup before deletion."""
        from meho_app.modules.connectors.service import ConnectorService

        mock_session = AsyncMock()
        mock_connector_repo = AsyncMock()
        mock_connector_repo.delete_connector = AsyncMock(return_value=True)

        service = ConnectorService(session=mock_session, connector_repo=mock_connector_repo)

        connector_id = str(uuid4())

        # Patch at the module where the import happens (inside the function)
        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = AsyncMock()
            mock_topology_service.delete_entities_for_connector = AsyncMock(return_value=5)
            mock_topology_class.return_value = mock_topology_service

            result = await service.delete_connector(connector_id)

        assert result is True
        mock_topology_service.delete_entities_for_connector.assert_called_once()
        mock_connector_repo.delete_connector.assert_called_once_with(connector_id, None)

    @pytest.mark.asyncio
    async def test_delete_connector_continues_on_topology_failure(self):
        """Verify delete_connector continues even if topology cleanup fails."""
        from meho_app.modules.connectors.service import ConnectorService

        mock_session = AsyncMock()
        mock_connector_repo = AsyncMock()
        mock_connector_repo.delete_connector = AsyncMock(return_value=True)

        service = ConnectorService(session=mock_session, connector_repo=mock_connector_repo)

        connector_id = str(uuid4())

        # Patch at the module where the import happens (inside the function)
        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = AsyncMock()
            mock_topology_service.delete_entities_for_connector = AsyncMock(
                side_effect=Exception("Topology DB error")
            )
            mock_topology_class.return_value = mock_topology_service

            # Should not raise, should proceed with connector deletion
            result = await service.delete_connector(connector_id)

        assert result is True
        # Connector should still be deleted despite topology failure
        mock_connector_repo.delete_connector.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_connector_without_session_skips_topology(self):
        """Verify delete_connector skips topology cleanup when no session."""
        from meho_app.modules.connectors.service import ConnectorService

        mock_connector_repo = AsyncMock()
        mock_connector_repo.delete_connector = AsyncMock(return_value=True)

        # Create service with protocol-based construction (no session)
        service = ConnectorService.from_protocols(connector_repo=mock_connector_repo)

        connector_id = str(uuid4())

        # Patch at the module where the import happens (inside the function)
        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            result = await service.delete_connector(connector_id)

        assert result is True
        # Topology service should not be instantiated without session
        mock_topology_class.assert_not_called()
        mock_connector_repo.delete_connector.assert_called_once()
