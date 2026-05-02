# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for BFF ↔ OpenAPI Service connector schema compatibility.

Prevents schema mismatch bugs when BFF proxies connector requests.
"""

from datetime import UTC, datetime

import pytest


class TestConnectorSchemaCompatibility:
    """Test connector schema compatibility between BFF and OpenAPI service"""

    def test_openapi_connector_response_has_all_bff_required_fields(self):
        """
        Verify OpenAPI service Connector includes all fields needed by BFF.

        Prevents: BFF trying to access missing fields → KeyError → 500 error
        """
        from meho_app.modules.connectors.schemas import Connector as OpenAPIConnector

        # BFF expects these fields when listing connectors
        required_fields = {"id", "name", "base_url", "auth_type", "auth_config", "created_at"}

        openapi_fields = set(OpenAPIConnector.model_fields.keys())

        missing = required_fields - openapi_fields

        assert not missing, (
            f"OpenAPI Connector schema missing fields that BFF expects: {missing}. "
            f"Add these fields to meho_openapi/schemas.py:Connector"
        )

    def test_endpoint_descriptor_completeness(self):
        """
        Verify EndpointDescriptor has all fields needed by Agent and BFF.

        Agent uses endpoints to make API calls via the generic HTTP client.
        """
        from meho_app.modules.connectors.rest.schemas import EndpointDescriptor

        fields = set(EndpointDescriptor.model_fields.keys())

        # Core fields needed for Agent to execute API calls
        required_fields = {"id", "connector_id", "path", "method", "summary", "description"}

        missing = required_fields - fields

        assert not missing, f"EndpointDescriptor missing fields needed by Agent: {missing}"

    def test_user_credential_serializable(self):
        """
        Test that UserCredential can be returned via API.

        Credentials should be serializable without exposing encrypted data.
        """
        from uuid import uuid4

        from meho_app.modules.connectors.schemas import UserCredential

        # Create credential (matches actual schema)
        cred = UserCredential(
            id=str(uuid4()),
            user_id="test-user",
            connector_id=str(uuid4()),
            credential_type="API_KEY",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_used_at=None,
        )

        # Should be serializable to JSON
        try:
            json_data = cred.model_dump(mode="json")
            assert isinstance(json_data["id"], str)
            assert isinstance(json_data["credential_type"], str)
            # Note: encrypted_credentials intentionally NOT exposed in response schema
        except Exception as e:
            pytest.fail(f"UserCredential not serializable to JSON: {e}")


class TestBFFConnectorRouteContract:
    """Test BFF connector routes have proper response schemas"""

    def test_list_connectors_response_schema_exists(self):
        """Verify BFF has response schema for list connectors"""
        # Check that connectors package has proper operations
        import inspect

        from meho_app.api.connectors.operations import crud

        # Find list_connectors function in CRUD operations
        members = inspect.getmembers(crud, inspect.isfunction)
        function_names = [name for name, _ in members]

        # Should have some connector listing endpoint
        has_connector_endpoint = any("connector" in name.lower() for name in function_names)

        assert has_connector_endpoint, (
            "BFF connectors package should have connector listing functions"
        )

    def test_test_connection_endpoint_exists(self):
        """
        Verify test connection endpoint exists.

        Users need to test connector credentials before saving.
        """
        import inspect

        from meho_app.api.connectors.operations import testing

        # Check testing operations module
        members = inspect.getmembers(testing, inspect.isfunction)
        function_names = [name for name, _ in members]

        # Should have test connection endpoint
        has_test_endpoint = any("test" in name.lower() for name in function_names)

        assert has_test_endpoint, (
            "BFF should have test_connection endpoint for validating credentials"
        )
