# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for SOAP data models.

Tests the Pydantic models used for SOAP operations, configuration,
and responses.
"""

from datetime import datetime
from uuid import uuid4

from meho_app.modules.connectors.soap.models import (
    SOAPAuthType,
    SOAPCallParams,
    SOAPConnectorConfig,
    SOAPOperation,
    SOAPParameter,
    SOAPResponse,
    SOAPStyle,
    WSDLMetadata,
)


class TestSOAPAuthType:
    """Tests for SOAPAuthType enum"""

    def test_auth_types_exist(self):
        """All auth types should be defined"""
        assert SOAPAuthType.NONE.value == "none"
        assert SOAPAuthType.BASIC.value == "basic"
        assert SOAPAuthType.SESSION.value == "session"
        assert SOAPAuthType.WS_SECURITY.value == "ws_security"
        assert SOAPAuthType.CERTIFICATE.value == "certificate"


class TestSOAPStyle:
    """Tests for SOAPStyle enum"""

    def test_styles_exist(self):
        """SOAP binding styles should be defined"""
        assert SOAPStyle.DOCUMENT.value == "document"
        assert SOAPStyle.RPC.value == "rpc"


class TestSOAPConnectorConfig:
    """Tests for SOAPConnectorConfig model"""

    def test_minimal_config(self):
        """Config with only required fields"""
        config = SOAPConnectorConfig(wsdl_url="https://example.com/service.wsdl")

        assert config.wsdl_url == "https://example.com/service.wsdl"
        assert config.auth_type == SOAPAuthType.NONE
        assert config.timeout == 30
        assert config.verify_ssl is True

    def test_basic_auth_config(self):
        """Config with basic auth"""
        config = SOAPConnectorConfig(
            wsdl_url="https://example.com/service.wsdl",
            auth_type=SOAPAuthType.BASIC,
            username="user",
            password="pass",
        )

        assert config.auth_type == SOAPAuthType.BASIC
        assert config.username == "user"
        assert config.password == "pass"

    def test_session_auth_config(self):
        """Config with session-based auth (VMware style)"""
        config = SOAPConnectorConfig(
            wsdl_url="https://vcenter.local/sdk/vimService.wsdl",
            auth_type=SOAPAuthType.SESSION,
            username="admin@vsphere.local",
            password="***",
            login_operation="SessionManager.Login",
            logout_operation="SessionManager.Logout",
        )

        assert config.auth_type == SOAPAuthType.SESSION
        assert config.login_operation == "SessionManager.Login"
        assert config.logout_operation == "SessionManager.Logout"

    def test_ws_security_config(self):
        """Config with WS-Security"""
        config = SOAPConnectorConfig(
            wsdl_url="https://example.com/service.wsdl",
            auth_type=SOAPAuthType.WS_SECURITY,
            ws_security_username="user",
            ws_security_password="pass",
            ws_security_use_digest=True,
        )

        assert config.auth_type == SOAPAuthType.WS_SECURITY
        assert config.ws_security_use_digest is True

    def test_ws_security_advanced_config(self):
        """Config with advanced WS-Security options (timestamp, nonce)"""
        config = SOAPConnectorConfig(
            wsdl_url="https://example.com/service.wsdl",
            auth_type=SOAPAuthType.WS_SECURITY,
            ws_security_username="user",
            ws_security_password="pass",
            ws_security_use_digest=True,
            ws_security_use_timestamp=True,
            ws_security_timestamp_ttl=600,  # 10 minutes
            ws_security_use_nonce=True,
        )

        assert config.ws_security_use_timestamp is True
        assert config.ws_security_timestamp_ttl == 600
        assert config.ws_security_use_nonce is True

    def test_ws_security_defaults(self):
        """WS-Security defaults should be secure"""
        config = SOAPConnectorConfig(
            wsdl_url="https://example.com/service.wsdl",
            auth_type=SOAPAuthType.WS_SECURITY,
            ws_security_username="user",
            ws_security_password="pass",
        )

        # Defaults should enable timestamp and nonce (secure by default)
        assert config.ws_security_use_timestamp is True
        assert config.ws_security_timestamp_ttl == 300  # 5 min default
        assert config.ws_security_use_nonce is True
        assert config.ws_security_use_digest is False  # Plain password by default


class TestSOAPOperation:
    """Tests for SOAPOperation model"""

    def test_create_operation(self):
        """Create a SOAP operation"""
        connector_id = uuid4()

        operation = SOAPOperation(
            connector_id=connector_id,
            tenant_id="test-tenant",
            service_name="VimService",
            port_name="VimPort",
            operation_name="RetrieveProperties",
            name="VimService.RetrieveProperties",
            description="Retrieves properties from managed objects",
            soap_action="urn:vim25/RetrieveProperties",
            style=SOAPStyle.DOCUMENT,
            namespace="urn:vim25",
            input_schema={"type": "object", "properties": {"specSet": {"type": "array"}}},
            output_schema={"type": "object", "properties": {"returnval": {"type": "array"}}},
            search_content="SOAP operation RetrieveProperties service VimService",
        )

        assert operation.connector_id == connector_id
        assert operation.service_name == "VimService"
        assert operation.operation_name == "RetrieveProperties"
        assert operation.name == "VimService.RetrieveProperties"
        assert operation.style == SOAPStyle.DOCUMENT
        assert operation.is_enabled is True

    def test_to_protocol_details(self):
        """Operation should convert to protocol_details format"""
        operation = SOAPOperation(
            connector_id=uuid4(),
            tenant_id="test-tenant",
            service_name="TestService",
            port_name="TestPort",
            operation_name="TestOp",
            name="TestService.TestOp",
            soap_action="urn:test",
            style=SOAPStyle.RPC,
            namespace="urn:test",
            protocol_details={"wsdl_url": "https://example.com/test.wsdl"},
        )

        details = operation.to_protocol_details()

        assert details["protocol"] == "soap"
        assert details["service"] == "TestService"
        assert details["port"] == "TestPort"
        assert details["operation"] == "TestOp"
        assert details["soap_action"] == "urn:test"
        assert details["style"] == "rpc"


class TestSOAPResponse:
    """Tests for SOAPResponse model"""

    def test_successful_response(self):
        """Create a successful response"""
        response = SOAPResponse(
            success=True,
            status_code=200,
            body={"result": "success", "data": [1, 2, 3]},
            operation_name="TestOp",
            duration_ms=150.5,
        )

        assert response.success is True
        assert response.status_code == 200
        assert response.body["result"] == "success"
        assert response.fault_code is None

    def test_fault_response(self):
        """Create a SOAP fault response"""
        response = SOAPResponse(
            success=False,
            status_code=500,
            body={"error": "SOAP fault"},
            fault_code="soap:Server",
            fault_string="Internal server error",
            fault_detail="Stack trace...",
            operation_name="TestOp",
            duration_ms=50.0,
        )

        assert response.success is False
        assert response.status_code == 500
        assert response.fault_code == "soap:Server"
        assert response.fault_string == "Internal server error"


class TestSOAPCallParams:
    """Tests for SOAPCallParams model"""

    def test_minimal_params(self):
        """Create minimal call params"""
        params = SOAPCallParams(
            operation_name="GetUser",
            params={"userId": "123"},
        )

        assert params.operation_name == "GetUser"
        assert params.params["userId"] == "123"
        assert params.service_name is None
        assert params.port_name is None

    def test_full_params(self):
        """Create call params with all options"""
        params = SOAPCallParams(
            operation_name="GetUser",
            params={"userId": "123"},
            service_name="UserService",
            port_name="UserPort",
            timeout=60,
        )

        assert params.service_name == "UserService"
        assert params.port_name == "UserPort"
        assert params.timeout == 60


class TestWSDLMetadata:
    """Tests for WSDLMetadata model"""

    def test_create_metadata(self):
        """Create WSDL metadata"""
        metadata = WSDLMetadata(
            wsdl_url="https://example.com/service.wsdl",
            target_namespace="urn:example",
            services=["Service1", "Service2"],
            ports=["Port1", "Port2"],
            operation_count=50,
        )

        assert metadata.wsdl_url == "https://example.com/service.wsdl"
        assert metadata.target_namespace == "urn:example"
        assert len(metadata.services) == 2
        assert len(metadata.ports) == 2
        assert metadata.operation_count == 50
        assert isinstance(metadata.parsed_at, datetime)


class TestSOAPParameter:
    """Tests for SOAPParameter model"""

    def test_simple_parameter(self):
        """Create a simple parameter"""
        param = SOAPParameter(
            name="userId",
            type="string",
            json_type="string",
            required=True,
            description="User identifier",
        )

        assert param.name == "userId"
        assert param.type == "string"
        assert param.json_type == "string"
        assert param.required is True

    def test_complex_parameter(self):
        """Create a complex parameter with nested properties"""
        param = SOAPParameter(
            name="address",
            type="AddressType",
            json_type="object",
            required=False,
            properties={
                "street": {"type": "string"},
                "city": {"type": "string"},
                "zip": {"type": "string"},
            },
        )

        assert param.name == "address"
        assert param.json_type == "object"
        assert param.required is False
        assert "street" in param.properties
