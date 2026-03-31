# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for SOAP/WSDL support.

Tests the full SOAP workflow including:
- WSDL ingestion
- Operation discovery
- SOAP client execution
- Protocol router integration

Uses a mock SOAP service for testing without network dependencies.
"""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from meho_app.modules.connectors.router import ProtocolRouter
from meho_app.modules.connectors.soap.client import SOAPClient, VMwareSOAPClient
from meho_app.modules.connectors.soap.ingester import SOAPSchemaIngester
from meho_app.modules.connectors.soap.models import (
    SOAPAuthType,
    SOAPConnectorConfig,
    SOAPOperation,
    SOAPStyle,
)


class MockZeepService:
    """Mock zeep service for testing"""

    def __init__(self, operations: dict):
        self._operations = operations

    def __getattr__(self, name: str):
        if name in self._operations:
            return self._operations[name]
        raise AttributeError(f"No operation '{name}'")


class MockZeepClient:
    """Mock zeep Client for testing"""

    def __init__(self, wsdl_url: str):
        self.wsdl_url = wsdl_url
        self.wsdl = MockWSDL()

        # Mock service with operations
        self.service = MockZeepService(
            {
                "Add": self._mock_add,
                "Subtract": self._mock_subtract,
                "GetUser": self._mock_get_user,
                "RetrieveProperties": self._mock_retrieve_properties,
            }
        )

    def _mock_add(self, intA: int, intB: int) -> int:
        return intA + intB

    def _mock_subtract(self, intA: int, intB: int) -> int:
        return intA - intB

    def _mock_get_user(self, userId: str) -> dict:
        return {"id": userId, "name": f"User-{userId}", "email": f"user{userId}@test.com"}

    def _mock_retrieve_properties(self, specSet: list) -> list:
        # Simulate VMware VIM API response
        return [
            {"obj": "vm-1", "propSet": [{"name": "name", "val": "VM-1"}]},
            {"obj": "vm-2", "propSet": [{"name": "name", "val": "VM-2"}]},
        ]

    def bind(self, service_name: str, port_name: str):
        return self.service


class MockWSDL:
    """Mock WSDL document for testing"""

    def __init__(self):
        self.services = {
            "CalculatorService": MockService(
                "CalculatorService",
                [
                    ("Add", "Add two numbers"),
                    ("Subtract", "Subtract two numbers"),
                ],
            ),
            "UserService": MockService(
                "UserService",
                [
                    ("GetUser", "Get user by ID"),
                ],
            ),
        }
        self.types = MockTypes()


class MockTypes:
    """Mock WSDL types"""

    prefix_map = {"tns": "http://test.example.com/"}  # noqa: RUF012 -- mutable default is intentional test state


class MockService:
    """Mock WSDL service"""

    def __init__(self, name: str, operations: list):
        self.name = name
        self.ports = {f"{name}Port": MockPort(name, operations)}


class MockPort:
    """Mock WSDL port"""

    def __init__(self, service_name: str, operations: list):
        self.name = f"{service_name}Port"
        self.binding = MockBinding(operations)


class MockBinding:
    """Mock WSDL binding"""

    def __init__(self, operations: list):
        self.style = "document"
        self._operations = {name: MockOperation(name, desc) for name, desc in operations}


class MockOperation:
    """Mock WSDL operation"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.documentation = description
        self.soapaction = f"http://test.example.com/{name}"
        self.input = MockMessage()
        self.output = MockMessage()


class MockMessage:
    """Mock WSDL message"""

    def __init__(self):
        self.body = MockBody()


class MockBody:
    """Mock WSDL body"""

    def __init__(self):
        self.namespace = "http://test.example.com/"
        self.type = MockElementType()


class MockElementType:
    """Mock XSD type"""

    elements = []  # noqa: RUF012 -- mutable default is intentional test state


