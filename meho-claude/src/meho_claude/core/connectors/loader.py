"""YAML connector config loading, saving, and connector instantiation.

Auto-discovers *.yaml files in ~/.meho/connectors/, validates them through
ConnectorConfig Pydantic model, and uses the registry to instantiate the
correct connector class with credentials from CredentialManager.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import yaml

from meho_claude.core.connectors.models import ConnectorConfig
from meho_claude.core.connectors.registry import get_connector_class

if TYPE_CHECKING:
    from meho_claude.core.connectors.base import BaseConnector
    from meho_claude.core.credentials import CredentialManager

logger = structlog.get_logger()


def load_connector_config(yaml_path: Path) -> ConnectorConfig:
    """Load and validate a single connector config from a YAML file.

    Args:
        yaml_path: Path to the YAML config file.

    Returns:
        Validated ConnectorConfig.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValidationError: If YAML content fails Pydantic validation.
        yaml.YAMLError: If YAML is malformed.
    """
    if not yaml_path.exists():
        raise FileNotFoundError(f"Connector config not found: {yaml_path}")

    raw = yaml_path.read_text()
    data = yaml.safe_load(raw)
    return ConnectorConfig.model_validate(data)


def load_all_configs(connectors_dir: Path) -> list[ConnectorConfig]:
    """Discover and load all *.yaml connector configs from a directory.

    Invalid configs are skipped with a warning log (never crashes).
    Returns configs sorted by name for deterministic output.

    Args:
        connectors_dir: Directory containing *.yaml connector configs.

    Returns:
        List of validated ConnectorConfig, sorted by name.
    """
    configs: list[ConnectorConfig] = []

    for yaml_path in sorted(connectors_dir.glob("*.yaml")):
        try:
            cfg = load_connector_config(yaml_path)
            configs.append(cfg)
        except Exception as exc:
            logger.warning(
                "skipping_invalid_connector_config",
                path=str(yaml_path),
                error=str(exc),
            )

    return sorted(configs, key=lambda c: c.name)


def save_connector_config(config: ConnectorConfig, connectors_dir: Path) -> Path:
    """Write a validated ConnectorConfig to YAML.

    Args:
        config: Validated ConnectorConfig to save.
        connectors_dir: Directory to write the YAML file to.

    Returns:
        Path to the saved YAML file.
    """
    yaml_path = connectors_dir / f"{config.name}.yaml"
    data = config.model_dump(exclude_none=True)
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return yaml_path


def instantiate_connector(
    config: ConnectorConfig,
    credential_manager: CredentialManager,
) -> BaseConnector:
    """Instantiate a connector from config + credentials.

    Looks up the connector class from the registry, retrieves credentials
    from CredentialManager, and returns an initialized connector instance.

    Args:
        config: Validated ConnectorConfig.
        credential_manager: CredentialManager for decrypting credentials.

    Returns:
        Initialized BaseConnector subclass instance.

    Raises:
        ValueError: If connector type is not registered.
    """
    cls = get_connector_class(config.connector_type)
    credentials = None
    if config.auth is not None:
        credentials = credential_manager.retrieve(config.auth.credential_name)
    return cls(config, credentials=credentials)
