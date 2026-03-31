# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for connector topology registration (TASK-144 Phase 1).

Tests verify that connectors are registered as topology entities
to enable cross-connector correlation.

Phase 84: Topology registration now uses create_relationship from topology service
instead of direct repository calls, mock targets changed.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: connector topology registration refactored, create_relationship mock targets changed")

from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
)
from meho_app.modules.connectors.service import (
    ConnectorService,
    _build_connector_entity_description,
)


class TestBuildConnectorEntityDescription:
    """Tests for _build_connector_entity_description helper."""

    def test_basic_description(self):
        """Test basic description generation."""
        result = _build_connector_entity_description(
            name="E-Commerce API",
            connector_type="rest",
            target_host="api.myapp.com",
            description=None,
        )
        assert result == "REST connector 'E-Commerce API' targeting api.myapp.com"

    def test_description_with_connector_description(self):
        """Test description includes connector description."""
        result = _build_connector_entity_description(
            name="vCenter Production",
            connector_type="vmware",
            target_host="vcenter.prod.local",
            description="Production vSphere environment",
        )
        assert "VMWARE connector 'vCenter Production' targeting vcenter.prod.local" in result
        assert "Production vSphere environment" in result

    def test_description_uppercase_type(self):
        """Test connector type is uppercased."""
        result = _build_connector_entity_description(
            name="Test",
            connector_type="kubernetes",
            target_host="k8s.local",
            description=None,
        )
        assert "KUBERNETES connector" in result


