# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for OpenAPI Service.

Verifies that the OpenAPI service provides the API that consumers expect.
"""

import pytest


class TestConnectorSchemaContract:
    """Test Connector schema contracts"""

    def test_connector_schema_has_required_fields(self):
        """Verify Connector schema has all required fields for BFF"""
        from meho_app.modules.connectors.schemas import Connector

        fields = set(Connector.model_fields.keys())

        required_fields = {"id", "name", "base_url", "auth_type", "auth_config", "created_at"}

        missing = required_fields - fields

        assert not missing, (
            f"Connector schema missing required fields: {missing}. "
            f"BFF connector routes depend on these fields."
        )

    def test_connector_create_schema(self):
        """Verify ConnectorCreate schema for POST requests"""
        from meho_app.modules.connectors.schemas import ConnectorCreate

        fields = set(ConnectorCreate.model_fields.keys())

        required_fields = {"name", "base_url", "tenant_id", "auth_type", "auth_config"}

        missing = required_fields - fields

        assert not missing, f"ConnectorCreate schema missing required fields: {missing}"


class TestEndpointSchemaContract:
    """Test Endpoint descriptor schema contracts"""

    def test_endpoint_descriptor_has_required_fields(self):
        """Verify EndpointDescriptor has fields needed by Agent"""
        from meho_app.modules.connectors.rest.schemas import EndpointDescriptor

        fields = set(EndpointDescriptor.model_fields.keys())

        required_fields = {"id", "connector_id", "path", "method", "summary", "description"}

        missing = required_fields - fields

        assert not missing, f"EndpointDescriptor missing fields needed by Agent: {missing}"


class TestCredentialSchemaContract:
    """Test user credential schema contracts"""

    def test_user_credential_schema_has_required_fields(self):
        """Verify UserCredential schema for credential storage"""
        from meho_app.modules.connectors.schemas import UserCredential

        fields = set(UserCredential.model_fields.keys())

        # Note: encrypted_credentials is NOT exposed in API response (security)
        # Only metadata is returned
        required_fields = {
            "id",
            "user_id",
            "connector_id",
            "credential_type",
            "is_active",
            "created_at",
        }

        missing = required_fields - fields

        assert not missing, f"UserCredential schema missing required fields: {missing}"


class TestOpenAPIServiceMethods:
    """Test that OpenAPI service has expected methods"""

    def test_openapi_repository_has_crud_methods(self):
        """Verify repository has CRUD operations for connectors"""
        from meho_app.modules.connectors import repositories as repository

        # Repository should have connector management functions/classes
        # Architecture may use functions or class methods
        assert hasattr(repository, "create_connector") or hasattr(
            repository, "ConnectorRepository"
        ), "Repository must provide connector CRUD operations"

    def test_spec_parser_exists(self):
        """Verify spec parser functionality is available"""
        # Spec parsing functionality exists in repository
        # This test just verifies the module is importable
        try:
            from meho_app.modules.connectors.rest import spec_parser

            # Module exists - parsing happens in repository layer
            assert spec_parser is not None
        except ImportError:
            pytest.fail("meho_openapi.spec_parser module must be importable")

    def test_endpoint_repository_has_upsert(self):
        """Verify EndpointDescriptorRepository has upsert_endpoint method (Session 77)"""
        from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository

        assert hasattr(EndpointDescriptorRepository, "upsert_endpoint"), (
            "EndpointDescriptorRepository must have upsert_endpoint method "
            "to prevent duplicates when re-uploading specs"
        )

        # Verify the method signature
        import inspect

        sig = inspect.signature(EndpointDescriptorRepository.upsert_endpoint)
        params = list(sig.parameters.keys())

        assert "endpoint" in params, "upsert_endpoint must accept 'endpoint' parameter"


class TestHTTPClientContract:
    """Test GenericHTTPClient API contract"""

    def test_http_client_has_call_endpoint(self):
        """Verify GenericHTTPClient.call_endpoint exists"""
        from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

        assert hasattr(GenericHTTPClient, "call_endpoint"), (
            "GenericHTTPClient must have call_endpoint method for Agent to call APIs"
        )

    def test_http_client_signature(self):
        """Verify call_endpoint has expected parameters"""
        import inspect

        from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

        sig = inspect.signature(GenericHTTPClient.call_endpoint)
        params = list(sig.parameters.keys())

        # Required parameters
        required = ["connector", "endpoint", "path_params"]

        for param in required:
            assert param in params, f"GenericHTTPClient.call_endpoint must have parameter: {param}"
