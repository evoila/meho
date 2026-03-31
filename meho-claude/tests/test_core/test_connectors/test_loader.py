"""Tests for YAML connector config loader and connector instantiation."""

import pytest
import yaml
from pydantic import ValidationError

from meho_claude.core.connectors.loader import (
    instantiate_connector,
    load_all_configs,
    load_connector_config,
    save_connector_config,
)
from meho_claude.core.connectors.models import AuthConfig, ConnectorConfig


@pytest.fixture()
def valid_config_dict():
    return {
        "name": "test-api",
        "connector_type": "rest",
        "base_url": "https://api.example.com",
        "auth": {
            "method": "bearer",
            "credential_name": "test-creds",
        },
    }


@pytest.fixture()
def valid_config_yaml(tmp_path, valid_config_dict):
    yaml_path = tmp_path / "test-api.yaml"
    yaml_path.write_text(yaml.dump(valid_config_dict))
    return yaml_path


@pytest.fixture()
def connectors_dir(tmp_path, valid_config_dict):
    """Create a connectors directory with multiple YAML files."""
    d = tmp_path / "connectors"
    d.mkdir()
    # Valid config
    (d / "alpha.yaml").write_text(yaml.dump({
        **valid_config_dict,
        "name": "alpha-api",
    }))
    # Another valid config
    (d / "beta.yaml").write_text(yaml.dump({
        **valid_config_dict,
        "name": "beta-api",
        "connector_type": "kubernetes",
    }))
    return d


class TestLoadConnectorConfig:
    def test_loads_valid_yaml(self, valid_config_yaml):
        cfg = load_connector_config(valid_config_yaml)
        assert isinstance(cfg, ConnectorConfig)
        assert cfg.name == "test-api"
        assert cfg.connector_type == "rest"
        assert cfg.base_url == "https://api.example.com"

    def test_raises_on_invalid_yaml_content(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(yaml.dump({"name": "bad", "connector_type": "graphql"}))
        with pytest.raises(ValidationError):
            load_connector_config(bad_yaml)

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_connector_config(tmp_path / "nonexistent.yaml")

    def test_raises_on_malformed_yaml(self, tmp_path):
        bad_yaml = tmp_path / "malformed.yaml"
        bad_yaml.write_text(":::\n  - invalid yaml: [")
        with pytest.raises(Exception):
            load_connector_config(bad_yaml)


class TestLoadAllConfigs:
    def test_discovers_all_yaml_files(self, connectors_dir):
        configs = load_all_configs(connectors_dir)
        assert len(configs) == 2

    def test_returns_sorted_by_name(self, connectors_dir):
        configs = load_all_configs(connectors_dir)
        assert configs[0].name == "alpha-api"
        assert configs[1].name == "beta-api"

    def test_skips_invalid_yaml(self, connectors_dir):
        # Add an invalid YAML file
        (connectors_dir / "invalid.yaml").write_text(
            yaml.dump({"name": "bad", "connector_type": "graphql"})
        )
        configs = load_all_configs(connectors_dir)
        # Should still return the 2 valid ones, not crash
        assert len(configs) == 2

    def test_returns_empty_for_no_files(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        configs = load_all_configs(empty_dir)
        assert configs == []

    def test_ignores_non_yaml_files(self, connectors_dir):
        (connectors_dir / "readme.txt").write_text("not a config")
        (connectors_dir / "data.json").write_text("{}")
        configs = load_all_configs(connectors_dir)
        assert len(configs) == 2


class TestSaveConnectorConfig:
    def test_saves_and_roundtrips(self, tmp_path):
        cfg = ConnectorConfig(
            name="saved-api",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=AuthConfig(method="bearer", credential_name="creds"),
        )
        connectors_dir = tmp_path / "connectors"
        connectors_dir.mkdir()

        saved_path = save_connector_config(cfg, connectors_dir)

        assert saved_path.exists()
        assert saved_path.name == "saved-api.yaml"

        # Round-trip: reload and verify
        reloaded = load_connector_config(saved_path)
        assert reloaded.name == cfg.name
        assert reloaded.connector_type == cfg.connector_type
        assert reloaded.base_url == cfg.base_url
        assert reloaded.auth.method == cfg.auth.method

    def test_excludes_none_fields(self, tmp_path):
        cfg = ConnectorConfig(
            name="minimal",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=AuthConfig(method="bearer", credential_name="creds"),
        )
        connectors_dir = tmp_path / "connectors"
        connectors_dir.mkdir()

        saved_path = save_connector_config(cfg, connectors_dir)
        raw = yaml.safe_load(saved_path.read_text())
        assert "spec_url" not in raw
        assert "spec_path" not in raw


class TestInstantiateConnector:
    def setup_method(self):
        """Save registry state before each test."""
        from meho_claude.core.connectors.registry import _REGISTRY
        self._saved_registry = dict(_REGISTRY)

    def teardown_method(self):
        """Restore registry state after each test."""
        from meho_claude.core.connectors.registry import _REGISTRY
        _REGISTRY.clear()
        _REGISTRY.update(self._saved_registry)

    def test_raises_for_unregistered_type(self, tmp_path):
        from unittest.mock import MagicMock

        from meho_claude.core.connectors.registry import _REGISTRY
        _REGISTRY.clear()

        cfg = ConnectorConfig(
            name="test",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=AuthConfig(method="bearer", credential_name="test-creds"),
        )
        mock_cm = MagicMock()
        mock_cm.retrieve.return_value = {"token": "abc"}

        with pytest.raises(ValueError, match="Unknown connector type"):
            instantiate_connector(cfg, mock_cm)

    def test_wires_registry_and_credentials(self, tmp_path):
        from unittest.mock import MagicMock

        from meho_claude.core.connectors.base import BaseConnector
        from meho_claude.core.connectors.registry import _REGISTRY, register_connector

        _REGISTRY.clear()

        @register_connector("rest")
        class FakeRESTConnector(BaseConnector):
            async def test_connection(self):
                return {"status": "ok"}

            async def discover_operations(self):
                return []

            async def execute(self, operation, params):
                return {}

            def get_trust_tier(self, operation):
                return "READ"

        cfg = ConnectorConfig(
            name="wired",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=AuthConfig(method="bearer", credential_name="my-creds"),
        )
        mock_cm = MagicMock()
        mock_cm.retrieve.return_value = {"token": "secret-token"}

        connector = instantiate_connector(cfg, mock_cm)

        assert isinstance(connector, FakeRESTConnector)
        assert connector.config.name == "wired"
        assert connector.credentials == {"token": "secret-token"}
        mock_cm.retrieve.assert_called_once_with("my-creds")