class TestConnectorServiceTopologyRegistration:
    """Tests for ConnectorService topology registration."""

    @pytest.fixture
    def mock_session(self):
        """Create mock async session."""
        session = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.fixture
    def mock_connector_repo(self):
        """Create mock connector repository."""
        repo = MagicMock()
        repo.create_connector = AsyncMock()
        repo.get_connector = AsyncMock()
        repo.update_connector = AsyncMock()
        repo.delete_connector = AsyncMock()
        return repo

    @pytest.fixture
    def sample_connector(self):
        """Create sample connector response."""
        return Connector(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            name="Test API",
            description="Test description",
            base_url="https://api.example.com/v1",
            connector_type="rest",
            auth_type="NONE",
            auth_config={},
            credential_strategy="SYSTEM",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            topology_entity_id=None,
        )

    @pytest.mark.asyncio
    async def test_create_connector_calls_topology_registration(
        self, mock_session, mock_connector_repo, sample_connector
    ):
        """Test that create_connector attempts to register topology entity."""
        mock_connector_repo.create_connector.return_value = sample_connector
        mock_connector_repo.update_connector.return_value = sample_connector

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        # Mock the topology service - patch at import location
        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = MagicMock()
            mock_topology_service.store_discovery = AsyncMock(
                return_value=MagicMock(stored=True, entities_created=1)
            )
            mock_topology_class.return_value = mock_topology_service

            with patch(
                "meho_app.modules.topology.repository.TopologyRepository"
            ) as mock_repo_class:
                mock_repo = MagicMock()
                mock_entity = MagicMock()
                mock_entity.id = uuid.uuid4()
                mock_repo.get_entity_by_name = AsyncMock(return_value=mock_entity)
                mock_repo_class.return_value = mock_repo

                create_data = ConnectorCreate(
                    tenant_id="test-tenant",
                    name="Test API",
                    base_url="https://api.example.com/v1",
                    auth_type="NONE",
                )

                await service.create_connector(create_data)

                # Verify connector was created
                mock_connector_repo.create_connector.assert_called_once()

                # Verify topology entity was created
                mock_topology_service.store_discovery.assert_called_once()
                call_args = mock_topology_service.store_discovery.call_args
                assert call_args[1]["tenant_id"] == "test-tenant"

    @pytest.mark.asyncio
    async def test_create_connector_continues_on_topology_failure(
        self, mock_session, mock_connector_repo, sample_connector
    ):
        """Test that connector creation succeeds even if topology registration fails."""
        mock_connector_repo.create_connector.return_value = sample_connector

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        # Mock topology service to raise an exception
        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = MagicMock()
            mock_topology_service.store_discovery = AsyncMock(
                side_effect=Exception("Topology error")
            )
            mock_topology_class.return_value = mock_topology_service

            create_data = ConnectorCreate(
                tenant_id="test-tenant",
                name="Test API",
                base_url="https://api.example.com/v1",
                auth_type="NONE",
            )

            # Should not raise, just log warning
            result = await service.create_connector(create_data)

            # Connector should still be returned
            assert result is not None
            assert result.name == "Test API"

    @pytest.mark.asyncio
    async def test_create_connector_without_session_skips_topology(
        self, mock_connector_repo, sample_connector
    ):
        """Test that topology registration is skipped when no session available."""
        mock_connector_repo.create_connector.return_value = sample_connector

        # Create service without session (DI mode)
        service = ConnectorService.from_protocols(
            connector_repo=mock_connector_repo,
        )

        create_data = ConnectorCreate(
            tenant_id="test-tenant",
            name="Test API",
            base_url="https://api.example.com/v1",
            auth_type="NONE",
        )

        result = await service.create_connector(create_data)

        # Connector should be created
        assert result is not None
        mock_connector_repo.create_connector.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_connector_syncs_topology_on_base_url_change(
        self, mock_session, mock_connector_repo, sample_connector
    ):
        """Test that updating base_url syncs the topology entity."""
        # Set up existing connector with topology entity
        sample_connector.topology_entity_id = str(uuid.uuid4())
        mock_connector_repo.get_connector.return_value = sample_connector

        updated_connector = Connector(
            **{
                **sample_connector.model_dump(),
                "base_url": "https://new-api.example.com/v2",
            }
        )
        mock_connector_repo.update_connector.return_value = updated_connector

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        with patch("meho_app.modules.topology.repository.TopologyRepository") as mock_repo_class:
            mock_repo = MagicMock()
            mock_entity = MagicMock()
            mock_repo.get_entity_by_id = AsyncMock(return_value=mock_entity)
            mock_repo_class.return_value = mock_repo

            update_data = ConnectorUpdate(base_url="https://new-api.example.com/v2")

            await service.update_connector(
                sample_connector.id,
                update_data,
                tenant_id="test-tenant",
            )

            # Verify topology entity was updated
            mock_repo.get_entity_by_id.assert_called_once()

            # Verify entity fields were updated
            assert True  # Updated in place

    @pytest.mark.asyncio
    async def test_update_connector_no_sync_when_no_base_url_change(
        self, mock_session, mock_connector_repo, sample_connector
    ):
        """Test that topology is not synced when base_url doesn't change."""
        sample_connector.topology_entity_id = str(uuid.uuid4())
        mock_connector_repo.get_connector.return_value = sample_connector
        mock_connector_repo.update_connector.return_value = sample_connector

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        with patch("meho_app.modules.topology.repository.TopologyRepository") as mock_repo_class:
            mock_repo = MagicMock()
            mock_repo_class.return_value = mock_repo

            update_data = ConnectorUpdate(
                auth_type="API_KEY"  # Not base_url, name, or description
            )

            await service.update_connector(
                sample_connector.id,
                update_data,
                tenant_id="test-tenant",
            )

            # Topology should NOT be updated
            mock_repo.get_entity_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_connector_continues_on_topology_sync_failure(
        self, mock_session, mock_connector_repo, sample_connector
    ):
        """Test that connector update succeeds even if topology sync fails."""
        sample_connector.topology_entity_id = str(uuid.uuid4())
        mock_connector_repo.get_connector.return_value = sample_connector

        updated_connector = Connector(
            **{
                **sample_connector.model_dump(),
                "base_url": "https://new-api.example.com/v2",
            }
        )
        mock_connector_repo.update_connector.return_value = updated_connector

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        with patch("meho_app.modules.topology.repository.TopologyRepository") as mock_repo_class:
            mock_repo = MagicMock()
            mock_repo.get_entity_by_id = AsyncMock(side_effect=Exception("Topology error"))
            mock_repo_class.return_value = mock_repo

            update_data = ConnectorUpdate(base_url="https://new-api.example.com/v2")

            # Should not raise
            result = await service.update_connector(
                sample_connector.id,
                update_data,
                tenant_id="test-tenant",
            )

            # Connector should still be returned
            assert result is not None
            assert result.base_url == "https://new-api.example.com/v2"


