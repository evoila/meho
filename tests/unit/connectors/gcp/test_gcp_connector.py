# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for GCP Connector (TASK-102)

Tests connector initialization, authentication, and basic operations.
Uses mocked GCP client libraries.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from meho_app.modules.connectors.gcp.connector import GCPConnector
from meho_app.modules.connectors.gcp.operations import GCP_OPERATIONS
from meho_app.modules.connectors.gcp.types import GCP_TYPES


class TestGCPConnectorInit:
    """Test connector initialization."""

    def test_init_with_config(self):
        """Test connector initializes with config."""
        connector = GCPConnector(
            connector_id="test-123",
            config={
                "project_id": "my-project",
                "default_region": "us-central1",
                "default_zone": "us-central1-a",
            },
            credentials={"service_account_json": '{"type": "service_account"}'},
        )

        assert connector.connector_id == "test-123"
        assert connector.project_id == "my-project"
        assert connector.default_region == "us-central1"
        assert connector.default_zone == "us-central1-a"
        assert not connector.is_connected

    def test_init_with_defaults(self):
        """Test connector uses default region/zone if not specified."""
        connector = GCPConnector(
            connector_id="test-123", config={"project_id": "my-project"}, credentials={}
        )

        assert connector.default_region == "us-central1"
        assert connector.default_zone == "us-central1-a"


class TestGCPConnectorAuth:
    """Test authentication handling."""

    def test_get_credentials_from_json(self):
        """Test credential extraction from JSON string."""
        sa_info = {
            "type": "service_account",
            "project_id": "my-project",
            "private_key_id": "key123",
            "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            "client_email": "test@my-project.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }

        connector = GCPConnector(
            connector_id="test-123",
            config={"project_id": "my-project"},
            credentials={"service_account_json": json.dumps(sa_info)},
        )

        with patch(
            "google.oauth2.service_account.Credentials.from_service_account_info"
        ) as mock_creds:
            mock_creds.return_value = MagicMock()
            connector._get_credentials()
            mock_creds.assert_called_once_with(sa_info)

    def test_get_credentials_from_base64(self):
        """Test credential extraction from base64-encoded JSON."""
        sa_info = {"type": "service_account", "project_id": "my-project"}
        sa_json = json.dumps(sa_info)
        sa_b64 = base64.b64encode(sa_json.encode()).decode()

        connector = GCPConnector(
            connector_id="test-123",
            config={"project_id": "my-project"},
            credentials={"service_account_json_base64": sa_b64},
        )

        with patch(
            "google.oauth2.service_account.Credentials.from_service_account_info"
        ) as mock_creds:
            mock_creds.return_value = MagicMock()
            connector._get_credentials()
            mock_creds.assert_called_once_with(sa_info)

    def test_get_credentials_falls_back_to_adc(self):
        """Test fallback to Application Default Credentials."""
        connector = GCPConnector(
            connector_id="test-123",
            config={"project_id": "my-project"},
            credentials={},  # No explicit credentials
        )

        with patch("google.auth.default") as mock_default:
            mock_default.return_value = (MagicMock(), "my-project")
            connector._get_credentials()
            mock_default.assert_called_once()


class TestGCPConnectorOperations:
    """Test operation definitions."""

    def test_operations_defined(self):
        """Test that operations are properly defined."""
        assert len(GCP_OPERATIONS) > 0

        # Check for key operations
        op_ids = {op.operation_id for op in GCP_OPERATIONS}
        assert "list_instances" in op_ids
        assert "get_instance" in op_ids
        assert "start_instance" in op_ids
        assert "stop_instance" in op_ids
        assert "list_clusters" in op_ids
        assert "list_networks" in op_ids
        assert "get_time_series" in op_ids

    def test_operation_categories(self):
        """Test that operations have proper categories."""
        categories = {op.category for op in GCP_OPERATIONS}

        assert "compute" in categories
        assert "storage" in categories
        assert "containers" in categories
        assert "networking" in categories
        assert "monitoring" in categories

    def test_operations_have_descriptions(self):
        """Test that all operations have descriptions."""
        for op in GCP_OPERATIONS:
            assert op.description, f"Operation {op.operation_id} missing description"
            assert len(op.description) > 10, (
                f"Operation {op.operation_id} has too short description"
            )


class TestGCPConnectorTypes:
    """Test type definitions."""

    def test_types_defined(self):
        """Test that types are properly defined."""
        assert len(GCP_TYPES) > 0

        type_names = {t.type_name for t in GCP_TYPES}
        assert "Instance" in type_names
        assert "Disk" in type_names
        assert "GKECluster" in type_names
        assert "VPCNetwork" in type_names

    def test_types_have_properties(self):
        """Test that types have properties defined."""
        for t in GCP_TYPES:
            assert t.properties, f"Type {t.type_name} has no properties"
            assert len(t.properties) > 0


class TestGCPConnectorInterface:
    """Test BaseConnector interface implementation."""

    def test_get_operations(self):
        """Test get_operations returns operation list."""
        connector = GCPConnector(
            connector_id="test-123", config={"project_id": "my-project"}, credentials={}
        )

        ops = connector.get_operations()
        assert ops == GCP_OPERATIONS

    def test_get_types(self):
        """Test get_types returns type list."""
        connector = GCPConnector(
            connector_id="test-123", config={"project_id": "my-project"}, credentials={}
        )

        types = connector.get_types()
        assert types == GCP_TYPES

    @pytest.mark.asyncio
    async def test_execute_without_connection(self):
        """Test execute fails gracefully when not connected."""
        connector = GCPConnector(
            connector_id="test-123", config={"project_id": "my-project"}, credentials={}
        )

        result = await connector.execute("list_instances", {})

        assert not result.success
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(self):
        """Test execute handles unknown operations."""
        connector = GCPConnector(
            connector_id="test-123", config={"project_id": "my-project"}, credentials={}
        )
        connector._is_connected = True  # Simulate connection

        result = await connector.execute("unknown_operation", {})

        assert not result.success
        assert "Unknown operation" in result.error
