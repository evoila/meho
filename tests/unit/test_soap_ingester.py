# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for SOAP Schema Ingester.

Tests WSDL parsing, operation discovery, and schema conversion.
"""

from unittest.mock import Mock
from uuid import uuid4

import pytest

from meho_app.modules.connectors.soap.ingester import SOAPSchemaIngester
from meho_app.modules.connectors.soap.models import SOAPStyle


class TestSOAPSchemaIngester:
    """Tests for SOAPSchemaIngester class"""

    @pytest.fixture
    def ingester(self):
        """Create an ingester instance"""
        return SOAPSchemaIngester()

    @pytest.fixture
    def mock_wsdl_operation(self):
        """Create a mock WSDL operation"""
        operation = Mock()
        operation.documentation = "Test operation documentation"
        operation.soapaction = "urn:test/TestOp"

        # Input
        input_body = Mock()
        input_body.namespace = "urn:test"
        input_body.type = Mock()
        input_body.type.elements = []
        operation.input = Mock()
        operation.input.body = input_body

        # Output
        output_body = Mock()
        output_body.type = Mock()
        output_body.type.elements = []
        operation.output = Mock()
        operation.output.body = output_body

        return operation

    def test_xsd_to_json_type_strings(self, ingester):
        """XSD string types should map to JSON string"""
        string_types = [
            "string",
            "normalizedString",
            "token",
            "language",
            "Name",
            "NCName",
            "ID",
            "IDREF",
            "NMTOKEN",
            "anyURI",
            "QName",
        ]

        for xsd_type in string_types:
            assert ingester._xsd_to_json_type(xsd_type) == "string", (
                f"{xsd_type} should map to string"
            )

    def test_xsd_to_json_type_integers(self, ingester):
        """XSD integer types should map to JSON integer"""
        int_types = [
            "int",
            "integer",
            "long",
            "short",
            "byte",
            "unsignedInt",
            "unsignedLong",
            "unsignedShort",
            "unsignedByte",
            "positiveInteger",
            "negativeInteger",
            "nonPositiveInteger",
            "nonNegativeInteger",
        ]

        for xsd_type in int_types:
            assert ingester._xsd_to_json_type(xsd_type) == "integer", (
                f"{xsd_type} should map to integer"
            )

    def test_xsd_to_json_type_numbers(self, ingester):
        """XSD float types should map to JSON number"""
        float_types = ["float", "double", "decimal"]

        for xsd_type in float_types:
            assert ingester._xsd_to_json_type(xsd_type) == "number", (
                f"{xsd_type} should map to number"
            )

    def test_xsd_to_json_type_boolean(self, ingester):
        """XSD boolean should map to JSON boolean"""
        assert ingester._xsd_to_json_type("boolean") == "boolean"

    def test_xsd_to_json_type_datetime(self, ingester):
        """XSD datetime types should map to JSON string"""
        datetime_types = [
            "dateTime",
            "date",
            "time",
            "duration",
            "gYearMonth",
            "gYear",
            "gMonthDay",
            "gDay",
            "gMonth",
        ]

        for xsd_type in datetime_types:
            assert ingester._xsd_to_json_type(xsd_type) == "string", (
                f"{xsd_type} should map to string"
            )

    def test_xsd_to_json_type_binary(self, ingester):
        """XSD binary types should map to JSON string"""
        assert ingester._xsd_to_json_type("base64Binary") == "string"
        assert ingester._xsd_to_json_type("hexBinary") == "string"

    def test_xsd_to_json_type_unknown(self, ingester):
        """Unknown XSD types should default to string"""
        assert ingester._xsd_to_json_type("unknownType") == "string"
        assert ingester._xsd_to_json_type("CustomType") == "string"

    def test_xsd_to_json_type_qualified_name(self, ingester):
        """Qualified XSD type names should be handled"""
        assert ingester._xsd_to_json_type("xsd:string") == "string"
        assert ingester._xsd_to_json_type("ns:integer") == "integer"
        assert ingester._xsd_to_json_type("vim25:boolean") == "boolean"

    def test_build_search_content(self, ingester):
        """Search content should include all relevant info"""
        content = ingester._build_search_content(
            service_name="UserService",
            port_name="UserPort",
            operation_name="GetUser",
            description="Get user by ID",
            input_schema={
                "type": "object",
                "properties": {
                    "userId": {"type": "string"},
                    "includeProfile": {"type": "boolean"},
                },
            },
        )

        assert "GetUser" in content
        assert "UserService" in content
        assert "Get user by ID" in content
        assert "userId" in content
        assert "includeProfile" in content

    def test_build_search_content_no_description(self, ingester):
        """Search content should work without description"""
        content = ingester._build_search_content(
            service_name="TestService",
            port_name="TestPort",
            operation_name="TestOp",
            description=None,
            input_schema={},
        )

        assert "TestOp" in content
        assert "TestService" in content

    def test_element_to_schema_empty(self, ingester):
        """Empty element should return empty schema"""
        schema = ingester._element_to_schema(None)
        assert schema == {}

    def test_parse_operation(self, ingester, mock_wsdl_operation):
        """Parse a single WSDL operation"""
        mock_binding = Mock()
        mock_binding.style = "document"

        connector_id = uuid4()

        operation = ingester._parse_operation(
            wsdl_url="https://example.com/service.wsdl",
            service_name="TestService",
            port_name="TestPort",
            operation_name="TestOp",
            operation=mock_wsdl_operation,
            binding=mock_binding,
            connector_id=connector_id,
            tenant_id="test-tenant",
        )

        assert operation.service_name == "TestService"
        assert operation.port_name == "TestPort"
        assert operation.operation_name == "TestOp"
        assert operation.name == "TestService.TestOp"
        assert operation.connector_id == connector_id
        assert operation.tenant_id == "test-tenant"
        assert operation.soap_action == "urn:test/TestOp"
        assert operation.style == SOAPStyle.DOCUMENT


class TestSOAPSchemaIngesterIntegration:
    """Integration tests that require zeep (marked to skip if not available)"""

    @pytest.fixture
    def ingester(self):
        return SOAPSchemaIngester()
