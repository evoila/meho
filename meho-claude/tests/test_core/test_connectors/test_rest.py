"""Tests for RESTConnector."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_claude.core.connectors.models import (
    AuthConfig,
    ConnectorConfig,
    Operation,
    TrustOverride,
)
from meho_claude.core.connectors.rest import RESTConnector
from meho_claude.core.connectors.registry import _REGISTRY, get_connector_class


class TestRESTConnectorRegistration:
    def test_rest_registered_in_registry(self):
        # Importing the module should trigger registration
        cls = get_connector_class("rest")
        assert cls is RESTConnector

    def test_rest_in_list(self):
        from meho_claude.core.connectors.registry import list_connector_types

        assert "rest" in list_connector_types()


def _make_config(**overrides) -> ConnectorConfig:
    """Helper to create a ConnectorConfig with sensible defaults."""
    defaults = {
        "name": "test-api",
        "connector_type": "rest",
        "base_url": "https://api.example.com",
        "spec_url": "https://api.example.com/openapi.yaml",
        "auth": AuthConfig(method="bearer", credential_name="test-api"),
    }
    defaults.update(overrides)
    return ConnectorConfig(**defaults)


class TestRESTConnectorTestConnection:
    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["status_code"] == 200
        assert "response_time_ms" in result

    @pytest.mark.asyncio
    async def test_test_connection_head_405_fallback_to_get(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        head_response = MagicMock()
        head_response.status_code = 405

        get_response = MagicMock()
        get_response.status_code = 200

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=[head_response, get_response])
            mock_client_factory.return_value = mock_client

            result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_test_connection_error(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_factory.return_value = mock_client

            result = await connector.test_connection()

        assert result["status"] == "error"
        assert "Connection refused" in result["message"]


class TestRESTConnectorDiscoverOperations:
    @pytest.mark.asyncio
    async def test_discover_operations_from_spec_url(self):
        config = _make_config(spec_url="https://api.example.com/openapi.yaml")
        connector = RESTConnector(config, credentials={"token": "abc123"})

        mock_ops = [
            Operation(
                connector_name="test-api",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
            )
        ]

        with patch(
            "meho_claude.core.connectors.rest.parse_openapi_spec",
            return_value=mock_ops,
        ) as mock_parse:
            ops = await connector.discover_operations()

        mock_parse.assert_called_once_with("https://api.example.com/openapi.yaml", "test-api")
        assert len(ops) == 1
        assert ops[0].operation_id == "listPets"

    @pytest.mark.asyncio
    async def test_discover_operations_from_spec_path(self):
        config = _make_config(spec_url=None, spec_path="/tmp/spec.yaml")
        connector = RESTConnector(config, credentials={"token": "abc123"})

        with patch(
            "meho_claude.core.connectors.rest.parse_openapi_spec",
            return_value=[],
        ) as mock_parse:
            ops = await connector.discover_operations()

        mock_parse.assert_called_once_with("/tmp/spec.yaml", "test-api")

    @pytest.mark.asyncio
    async def test_discover_applies_trust_overrides(self):
        overrides = [TrustOverride(operation_id="listPets", trust_tier="WRITE")]
        config = _make_config(trust_overrides=overrides)
        connector = RESTConnector(config, credentials={"token": "abc123"})

        mock_ops = [
            Operation(
                connector_name="test-api",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
            )
        ]

        with patch(
            "meho_claude.core.connectors.rest.parse_openapi_spec",
            return_value=mock_ops,
        ):
            ops = await connector.discover_operations()

        assert ops[0].trust_tier == "WRITE"


class TestRESTConnectorExecute:
    @pytest.mark.asyncio
    async def test_execute_get_request(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        operation = Operation(
            connector_name="test-api",
            operation_id="listPets",
            display_name="List pets",
            http_method="GET",
            url_template="/pets",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = [{"id": 1, "name": "Fido"}]

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await connector.execute(operation, {})

        assert result["status_code"] == 200
        assert result["data"] == [{"id": 1, "name": "Fido"}]

    @pytest.mark.asyncio
    async def test_execute_with_path_params(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        operation = Operation(
            connector_name="test-api",
            operation_id="getPet",
            display_name="Get pet",
            http_method="GET",
            url_template="/pets/{petId}",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"id": 42, "name": "Rex"}

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await connector.execute(operation, {"petId": 42})

        # Verify the URL was built with the path parameter substituted
        call_args = mock_client.request.call_args
        assert "/pets/42" in str(call_args)

    @pytest.mark.asyncio
    async def test_execute_post_with_body(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        operation = Operation(
            connector_name="test-api",
            operation_id="createPet",
            display_name="Create pet",
            http_method="POST",
            url_template="/pets",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"id": 1, "name": "Buddy"}

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await connector.execute(operation, {"name": "Buddy"})

        assert result["status_code"] == 201

    @pytest.mark.asyncio
    async def test_execute_non_json_response(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        operation = Operation(
            connector_name="test-api",
            operation_id="getHealth",
            display_name="Health",
            http_method="GET",
            url_template="/health",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.json.side_effect = Exception("not JSON")
        mock_response.text = "OK"

        with patch.object(connector, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await connector.execute(operation, {})

        assert result["data"] == "OK"


class TestRESTConnectorGetTrustTier:
    def test_default_tier_from_operation(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})

        op = Operation(
            connector_name="test-api",
            operation_id="listPets",
            display_name="List pets",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_override_from_config(self):
        overrides = [TrustOverride(operation_id="listPets", trust_tier="DESTRUCTIVE")]
        config = _make_config(trust_overrides=overrides)
        connector = RESTConnector(config, credentials={"token": "abc123"})

        op = Operation(
            connector_name="test-api",
            operation_id="listPets",
            display_name="List pets",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "DESTRUCTIVE"

    def test_no_override_falls_through(self):
        overrides = [TrustOverride(operation_id="deletePet", trust_tier="DESTRUCTIVE")]
        config = _make_config(trust_overrides=overrides)
        connector = RESTConnector(config, credentials={"token": "abc123"})

        op = Operation(
            connector_name="test-api",
            operation_id="listPets",
            display_name="List pets",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"


class TestRESTConnectorClose:
    def test_close_without_client(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})
        # Should not raise even without initializing the client
        connector.close()

    def test_close_with_client(self):
        config = _make_config()
        connector = RESTConnector(config, credentials={"token": "abc123"})
        connector._client = MagicMock()
        connector.close()
        connector._client.close.assert_called_once()
