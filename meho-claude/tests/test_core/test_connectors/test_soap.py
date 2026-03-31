"""Tests for SOAPConnector -- mirrors test_vmware.py pattern."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_claude.core.connectors.models import ConnectorConfig, Operation, TrustOverride


class TestSOAPConnectorRegistration:
    """Test that SOAPConnector is registered as 'soap'."""

    def test_registered_as_soap(self):
        # Import triggers registration
        import meho_claude.core.connectors.soap  # noqa: F401
        from meho_claude.core.connectors.registry import get_connector_class

        cls = get_connector_class("soap")
        assert cls.__name__ == "SOAPConnector"


class TestSOAPConnectorInit:
    """Test SOAPConnector initialization."""

    def test_missing_spec_url_and_spec_path_raises(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="bad-soap",
            connector_type="soap",
        )
        connector = SOAPConnector(config)
        with pytest.raises(ValueError, match="spec_url.*spec_path"):
            connector._get_wsdl_source()

    def test_get_wsdl_source_from_spec_url(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
        )
        connector = SOAPConnector(config)
        assert connector._get_wsdl_source() == "https://sap.example.com/service?wsdl"

    def test_get_wsdl_source_from_spec_path(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="local-soap",
            connector_type="soap",
            spec_path="/tmp/service.wsdl",
        )
        connector = SOAPConnector(config)
        assert connector._get_wsdl_source() == "/tmp/service.wsdl"

    def test_spec_url_preferred_over_spec_path(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="dual",
            connector_type="soap",
            spec_url="https://example.com/service?wsdl",
            spec_path="/tmp/service.wsdl",
        )
        connector = SOAPConnector(config)
        assert connector._get_wsdl_source() == "https://example.com/service?wsdl"


class TestSOAPConnectorTestConnection:
    """Test SOAPConnector.test_connection."""

    @pytest.mark.asyncio
    @patch("meho_claude.core.connectors.soap.Client")
    @patch("meho_claude.core.connectors.soap.Settings")
    async def test_successful_connection(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.soap import SOAPConnector

        mock_client = MagicMock()
        mock_client.wsdl.services = {"Svc1": MagicMock(), "Svc2": MagicMock()}
        mock_client_cls.return_value = mock_client

        config = ConnectorConfig(
            name="test-soap",
            connector_type="soap",
            spec_url="https://example.com/service?wsdl",
        )
        connector = SOAPConnector(config)
        result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["services"] == 2

    @pytest.mark.asyncio
    @patch("meho_claude.core.connectors.soap.Client")
    @patch("meho_claude.core.connectors.soap.Settings")
    async def test_connection_error(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.soap import SOAPConnector

        mock_client_cls.side_effect = Exception("Connection refused")

        config = ConnectorConfig(
            name="bad-soap",
            connector_type="soap",
            spec_url="https://bad.example.com/service?wsdl",
        )
        connector = SOAPConnector(config)
        result = await connector.test_connection()

        assert result["status"] == "error"
        assert "Connection refused" in result["message"]


class TestSOAPConnectorDiscoverOperations:
    """Test SOAPConnector.discover_operations delegates to parse_wsdl."""

    @pytest.mark.asyncio
    @patch("meho_claude.core.connectors.soap.parse_wsdl")
    async def test_discover_delegates_to_parse_wsdl(self, mock_parse):
        from meho_claude.core.connectors.soap import SOAPConnector

        mock_ops = [
            Operation(
                connector_name="sap",
                operation_id="Svc.GetUser",
                display_name="GetUser",
                trust_tier="READ",
                tags=["Svc", "soap"],
            )
        ]
        mock_parse.return_value = mock_ops

        config = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
        )
        connector = SOAPConnector(config)
        ops = await connector.discover_operations()

        assert len(ops) == 1
        assert ops[0].operation_id == "Svc.GetUser"
        mock_parse.assert_called_once_with(
            "https://sap.example.com/service?wsdl", "sap"
        )


class TestSOAPConnectorExecute:
    """Test SOAPConnector.execute calls zeep CachingClient and serializes."""

    @pytest.mark.asyncio
    @patch("meho_claude.core.connectors.soap.serialize_object")
    @patch("meho_claude.core.connectors.soap.CachingClient")
    @patch("meho_claude.core.connectors.soap.Settings")
    async def test_execute_calls_operation(
        self, mock_settings, mock_caching_cls, mock_serialize
    ):
        from meho_claude.core.connectors.soap import SOAPConnector

        # Mock the service proxy
        mock_result = MagicMock()
        mock_service = MagicMock()
        mock_service.GetUser.return_value = mock_result

        mock_client = MagicMock()
        mock_client.service = mock_service
        mock_caching_cls.return_value = mock_client

        mock_serialize.return_value = {"name": "John", "id": "123"}

        config = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
        )
        connector = SOAPConnector(config)

        operation = Operation(
            connector_name="sap",
            operation_id="UserService.GetUser",
            display_name="GetUser",
            trust_tier="READ",
        )

        result = await connector.execute(operation, {"userId": "123"})

        assert result["data"] == {"name": "John", "id": "123"}
        mock_service.GetUser.assert_called_once_with(userId="123")
        mock_serialize.assert_called_once_with(mock_result, target_cls=dict)


class TestSOAPConnectorTrustTier:
    """Test SOAPConnector.get_trust_tier respects overrides."""

    def test_returns_operation_trust_tier(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
        )
        connector = SOAPConnector(config)

        op = Operation(
            connector_name="sap",
            operation_id="Svc.GetUser",
            display_name="GetUser",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_trust_override_takes_precedence(self):
        from meho_claude.core.connectors.soap import SOAPConnector

        config = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
            trust_overrides=[
                TrustOverride(operation_id="Svc.GetUser", trust_tier="DESTRUCTIVE"),
            ],
        )
        connector = SOAPConnector(config)

        op = Operation(
            connector_name="sap",
            operation_id="Svc.GetUser",
            display_name="GetUser",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "DESTRUCTIVE"
