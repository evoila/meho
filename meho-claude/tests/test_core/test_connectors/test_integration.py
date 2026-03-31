"""Integration tests for connector registry auto-import and full instantiation path.

Verifies that importing the connectors package triggers @register_connector
decorators for all 6 connector types, and that the full CLI->loader->registry->connector
instantiation chain works end-to-end.

IMPORTANT: No concrete connector modules are imported at the top level -- this ensures
the auto-import mechanism in connectors/__init__.py is what populates the registry.
"""

from unittest.mock import MagicMock

import pytest

from meho_claude.core.connectors.models import ConnectorConfig
from meho_claude.core.connectors.registry import get_connector_class, list_connector_types

ALL_CONNECTOR_TYPES = ["rest", "kubernetes", "soap", "vmware", "proxmox", "gcp"]


class TestConnectorAutoImport:
    """Verify all 6 connector types are auto-registered on package import."""

    @pytest.mark.parametrize("connector_type", ALL_CONNECTOR_TYPES)
    def test_all_connector_types_registered(self, connector_type: str):
        """get_connector_class should succeed for each type after package import."""
        # Importing the package should have already triggered auto-imports
        import meho_claude.core.connectors  # noqa: F401

        cls = get_connector_class(connector_type)
        assert cls is not None, f"Connector type '{connector_type}' not in registry"

    def test_list_connector_types_returns_all_six(self):
        """list_connector_types should contain all 6 types."""
        import meho_claude.core.connectors  # noqa: F401

        registered = list_connector_types()
        for ct in ALL_CONNECTOR_TYPES:
            assert ct in registered, f"'{ct}' missing from list_connector_types()"
        assert len(registered) >= 6


class TestConnectorInstantiation:
    """Verify full instantiation path through loader for each connector type."""

    @pytest.mark.parametrize("connector_type", ALL_CONNECTOR_TYPES)
    def test_instantiate_connector_for_each_type(self, connector_type: str):
        """instantiate_connector should return a BaseConnector for each type."""
        from meho_claude.core.connectors.base import BaseConnector
        from meho_claude.core.connectors.loader import instantiate_connector

        # Build config appropriate for each connector type
        kwargs = {
            "name": f"test-{connector_type}",
            "connector_type": connector_type,
            "auth": None,
        }

        if connector_type == "kubernetes":
            kwargs["kubeconfig_path"] = "/tmp/fake-kubeconfig"
        elif connector_type == "gcp":
            kwargs["project_id"] = "test-project"
        else:
            kwargs["base_url"] = "https://example.com"

        config = ConnectorConfig(**kwargs)
        mock_cm = MagicMock()

        connector = instantiate_connector(config, mock_cm)
        assert isinstance(connector, BaseConnector), (
            f"Expected BaseConnector instance for '{connector_type}', "
            f"got {type(connector).__name__}"
        )


class TestImportChain:
    """Verify the import chain: loader -> connectors/__init__.py -> auto-imports."""

    def test_registry_not_empty_after_loader_import(self):
        """Importing loader should trigger connectors/__init__.py auto-imports."""
        import meho_claude.core.connectors.loader  # noqa: F401

        registered = list_connector_types()
        assert len(registered) > 0, "Registry empty after importing loader"
        assert "rest" in registered, "REST connector not registered after loader import"
