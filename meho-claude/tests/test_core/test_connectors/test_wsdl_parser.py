"""Tests for WSDL parser -- mirrors test_openapi_parser.py pattern."""

from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.connectors.models import Operation


class TestInferTrustTier:
    """Test trust tier heuristics for SOAP operation names."""

    def test_read_prefixes(self):
        from meho_claude.core.connectors.wsdl_parser import _infer_trust_tier

        for prefix in ("Get", "List", "Find", "Search", "Query", "Retrieve", "Fetch", "Read"):
            assert _infer_trust_tier(f"{prefix}User") == "READ", f"Failed for {prefix}User"

    def test_write_prefixes(self):
        from meho_claude.core.connectors.wsdl_parser import _infer_trust_tier

        for prefix in ("Create", "Update", "Set", "Add", "Modify", "Insert", "Put"):
            assert _infer_trust_tier(f"{prefix}Order") == "WRITE", f"Failed for {prefix}Order"

    def test_destructive_prefixes(self):
        from meho_claude.core.connectors.wsdl_parser import _infer_trust_tier

        for prefix in ("Delete", "Remove", "Destroy", "Purge", "Drop", "Clear"):
            assert (
                _infer_trust_tier(f"{prefix}Account") == "DESTRUCTIVE"
            ), f"Failed for {prefix}Account"

    def test_unknown_defaults_to_write(self):
        from meho_claude.core.connectors.wsdl_parser import _infer_trust_tier

        assert _infer_trust_tier("ProcessPayment") == "WRITE"
        assert _infer_trust_tier("RunDiagnostic") == "WRITE"
        assert _infer_trust_tier("Execute") == "WRITE"

    def test_case_insensitive(self):
        from meho_claude.core.connectors.wsdl_parser import _infer_trust_tier

        assert _infer_trust_tier("getUser") == "READ"
        assert _infer_trust_tier("GETUSER") == "READ"
        assert _infer_trust_tier("deleteItem") == "DESTRUCTIVE"


class TestParseWsdl:
    """Test WSDL-to-Operation parsing with mocked zeep."""

    def _build_mock_client(self):
        """Build a mock zeep Client with a WSDL structure containing 3 operations."""
        client = MagicMock()

        # Build operation mocks
        op_get = MagicMock()
        op_get.input = MagicMock()
        op_get.input.body = None

        op_create = MagicMock()
        op_create.input = MagicMock()
        op_create.input.body = None

        op_delete = MagicMock()
        op_delete.input = MagicMock()
        op_delete.input.body = None

        # Build binding mock
        binding = MagicMock()
        binding._operations = {
            "GetUser": op_get,
            "CreateOrder": op_create,
            "DeleteAccount": op_delete,
        }

        # Build port mock
        port = MagicMock()
        port.binding = binding

        # Build service mock
        service = MagicMock()
        service.ports = {"MainPort": port}

        # Attach to client.wsdl.services
        client.wsdl.services = {"UserService": service}

        return client

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_parse_returns_operations(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        ops = parse_wsdl("https://example.com/service?wsdl", "test-soap")

        assert len(ops) == 3
        assert all(isinstance(op, Operation) for op in ops)

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_trust_tiers_correctly_inferred(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        ops = parse_wsdl("https://example.com/service?wsdl", "test-soap")
        op_map = {op.display_name: op for op in ops}

        assert op_map["GetUser"].trust_tier == "READ"
        assert op_map["CreateOrder"].trust_tier == "WRITE"
        assert op_map["DeleteAccount"].trust_tier == "DESTRUCTIVE"

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_operation_id_format(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        ops = parse_wsdl("https://example.com/service?wsdl", "test-soap")
        op_ids = [op.operation_id for op in ops]

        assert "UserService.GetUser" in op_ids
        assert "UserService.CreateOrder" in op_ids
        assert "UserService.DeleteAccount" in op_ids

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_tags_include_service_name_and_soap(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        ops = parse_wsdl("https://example.com/service?wsdl", "test-soap")
        for op in ops:
            assert "UserService" in op.tags
            assert "soap" in op.tags

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_connector_name_set_on_operations(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        ops = parse_wsdl("https://example.com/service?wsdl", "my-sap-connector")
        for op in ops:
            assert op.connector_name == "my-sap-connector"

    @patch("meho_claude.core.connectors.wsdl_parser.Client")
    @patch("meho_claude.core.connectors.wsdl_parser.Settings")
    def test_settings_strict_false(self, mock_settings, mock_client_cls):
        from meho_claude.core.connectors.wsdl_parser import parse_wsdl

        mock_client_cls.return_value = self._build_mock_client()

        parse_wsdl("https://example.com/service?wsdl", "test-soap")
        mock_settings.assert_called_with(strict=False)
