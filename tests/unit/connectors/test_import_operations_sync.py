# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for import operations sync (TASK-142 Phase 9).

Tests the sync_operations_for_imported_connector function that
syncs operations after importing connectors.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.import_operations_sync import (
    ImportOperationsSyncResult,
    _sync_gcp_operations,
    _sync_kubernetes_operations,
    _sync_proxmox_operations,
    _sync_rest_operations,
    _sync_soap_operations,
    _sync_vmware_operations,
    sync_operations_for_imported_connector,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session():
    """Create a mock AsyncSession."""
    session = AsyncMock()
    session.commit = AsyncMock()
    return session


# =============================================================================
# ImportOperationsSyncResult Tests
# =============================================================================


class TestImportOperationsSyncResult:
    """Tests for the result dataclass."""

    def test_default_values(self):
        """Test default values are correct."""
        result = ImportOperationsSyncResult()
        assert result.operations_synced == 0
        assert result.types_synced == 0
        assert result.knowledge_chunks_created == 0
        assert result.warning is None
        assert result.success is True

    def test_with_values(self):
        """Test setting values."""
        result = ImportOperationsSyncResult(
            operations_synced=50,
            types_synced=10,
            knowledge_chunks_created=100,
            warning="Test warning",
            success=False,
        )
        assert result.operations_synced == 50
        assert result.types_synced == 10
        assert result.knowledge_chunks_created == 100
        assert result.warning == "Test warning"
        assert result.success is False


# =============================================================================
# Typed Connector Sync Tests (VMware, Proxmox, GCP)
# =============================================================================


class TestVMwareOperationsSync:
    """Tests for VMware operations sync."""

    @pytest.mark.asyncio
    async def test_sync_vmware_operations_success(self, mock_session):
        """Test successful VMware operations sync."""
        with (
            patch(
                "meho_app.modules.connectors.vmware.sync.sync_vmware_operations_if_needed"
            ) as mock_sync,
            patch(
                "meho_app.modules.connectors.import_operations_sync._get_knowledge_store"
            ) as mock_ks,
            patch(
                "meho_app.modules.connectors.repositories.ConnectorTypeRepository"
            ) as mock_type_repo,
        ):
            # Setup mocks
            mock_sync.return_value = (100, 50, 150)  # added, updated, chunks
            mock_ks.return_value = None
            mock_type_repo_instance = MagicMock()
            mock_type_repo_instance.create_types_bulk = AsyncMock(return_value=25)
            mock_type_repo.return_value = mock_type_repo_instance

            result = await _sync_vmware_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test VMware",
            )

            assert result.success is True
            assert result.operations_synced == 150  # 100 + 50
            assert result.types_synced == 25
            assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_vmware_operations_failure(self, mock_session):
        """Test VMware operations sync failure returns warning."""
        with patch(
            "meho_app.modules.connectors.vmware.sync.sync_vmware_operations_if_needed"
        ) as mock_sync:
            mock_sync.side_effect = Exception("Connection failed")

            result = await _sync_vmware_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test VMware",
            )

            assert result.success is False
            assert result.warning is not None
            assert "Connection failed" in result.warning


class TestProxmoxOperationsSync:
    """Tests for Proxmox operations sync."""

    @pytest.mark.asyncio
    async def test_sync_proxmox_operations_success(self, mock_session):
        """Test successful Proxmox operations sync."""
        with (
            patch(
                "meho_app.modules.connectors.proxmox.sync.sync_proxmox_operations_if_needed"
            ) as mock_sync,
            patch(
                "meho_app.modules.connectors.import_operations_sync._get_knowledge_store"
            ) as mock_ks,
            patch(
                "meho_app.modules.connectors.repositories.ConnectorTypeRepository"
            ) as mock_type_repo,
        ):
            mock_sync.return_value = (40, 10, 50)
            mock_ks.return_value = None
            mock_type_repo_instance = MagicMock()
            mock_type_repo_instance.create_types_bulk = AsyncMock(return_value=15)
            mock_type_repo.return_value = mock_type_repo_instance

            result = await _sync_proxmox_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test Proxmox",
            )

            assert result.success is True
            assert result.operations_synced == 50
            assert result.types_synced == 15