class TestSOAPIngesterIntegration:
    """Integration tests for SOAP schema ingester"""

    @pytest.fixture
    def ingester(self):
        return SOAPSchemaIngester()

    @pytest.fixture
    def connector_id(self):
        return uuid4()

    @patch("meho_openapi.soap.ingester.Client")
    async def test_ingest_wsdl_discovers_operations(
        self, mock_client_class, ingester, connector_id
    ):
        """WSDL ingestion should discover all operations"""
        # Setup mock
        mock_client = MockZeepClient("http://test.example.com/service.wsdl")
        mock_client_class.return_value = mock_client

        # Ingest - TASK-96: now returns 3 values
        operations, metadata, type_definitions = await ingester.ingest_wsdl(
            wsdl_url="http://test.example.com/service.wsdl",
            connector_id=connector_id,
            tenant_id="test-tenant",
        )

        # Verify operations discovered
        assert len(operations) == 3  # Add, Subtract, GetUser

        # Verify metadata
        assert len(metadata.services) == 2
        assert "CalculatorService" in metadata.services
        assert "UserService" in metadata.services

        # Type definitions may be empty for mock (no complex types defined)
        assert isinstance(type_definitions, list)

    @patch("meho_openapi.soap.ingester.Client")
    async def test_operation_details_extracted(self, mock_client_class, ingester, connector_id):
        """Operation details should be correctly extracted"""
        mock_client = MockZeepClient("http://test.example.com/service.wsdl")
        mock_client_class.return_value = mock_client

        # TASK-96: now returns 3 values
        operations, _, _ = await ingester.ingest_wsdl(
            wsdl_url="http://test.example.com/service.wsdl",
            connector_id=connector_id,
            tenant_id="test-tenant",
        )

        # Find Add operation
        add_op = next((op for op in operations if op.operation_name == "Add"), None)
        assert add_op is not None
        assert add_op.service_name == "CalculatorService"
        assert add_op.name == "CalculatorService.Add"
        assert add_op.description == "Add two numbers"
        assert add_op.style == SOAPStyle.DOCUMENT

    @patch("meho_openapi.soap.ingester.Client")
    async def test_search_content_built(self, mock_client_class, ingester, connector_id):
        """Search content should be built for BM25"""
        mock_client = MockZeepClient("http://test.example.com/service.wsdl")
        mock_client_class.return_value = mock_client

        # TASK-96: now returns 3 values
        operations, _, _ = await ingester.ingest_wsdl(
            wsdl_url="http://test.example.com/service.wsdl",
            connector_id=connector_id,
            tenant_id="test-tenant",
        )

        add_op = next((op for op in operations if op.operation_name == "Add"), None)
        assert "Add" in add_op.search_content
        assert "CalculatorService" in add_op.search_content


class TestSOAPClientIntegration:
    """Integration tests for SOAP client"""

    @pytest.fixture
    def config(self):
        return SOAPConnectorConfig(
            wsdl_url="http://test.example.com/service.wsdl",
            auth_type=SOAPAuthType.NONE,
        )

    @patch("meho_openapi.soap.client.Client")
    async def test_call_operation_success(self, mock_client_class, config):
        """Calling operation should return result"""
        # Setup mock
        mock_client = MockZeepClient(config.wsdl_url)
        mock_client_class.return_value = mock_client

        client = SOAPClient(config)

        # Patch the _client attribute directly
        client._client = mock_client
        client._is_connected = True

        response = await client.call(
            operation_name="Add",
            params={"intA": 5, "intB": 3},
        )

        assert response.success is True
        assert response.status_code == 200
        assert response.body == 8

    @patch("meho_openapi.soap.client.Client")
    async def test_call_operation_not_found(self, mock_client_class, config):
        """Calling non-existent operation should return error"""
        mock_client = MockZeepClient(config.wsdl_url)
        mock_client_class.return_value = mock_client

        client = SOAPClient(config)
        client._client = mock_client
        client._is_connected = True

        response = await client.call(
            operation_name="NonExistent",
            params={},
        )

        assert response.success is False
        assert response.status_code == 404
        assert "not found" in response.fault_string.lower()


class TestVMwareSOAPClientIntegration:
    """Integration tests for VMware-specific SOAP client"""

    @pytest.fixture
    def vmware_config(self):
        return SOAPConnectorConfig(
            wsdl_url="https://vcenter.local/sdk/vimService.wsdl",
            auth_type=SOAPAuthType.SESSION,
            username="admin@vsphere.local",
            password="test-password",
            login_operation="SessionManager.Login",
            logout_operation="SessionManager.Logout",
            verify_ssl=False,
        )

    @patch("meho_openapi.soap.client.Client")
    async def test_vmware_client_initialization(self, mock_client_class, vmware_config):
        """VMware client should initialize correctly"""
        mock_client = MockZeepClient(vmware_config.wsdl_url)
        mock_client_class.return_value = mock_client

        client = VMwareSOAPClient(vmware_config)

        assert client.config.auth_type == SOAPAuthType.SESSION
        assert client.config.username == "admin@vsphere.local"


