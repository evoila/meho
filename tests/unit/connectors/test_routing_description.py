# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for routing_description field on connectors.

Tests that the routing_description field is properly:
- Defined in the ConnectorModel
- Included in ConnectorCreate, Connector, and ConnectorUpdate schemas
- Serializable to/from the database
"""

from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
)


class TestConnectorCreateSchema:
    """Tests for ConnectorCreate schema with routing_description."""

    def test_create_with_routing_description(self):
        """Test creating a connector with routing_description."""
        connector = ConnectorCreate(
            tenant_id="tenant-123",
            name="Production K8s",
            base_url="https://k8s.example.com",
            auth_type="API_KEY",
            routing_description="Production Kubernetes cluster hosting api.example.com",
        )

        assert (
            connector.routing_description == "Production Kubernetes cluster hosting api.example.com"
        )

    def test_create_without_routing_description(self):
        """Test creating a connector without routing_description (optional)."""
        connector = ConnectorCreate(
            tenant_id="tenant-123",
            name="Test Connector",
            base_url="https://api.example.com",
            auth_type="NONE",
        )

        assert connector.routing_description is None

    def test_create_with_empty_routing_description(self):
        """Test creating a connector with empty string routing_description."""
        connector = ConnectorCreate(
            tenant_id="tenant-123",
            name="Test Connector",
            base_url="https://api.example.com",
            auth_type="NONE",
            routing_description="",
        )

        assert connector.routing_description == ""

    def test_routing_description_is_str(self):
        """Test that routing_description accepts string values."""
        long_description = (
            "Kubernetes cluster 'prod-k8s' hosting production workloads for "
            "example.com and api.example.com. Contains web services, APIs, "
            "and background workers. Connected to GCP project 'example-prod'."
        )

        connector = ConnectorCreate(
            tenant_id="tenant-123",
            name="Prod K8s",
            base_url="https://k8s.example.com",
            auth_type="API_KEY",
            routing_description=long_description,
        )

        assert connector.routing_description == long_description


class TestConnectorUpdateSchema:
    """Tests for ConnectorUpdate schema with routing_description."""

    def test_update_routing_description(self):
        """Test updating routing_description."""
        update = ConnectorUpdate(
            routing_description="Updated description for LLM routing",
        )

        assert update.routing_description == "Updated description for LLM routing"

    def test_update_without_routing_description(self):
        """Test update without routing_description (optional)."""
        update = ConnectorUpdate(
            name="New Name",
        )

        assert update.routing_description is None

    def test_update_routing_description_to_empty(self):
        """Test updating routing_description to empty string."""
        update = ConnectorUpdate(
            routing_description="",
        )

        assert update.routing_description == ""

    def test_update_multiple_fields_including_routing_description(self):
        """Test updating multiple fields including routing_description."""
        update = ConnectorUpdate(
            name="Updated Name",
            description="Updated description",
            routing_description="New routing description",
            is_active=False,
        )

        assert update.name == "Updated Name"
        assert update.routing_description == "New routing description"
        assert update.is_active is False


class TestConnectorSchema:
    """Tests for Connector response schema with routing_description."""

    def test_connector_inherits_routing_description(self):
        """Test that Connector schema includes routing_description from ConnectorCreate."""
        from datetime import UTC, datetime

        # Connector inherits from ConnectorCreate, so it should have routing_description
        connector = Connector(
            id="conn-123",
            tenant_id="tenant-123",
            name="Test Connector",
            base_url="https://api.example.com",
            auth_type="NONE",
            routing_description="Test routing description",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        assert connector.routing_description == "Test routing description"

    def test_connector_model_dump_includes_routing_description(self):
        """Test that model_dump includes routing_description."""
        from datetime import UTC, datetime

        connector = Connector(
            id="conn-456",
            tenant_id="tenant-123",
            name="Test",
            base_url="https://example.com",
            auth_type="API_KEY",
            routing_description="Description for routing",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        data = connector.model_dump()

        assert "routing_description" in data
        assert data["routing_description"] == "Description for routing"


class TestConnectorModelField:
    """Tests for routing_description column in ConnectorModel."""

    def test_model_has_routing_description_column(self):
        """Test that ConnectorModel has routing_description column defined."""
        from meho_app.modules.connectors.models import ConnectorModel

        # Check that the column exists in the mapper
        columns = {c.name for c in ConnectorModel.__table__.columns}
        assert "routing_description" in columns

    def test_routing_description_column_is_nullable(self):
        """Test that routing_description column is nullable."""
        from meho_app.modules.connectors.models import ConnectorModel

        column = ConnectorModel.__table__.columns["routing_description"]
        assert column.nullable is True

    def test_routing_description_column_is_text_type(self):
        """Test that routing_description column is Text type."""
        from sqlalchemy import Text

        from meho_app.modules.connectors.models import ConnectorModel

        column = ConnectorModel.__table__.columns["routing_description"]
        assert isinstance(column.type, Text)


class TestRoutingDescriptionExamples:
    """Test that various routing description formats work correctly."""

    def test_kubernetes_routing_description(self):
        """Test Kubernetes-style routing description."""
        desc = (
            "Kubernetes cluster 'prod-k8s' hosting production workloads for "
            "example.com and api.example.com. Contains web services, APIs, "
            "and background workers."
        )

        connector = ConnectorCreate(
            tenant_id="t1",
            name="K8s Prod",
            base_url="https://k8s.example.com",
            auth_type="API_KEY",
            routing_description=desc,
        )

        assert "Kubernetes cluster" in connector.routing_description
        assert "api.example.com" in connector.routing_description

    def test_gcp_routing_description(self):
        """Test GCP-style routing description."""
        desc = (
            "GCP project 'example-prod' with Compute Engine VMs, Cloud SQL "
            "databases, and VPC networking for production infrastructure."
        )

        connector = ConnectorCreate(
            tenant_id="t1",
            name="GCP Prod",
            base_url="https://compute.googleapis.com",
            auth_type="OAUTH2",
            routing_description=desc,
        )

        assert "GCP project" in connector.routing_description
        assert "Compute Engine" in connector.routing_description

    def test_rest_api_routing_description(self):
        """Test REST API-style routing description."""
        desc = (
            "Internal billing REST API at billing.internal. Manages customer "
            "invoices, subscriptions, and payment processing."
        )

        connector = ConnectorCreate(
            tenant_id="t1",
            name="Billing API",
            base_url="https://billing.internal",
            auth_type="API_KEY",
            routing_description=desc,
        )

        assert "billing" in connector.routing_description.lower()
        assert "invoices" in connector.routing_description