class TestGCPOperationsSync:
    """Tests for GCP operations sync."""

    @pytest.mark.asyncio
    async def test_sync_gcp_operations_success(self, mock_session):
        """Test successful GCP operations sync."""
        with (
            patch(
                "meho_app.modules.connectors.gcp.sync.sync_gcp_operations_if_needed"
            ) as mock_sync,
            patch(
                "meho_app.modules.connectors.import_operations_sync._get_knowledge_store"
            ) as mock_ks,
            patch(
                "meho_app.modules.connectors.repositories.ConnectorTypeRepository"
            ) as mock_type_repo,
        ):
            mock_sync.return_value = (60, 20, 80)
            mock_ks.return_value = None
            mock_type_repo_instance = MagicMock()
            mock_type_repo_instance.create_types_bulk = AsyncMock(return_value=12)
            mock_type_repo.return_value = mock_type_repo_instance

            result = await _sync_gcp_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test GCP",
            )

            assert result.success is True
            assert result.operations_synced == 80
            assert result.types_synced == 12


class TestKubernetesOperationsSync:
    """Tests for Kubernetes operations sync."""

    @pytest.mark.asyncio
    async def test_sync_kubernetes_operations_success(self, mock_session):
        """Test successful Kubernetes operations sync."""
        with (
            patch(
                "meho_app.modules.connectors.kubernetes.sync.sync_kubernetes_operations_if_needed"
            ) as mock_sync,
            patch(
                "meho_app.modules.connectors.import_operations_sync._get_knowledge_store"
            ) as mock_ks,
            patch(
                "meho_app.modules.connectors.repositories.ConnectorTypeRepository"
            ) as mock_type_repo,
        ):
            mock_sync.return_value = (60, 20, 80)
            mock_ks.return_value = None
            mock_type_repo_instance = MagicMock()
            mock_type_repo_instance.create_types_bulk = AsyncMock(return_value=18)
            mock_type_repo.return_value = mock_type_repo_instance

            result = await _sync_kubernetes_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test K8s",
            )

            assert result.success is True
            assert result.operations_synced == 80  # 60 + 20
            assert result.types_synced == 18
            assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_kubernetes_operations_failure(self, mock_session):
        """Test Kubernetes operations sync failure returns warning."""
        with patch(
            "meho_app.modules.connectors.kubernetes.sync.sync_kubernetes_operations_if_needed"
        ) as mock_sync:
            mock_sync.side_effect = Exception("Connection failed")

            result = await _sync_kubernetes_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test K8s",
            )

            assert result.success is False
            assert result.warning is not None
            assert "Connection failed" in result.warning


# =============================================================================
# REST Connector Sync Tests
# =============================================================================


