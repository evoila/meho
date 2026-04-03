# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for connector topology registration (TASK-144).

Tests that connectors are automatically registered as topology entities
when created through the ConnectorService.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.connectors.schemas import Connector, ConnectorCreate
from meho_app.modules.connectors.service import ConnectorService


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture
def connector_create_data():
    """Sample connector creation data."""
    return ConnectorCreate(
        name="Test K8s Connector",
        base_url="https://kubernetes.example.com:6443",
        auth_type="OAUTH2",
        tenant_id="test-tenant",
        connector_type="kubernetes",
        description="Test Kubernetes cluster",
        protocol_config={"kubernetes": True},
    )


def create_mock_connector(connector_id: str, data: ConnectorCreate) -> Connector:
    """Helper to create a mock Connector with all required fields."""
    return Connector(
        id=connector_id,
        name=data.name,
        base_url=data.base_url,
        auth_type=data.auth_type,
        tenant_id=data.tenant_id,
        connector_type=data.connector_type,
        description=data.description,
        protocol_config=data.protocol_config,
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


class TestConnectorTopologyRegistration:
    """Test that connector creation registers topology entities."""

    @pytest.mark.asyncio
    async def test_create_connector_registers_topology_entity(
        self,
        mock_session,
        connector_create_data,
    ):
        """Verify that creating a connector also creates a topology entity."""
        connector_id = str(uuid4())
        topology_entity_id = uuid4()

        mock_connector = create_mock_connector(connector_id, connector_create_data)

        mock_connector_repo = AsyncMock()
        mock_connector_repo.create_connector = AsyncMock(return_value=mock_connector)
        mock_connector_repo.update_connector = AsyncMock(return_value=mock_connector)

        with (
            patch(
                "meho_app.modules.topology.service.TopologyService"
            ) as mock_topology_service_class,
            patch(
                "meho_app.modules.topology.repository.TopologyRepository"
            ) as mock_topology_repo_class,
        ):
            mock_topology_service = AsyncMock()
            mock_topology_service.store_discovery = AsyncMock(
                return_value=MagicMock(stored=True, entities_created=1)
            )
            mock_topology_service_class.return_value = mock_topology_service

            mock_entity = MagicMock()
            mock_entity.id = topology_entity_id
            mock_topology_repo = AsyncMock()
            mock_topology_repo.get_entity_by_name = AsyncMock(return_value=mock_entity)
            mock_topology_repo_class.return_value = mock_topology_repo

            service = ConnectorService(
                session=mock_session,
                connector_repo=mock_connector_repo,
            )

            result = await service.create_connector(connector_create_data)

            assert result is not None
            mock_connector_repo.create_connector.assert_called_once()
            mock_topology_service.store_discovery.assert_called_once()

            call_args = mock_topology_service.store_discovery.call_args
            store_input = call_args[0][0]
            assert len(store_input.entities) == 1

            entity = store_input.entities[0]
            assert entity.name == connector_create_data.name
            assert entity.raw_attributes["connector_type"] == "kubernetes"
            assert entity.raw_attributes["is_connector_entity"] is True

            mock_connector_repo.update_connector.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_connector_handles_topology_failure_gracefully(
        self,
        mock_session,
        connector_create_data,
    ):
        """Verify connector creation succeeds even if topology registration fails."""
        connector_id = str(uuid4())

        mock_connector = create_mock_connector(connector_id, connector_create_data)

        mock_connector_repo = AsyncMock()
        mock_connector_repo.create_connector = AsyncMock(return_value=mock_connector)

        with patch(
            "meho_app.modules.topology.service.TopologyService"
        ) as mock_topology_service_class:
            mock_topology_service = AsyncMock()
            mock_topology_service.store_discovery = AsyncMock(
                side_effect=Exception("Topology service unavailable")
            )
            mock_topology_service_class.return_value = mock_topology_service

            service = ConnectorService(
                session=mock_session,
                connector_repo=mock_connector_repo,
            )

            result = await service.create_connector(connector_create_data)

            assert result is not None
            assert result.id == connector_id
            mock_connector_repo.create_connector.assert_called_once()