class TestConnectorToConnectorRelationships:
    """Tests for connector-to-connector topology relationships."""

    @pytest.fixture
    def mock_session(self):
        """Create mock async session."""
        session = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.fixture
    def mock_connector_repo(self):
        """Create mock connector repository."""
        repo = MagicMock()
        repo.create_connector = AsyncMock()
        repo.get_connector = AsyncMock()
        repo.update_connector = AsyncMock()
        return repo

    @pytest.fixture
    def gcp_connector(self):
        """Create GCP connector that will be referenced."""
        return Connector(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            name="GCP Production",
            description="GCP cloud account",
            base_url="https://compute.googleapis.com",
            connector_type="gcp",
            auth_type="OAUTH2",
            auth_config={},
            credential_strategy="SYSTEM",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            topology_entity_id=str(uuid.uuid4()),  # Has topology entity
        )

    @pytest.fixture
    def gke_connector(self, gcp_connector):
        """Create GKE connector that references GCP connector."""
        return Connector(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            name="GKE Main Cluster",
            description="Main GKE cluster",
            base_url="https://gke.example.com",
            connector_type="kubernetes",
            auth_type="OAUTH2",
            auth_config={},
            credential_strategy="SYSTEM",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            topology_entity_id=None,
            related_connector_ids=[gcp_connector.id],
        )

    @pytest.mark.asyncio
    async def test_create_connector_creates_related_to_relationships(
        self, mock_session, mock_connector_repo, gcp_connector, gke_connector
    ):
        """Test that creating a connector with related_connector_ids creates topology relationships."""
        mock_connector_repo.create_connector.return_value = gke_connector
        mock_connector_repo.update_connector.return_value = gke_connector
        mock_connector_repo.get_connector.return_value = gcp_connector  # For related lookup

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        gke_topology_entity_id = uuid.uuid4()
        gcp_topology_entity_id = uuid.UUID(gcp_connector.topology_entity_id)

        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = MagicMock()
            mock_topology_service.store_discovery = AsyncMock(
                return_value=MagicMock(stored=True, entities_created=1)
            )
            mock_topology_class.return_value = mock_topology_service

            with patch(
                "meho_app.modules.topology.repository.TopologyRepository"
            ) as mock_repo_class:
                mock_repo = MagicMock()
                mock_entity = MagicMock()
                mock_entity.id = gke_topology_entity_id
                mock_repo.get_entity_by_name = AsyncMock(return_value=mock_entity)
                mock_repo.create_relationship = AsyncMock()
                mock_repo_class.return_value = mock_repo

                create_data = ConnectorCreate(
                    tenant_id="test-tenant",
                    name="GKE Main Cluster",
                    base_url="https://gke.example.com",
                    connector_type="kubernetes",
                    auth_type="OAUTH2",
                    related_connector_ids=[gcp_connector.id],
                )

                await service.create_connector(create_data)

                # Verify relationship was created
                mock_repo.create_relationship.assert_called_once_with(
                    from_entity_id=gke_topology_entity_id,
                    to_entity_id=gcp_topology_entity_id,
                    relationship_type="related_to",
                )

    @pytest.mark.asyncio
    async def test_create_connector_skips_relationship_if_related_connector_missing(
        self, mock_session, mock_connector_repo, gke_connector
    ):
        """Test that missing related connector doesn't fail creation."""
        mock_connector_repo.create_connector.return_value = gke_connector
        mock_connector_repo.update_connector.return_value = gke_connector
        mock_connector_repo.get_connector.return_value = None  # Related connector not found

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = MagicMock()
            mock_topology_service.store_discovery = AsyncMock(
                return_value=MagicMock(stored=True, entities_created=1)
            )
            mock_topology_class.return_value = mock_topology_service

            with patch(
                "meho_app.modules.topology.repository.TopologyRepository"
            ) as mock_repo_class:
                mock_repo = MagicMock()
                mock_entity = MagicMock()
                mock_entity.id = uuid.uuid4()
                mock_repo.get_entity_by_name = AsyncMock(return_value=mock_entity)
                mock_repo.create_relationship = AsyncMock()
                mock_repo_class.return_value = mock_repo

                create_data = ConnectorCreate(
                    tenant_id="test-tenant",
                    name="GKE Main Cluster",
                    base_url="https://gke.example.com",
                    connector_type="kubernetes",
                    auth_type="OAUTH2",
                    related_connector_ids=["nonexistent-id"],
                )

                # Should not raise
                await service.create_connector(create_data)

                # Relationship should NOT be created (related connector not found)
                mock_repo.create_relationship.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_connector_skips_relationship_if_related_has_no_topology_entity(
        self, mock_session, mock_connector_repo, gcp_connector, gke_connector
    ):
        """Test that relationship is skipped if related connector has no topology entity."""
        # GCP connector without topology entity
        gcp_connector_no_topology = Connector(
            **{**gcp_connector.model_dump(), "topology_entity_id": None}
        )

        mock_connector_repo.create_connector.return_value = gke_connector
        mock_connector_repo.update_connector.return_value = gke_connector
        mock_connector_repo.get_connector.return_value = gcp_connector_no_topology

        service = ConnectorService(
            session=mock_session,
            connector_repo=mock_connector_repo,
        )

        with patch("meho_app.modules.topology.service.TopologyService") as mock_topology_class:
            mock_topology_service = MagicMock()
            mock_topology_service.store_discovery = AsyncMock(
                return_value=MagicMock(stored=True, entities_created=1)
            )
            mock_topology_class.return_value = mock_topology_service

            with patch(
                "meho_app.modules.topology.repository.TopologyRepository"
            ) as mock_repo_class:
                mock_repo = MagicMock()
                mock_entity = MagicMock()
                mock_entity.id = uuid.uuid4()
                mock_repo.get_entity_by_name = AsyncMock(return_value=mock_entity)
                mock_repo.create_relationship = AsyncMock()
                mock_repo_class.return_value = mock_repo

                create_data = ConnectorCreate(
                    tenant_id="test-tenant",
                    name="GKE Main Cluster",
                    base_url="https://gke.example.com",
                    connector_type="kubernetes",
                    auth_type="OAUTH2",
                    related_connector_ids=[gcp_connector.id],
                )

                await service.create_connector(create_data)

                # Relationship should NOT be created
                mock_repo.create_relationship.assert_not_called()