class TestProtocolRouterSOAPIntegration:
    """Integration tests for protocol router with SOAP"""

    @pytest.fixture
    def router(self):
        return ProtocolRouter()

    @pytest.fixture
    def soap_connector(self):
        """Create a mock SOAP connector"""
        connector = Mock()
        connector.id = uuid4()
        connector.name = "Test SOAP Service"
        connector.base_url = "http://test.example.com"
        connector.protocol = "soap"
        connector.protocol_config = {
            "wsdl_url": "http://test.example.com/service.wsdl",
        }
        connector.auth_type = "NONE"
        connector.credential_strategy = "SYSTEM"
        return connector

    @pytest.fixture
    def soap_operation(self, soap_connector):
        """Create a mock SOAP operation"""
        return SOAPOperation(
            connector_id=soap_connector.id,
            tenant_id="test-tenant",
            service_name="CalculatorService",
            port_name="CalculatorPort",
            operation_name="Add",
            name="CalculatorService.Add",
            description="Add two numbers",
            soap_action="http://test.example.com/Add",
            style=SOAPStyle.DOCUMENT,
            namespace="http://test.example.com/",
        )

    @patch("meho_openapi.soap.client.Client")
    async def test_router_routes_soap_calls(
        self, mock_client_class, router, soap_connector, soap_operation
    ):
        """Router should route SOAP calls to SOAP client"""
        mock_client = MockZeepClient(soap_connector.protocol_config["wsdl_url"])
        mock_client_class.return_value = mock_client

        # Create a mock that returns a proper response
        with patch.object(router, "_call_soap", new_callable=AsyncMock) as mock_soap:
            mock_soap.return_value = (200, {"result": 8})

            status, data = await router.call(
                connector=soap_connector,
                operation=soap_operation,
                params={"intA": 5, "intB": 3},
            )

            assert status == 200
            assert data["result"] == 8
            mock_soap.assert_called_once()

    async def test_router_rejects_non_soap_connector(self, router):
        """Router should reject non-SOAP connectors for SOAP operations"""
        rest_connector = Mock()
        rest_connector.protocol = "rest"

        soap_op = SOAPOperation(
            connector_id=uuid4(),
            tenant_id="test",
            service_name="Test",
            port_name="TestPort",
            operation_name="TestOp",
            name="Test.TestOp",
            namespace="urn:test",
        )

        # Call should proceed but would fail if we tried SOAP on REST
        # Router checks protocol and routes accordingly
        with pytest.raises(ValueError, match="endpoint"):
            await router.call(
                connector=rest_connector,
                operation=soap_op,
                params={},
            )


class TestSOAPSearchIntegration:
    """Test SOAP operations are searchable"""

    @pytest.fixture
    def operations(self):
        """Create sample SOAP operations"""
        connector_id = uuid4()
        return [
            SOAPOperation(
                connector_id=connector_id,
                tenant_id="test",
                service_name="VimService",
                port_name="VimPort",
                operation_name="RetrieveProperties",
                name="VimService.RetrieveProperties",
                description="Retrieve properties from managed objects",
                namespace="urn:vim25",
                search_content="SOAP RetrieveProperties VimService managed objects properties",
            ),
            SOAPOperation(
                connector_id=connector_id,
                tenant_id="test",
                service_name="VimService",
                port_name="VimPort",
                operation_name="ApplyRecommendation",
                name="VimService.ApplyRecommendation",
                description="Apply DRS recommendation to cluster",
                namespace="urn:vim25",
                search_content="SOAP ApplyRecommendation VimService DRS cluster recommendation",
            ),
            SOAPOperation(
                connector_id=connector_id,
                tenant_id="test",
                service_name="VimService",
                port_name="VimPort",
                operation_name="PowerOnVM_Task",
                name="VimService.PowerOnVM_Task",
                description="Power on a virtual machine",
                namespace="urn:vim25",
                search_content="SOAP PowerOnVM_Task VimService power virtual machine VM",
            ),
        ]

    def test_search_by_operation_name(self, operations):
        """Should find operations by name"""
        query = "RetrieveProperties"

        matches = [op for op in operations if query.lower() in op.search_content.lower()]

        assert len(matches) == 1
        assert matches[0].operation_name == "RetrieveProperties"

    def test_search_by_description(self, operations):
        """Should find operations by description content"""
        query = "DRS"

        matches = [op for op in operations if query.lower() in op.search_content.lower()]

        assert len(matches) == 1
        assert matches[0].operation_name == "ApplyRecommendation"

    def test_search_by_concept(self, operations):
        """Should find operations by related concepts"""
        query = "power"

        matches = [op for op in operations if query.lower() in op.search_content.lower()]

        assert len(matches) == 1
        assert matches[0].operation_name == "PowerOnVM_Task"