class TestRESTOperationsSync:
    """Tests for REST operations sync."""

    @pytest.mark.asyncio
    async def test_sync_rest_without_url_skips_gracefully(self, mock_session):
        """Test REST connector without openapi_url skips gracefully."""
        result = await _sync_rest_operations(
            session=mock_session,
            connector_id="test-id",
            tenant_id="test-tenant",
            connector_name="Test REST",
            protocol_config=None,
        )

        assert result.success is True
        assert result.operations_synced == 0
        assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_rest_with_empty_protocol_config(self, mock_session):
        """Test REST connector with empty protocol_config skips gracefully."""
        result = await _sync_rest_operations(
            session=mock_session,
            connector_id="test-id",
            tenant_id="test-tenant",
            connector_name="Test REST",
            protocol_config={},
        )

        assert result.success is True
        assert result.operations_synced == 0
        assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_rest_success(self, mock_session):
        """Test successful REST operations sync from OpenAPI URL."""
        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("meho_app.modules.connectors.rest.spec_parser.OpenAPIParser") as mock_parser,
            patch(
                "meho_app.modules.connectors.rest.repository.EndpointDescriptorRepository"
            ) as mock_repo,
            patch(
                "meho_app.modules.connectors.import_operations_sync._get_knowledge_store"
            ) as mock_ks,
        ):
            # Setup HTTP mock
            mock_response = MagicMock()
            mock_response.content = b'{"openapi": "3.0.0"}'
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_cls.return_value = mock_client

            # Setup parser mock
            mock_parser_instance = MagicMock()
            mock_parser_instance.parse = AsyncMock(return_value={"openapi": "3.0.0"})
            mock_parser.return_value = mock_parser_instance

            # Setup repo mock
            mock_repo_instance = MagicMock()
            mock_repo_instance.create_from_spec = AsyncMock(
                return_value=[1, 2, 3, 4, 5]
            )  # 5 endpoints
            mock_repo.return_value = mock_repo_instance

            mock_ks.return_value = None

            result = await _sync_rest_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test REST",
                protocol_config={"openapi_url": "https://api.example.com/openapi.json"},
            )

            assert result.success is True
            assert result.operations_synced == 5
            assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_rest_network_error_returns_warning(self, mock_session):
        """Test REST sync with network error returns warning, not failure."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_cls.return_value = mock_client

            result = await _sync_rest_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test REST",
                protocol_config={"openapi_url": "https://api.example.com/openapi.json"},
            )

            # Should not fail, just add warning
            assert result.success is True  # Default, sync continues
            assert result.warning is not None
            # Warning should mention the URL and allow manual upload
            assert "openapi" in result.warning.lower() or "spec" in result.warning.lower()
            assert "manually" in result.warning.lower()


# =============================================================================
# SOAP Connector Sync Tests
# =============================================================================


class TestSOAPOperationsSync:
    """Tests for SOAP operations sync."""

    @pytest.mark.asyncio
    async def test_sync_soap_without_url_skips_gracefully(self, mock_session):
        """Test SOAP connector without wsdl_url skips gracefully."""
        result = await _sync_soap_operations(
            session=mock_session,
            connector_id="test-id",
            tenant_id="test-tenant",
            connector_name="Test SOAP",
            protocol_config=None,
        )

        assert result.success is True
        assert result.operations_synced == 0
        assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_soap_with_empty_protocol_config(self, mock_session):
        """Test SOAP connector with empty protocol_config skips gracefully."""
        result = await _sync_soap_operations(
            session=mock_session,
            connector_id="test-id",
            tenant_id="test-tenant",
            connector_name="Test SOAP",
            protocol_config={},
        )

        assert result.success is True
        assert result.operations_synced == 0
        assert result.warning is None

    @pytest.mark.asyncio
    async def test_sync_soap_network_error_returns_warning(self, mock_session):
        """Test SOAP sync with network error returns warning, not failure."""
        with patch("meho_app.modules.connectors.soap.ingester.SOAPSchemaIngester") as mock_ingester:
            mock_ingester_instance = MagicMock()
            mock_ingester_instance.ingest_wsdl = AsyncMock(
                side_effect=Exception("Failed to parse WSDL")
            )
            mock_ingester.return_value = mock_ingester_instance

            result = await _sync_soap_operations(
                session=mock_session,
                connector_id="test-id",
                tenant_id="test-tenant",
                connector_name="Test SOAP",
                protocol_config={"wsdl_url": "https://api.example.com/service.wsdl"},
            )

            # Should not fail, just add warning
            assert result.success is True  # Default
            assert result.warning is not None
            assert "Could not fetch/parse WSDL" in result.warning


# =============================================================================
# Main Dispatch Function Tests
# =============================================================================


class TestSyncOperationsForImportedConnector:
    """Tests for the main dispatch function."""

    @pytest.mark.asyncio
    async def test_dispatch_to_vmware(self, mock_session):
        """Test dispatch to VMware sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_vmware_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=100)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="vmware",
                tenant_id="test-tenant",
                connector_name="Test VMware",
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 100

    @pytest.mark.asyncio
    async def test_dispatch_to_proxmox(self, mock_session):
        """Test dispatch to Proxmox sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_proxmox_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=50)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="proxmox",
                tenant_id="test-tenant",
                connector_name="Test Proxmox",
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 50

    @pytest.mark.asyncio
    async def test_dispatch_to_gcp(self, mock_session):
        """Test dispatch to GCP sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_gcp_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=75)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="gcp",
                tenant_id="test-tenant",
                connector_name="Test GCP",
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 75

    @pytest.mark.asyncio
    async def test_dispatch_to_rest(self, mock_session):
        """Test dispatch to REST sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_rest_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=25)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="rest",
                tenant_id="test-tenant",
                connector_name="Test REST",
                protocol_config={"openapi_url": "https://example.com/api"},
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 25

    @pytest.mark.asyncio
    async def test_dispatch_to_soap(self, mock_session):
        """Test dispatch to SOAP sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_soap_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=30)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="soap",
                tenant_id="test-tenant",
                connector_name="Test SOAP",
                protocol_config={"wsdl_url": "https://example.com/service.wsdl"},
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 30

    @pytest.mark.asyncio
    async def test_dispatch_to_kubernetes(self, mock_session):
        """Test dispatch to Kubernetes sync."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_kubernetes_operations"
        ) as mock_sync:
            mock_sync.return_value = ImportOperationsSyncResult(operations_synced=80)

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="kubernetes",
                tenant_id="test-tenant",
                connector_name="Test K8s",
            )

            mock_sync.assert_called_once()
            assert result.operations_synced == 80

    @pytest.mark.asyncio
    async def test_graphql_connector_returns_warning(self, mock_session):
        """Test GraphQL connector type returns warning."""
        result = await sync_operations_for_imported_connector(
            session=mock_session,
            connector_id="test-id",
            connector_type="graphql",
            tenant_id="test-tenant",
            connector_name="Test GraphQL",
        )

        assert result.operations_synced == 0
        assert result.warning is not None

    @pytest.mark.asyncio
    async def test_exception_handling(self, mock_session):
        """Test that exceptions are caught and returned as warnings."""
        with patch(
            "meho_app.modules.connectors.import_operations_sync._sync_vmware_operations"
        ) as mock_sync:
            mock_sync.side_effect = Exception("Unexpected error")

            result = await sync_operations_for_imported_connector(
                session=mock_session,
                connector_id="test-id",
                connector_type="vmware",
                tenant_id="test-tenant",
                connector_name="Test VMware",
            )

            assert result.success is False
            assert result.warning is not None
            assert "Unexpected error" in result.warning


# =============================================================================
# Integration with Export Service Tests
# =============================================================================


class TestImportResultSchema:
    """Tests for the updated ImportResult schema."""

    def test_import_result_has_warnings_field(self):
        """Test ImportResult has warnings field."""
        from meho_app.modules.connectors.export_service import ImportResult

        result = ImportResult()
        assert hasattr(result, "warnings")
        assert result.warnings == []

    def test_import_result_has_operations_synced_field(self):
        """Test ImportResult has operations_synced field."""
        from meho_app.modules.connectors.export_service import ImportResult

        result = ImportResult()
        assert hasattr(result, "operations_synced")
        assert result.operations_synced == 0

    def test_import_result_with_values(self):
        """Test ImportResult with all fields populated."""
        from meho_app.modules.connectors.export_service import ImportResult

        result = ImportResult(
            imported=3,
            skipped=1,
            errors=["Error 1"],
            connectors=["Conn1", "Conn2", "Conn3"],
            warnings=["Warning 1", "Warning 2"],
            operations_synced=150,
        )

        assert result.imported == 3
        assert result.skipped == 1
        assert result.errors == ["Error 1"]
        assert result.connectors == ["Conn1", "Conn2", "Conn3"]
        assert result.warnings == ["Warning 1", "Warning 2"]
        assert result.operations_synced == 150
