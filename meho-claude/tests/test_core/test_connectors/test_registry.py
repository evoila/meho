"""Tests for connector type registry."""

import pytest

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import (
    _REGISTRY,
    get_connector_class,
    list_connector_types,
    register_connector,
)


class DummyConnector(BaseConnector):
    """Minimal concrete connector for testing."""

    async def test_connection(self):
        return {"status": "ok"}

    async def discover_operations(self):
        return []

    async def execute(self, operation, params):
        return {}

    def get_trust_tier(self, operation):
        return operation.trust_tier


class TestBaseConnector:
    def test_cannot_instantiate_abc(self):
        from meho_claude.core.connectors.models import AuthConfig

        auth = AuthConfig(method="bearer", credential_name="test")
        cfg = ConnectorConfig(
            name="test",
            connector_type="rest",
            base_url="https://example.com",
            auth=auth,
        )
        with pytest.raises(TypeError):
            BaseConnector(cfg)

    def test_concrete_subclass_instantiates(self):
        from meho_claude.core.connectors.models import AuthConfig

        auth = AuthConfig(method="bearer", credential_name="test")
        cfg = ConnectorConfig(
            name="test",
            connector_type="rest",
            base_url="https://example.com",
            auth=auth,
        )
        connector = DummyConnector(cfg)
        assert connector.config.name == "test"
        assert connector.credentials is None

    def test_concrete_subclass_with_credentials(self):
        from meho_claude.core.connectors.models import AuthConfig

        auth = AuthConfig(method="bearer", credential_name="test")
        cfg = ConnectorConfig(
            name="test",
            connector_type="rest",
            base_url="https://example.com",
            auth=auth,
        )
        creds = {"token": "abc123"}
        connector = DummyConnector(cfg, credentials=creds)
        assert connector.credentials == {"token": "abc123"}

    def test_close_is_concrete_noop(self):
        from meho_claude.core.connectors.models import AuthConfig

        auth = AuthConfig(method="bearer", credential_name="test")
        cfg = ConnectorConfig(
            name="test",
            connector_type="rest",
            base_url="https://example.com",
            auth=auth,
        )
        connector = DummyConnector(cfg)
        # Should not raise
        connector.close()


class TestRegistry:
    def setup_method(self):
        """Save and clear registry before each test."""
        self._saved_registry = dict(_REGISTRY)
        _REGISTRY.clear()

    def teardown_method(self):
        """Restore registry after each test."""
        _REGISTRY.clear()
        _REGISTRY.update(self._saved_registry)

    def test_register_and_get(self):
        @register_connector("test-type")
        class TestConn(BaseConnector):
            async def test_connection(self):
                return {}

            async def discover_operations(self):
                return []

            async def execute(self, operation, params):
                return {}

            def get_trust_tier(self, operation):
                return "READ"

        cls = get_connector_class("test-type")
        assert cls is TestConn

    def test_get_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown connector type"):
            get_connector_class("nonexistent")

    def test_list_types_sorted(self):
        @register_connector("zebra")
        class Z(DummyConnector):
            pass

        @register_connector("alpha")
        class A(DummyConnector):
            pass

        types = list_connector_types()
        assert types == ["alpha", "zebra"]

    def test_list_types_empty(self):
        assert list_connector_types() == []
