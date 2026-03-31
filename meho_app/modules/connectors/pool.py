# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector Router, Registry, and Connection Pool.

Routes operations to the right implementation based on connector_type.
This is the ONLY place in the codebase that switches on connector_type.
Everything else uses the generic BaseConnector interface.
"""

from typing import Any

from meho_app.modules.connectors.base import BaseConnector, OperationResult

# Connector type registry
_connector_types: dict[str, type] = {}


def register_connector_type(connector_type: str, connector_class: type) -> None:
    """Register a connector implementation for a connector type."""
    _connector_types[connector_type] = connector_class


async def get_connector_instance(
    connector_type: str,
    connector_id: str,
    config: dict[str, Any],
    credentials: dict[str, Any],
) -> BaseConnector:
    """
    Factory function to get the right connector implementation.

    This is the ONLY place in the codebase that switches on connector_type.
    Everything else uses the generic BaseConnector interface.

    Args:
        connector_type: Type of connector (vmware, rest, soap)
        connector_id: Unique connector identifier
        config: Connector configuration (host, port, etc.)
        credentials: User credentials (username, password)

    Returns:
        Configured connector instance

    Raises:
        ValueError: If connector_type is unknown
    """
    if connector_type == "vmware":
        # Lazy import to avoid loading pyvmomi when not needed
        from meho_app.modules.connectors.vmware import VMwareConnector

        return VMwareConnector(connector_id, config, credentials)

    if connector_type == "proxmox":
        # Lazy import to avoid loading proxmoxer when not needed
        from meho_app.modules.connectors.proxmox import ProxmoxConnector

        return ProxmoxConnector(connector_id, config, credentials)

    if connector_type == "gcp":
        # Lazy import to avoid loading google-cloud SDKs when not needed
        from meho_app.modules.connectors.gcp import GCPConnector

        return GCPConnector(connector_id, config, credentials)

    if connector_type == "kubernetes":
        # Lazy import to avoid loading kubernetes-asyncio when not needed
        from meho_app.modules.connectors.kubernetes import KubernetesConnector

        return KubernetesConnector(connector_id, config, credentials)

    if connector_type == "prometheus":
        from meho_app.modules.connectors.prometheus import PrometheusConnector

        return PrometheusConnector(connector_id, config, credentials)

    if connector_type == "loki":
        from meho_app.modules.connectors.loki import LokiConnector

        return LokiConnector(connector_id, config, credentials)

    if connector_type == "tempo":
        from meho_app.modules.connectors.tempo import TempoConnector

        return TempoConnector(connector_id, config, credentials)

    if connector_type == "alertmanager":
        from meho_app.modules.connectors.alertmanager import AlertmanagerConnector

        return AlertmanagerConnector(connector_id, config, credentials)

    if connector_type == "jira":
        from meho_app.modules.connectors.jira import JiraConnector

        return JiraConnector(connector_id, config, credentials)

    if connector_type == "confluence":
        from meho_app.modules.connectors.confluence import ConfluenceConnector

        return ConfluenceConnector(connector_id, config, credentials)

    if connector_type == "email":
        from meho_app.modules.connectors.email.connector import EmailConnector

        return EmailConnector(connector_id, config, credentials)

    if connector_type == "argocd":
        from meho_app.modules.connectors.argocd import ArgoConnector

        return ArgoConnector(connector_id, config, credentials)

    if connector_type == "github":
        from meho_app.modules.connectors.github import GitHubConnector

        return GitHubConnector(connector_id, config, credentials)

    if connector_type == "slack":
        from meho_app.modules.connectors.slack import SlackConnector

        return SlackConnector(connector_id, config, credentials)

    if connector_type == "aws":
        from meho_app.modules.connectors.aws import AWSConnector

        return AWSConnector(connector_id, config, credentials)

    if connector_type == "azure":
        from meho_app.modules.connectors.azure import AzureConnector

        return AzureConnector(connector_id, config, credentials)

    if connector_type == "mcp":
        from meho_app.modules.connectors.mcp import MCPConnector

        return MCPConnector(connector_id, config, credentials)

    # Check registry for other connector types
    if connector_type in _connector_types:
        connector_class = _connector_types[connector_type]
        connector: BaseConnector = connector_class(connector_id, config, credentials)
        return connector

    raise ValueError(f"Unknown connector type: {connector_type}")


async def execute_connector_operation(
    connector_type: str,
    connector_id: str,
    config: dict[str, Any],
    credentials: dict[str, Any],
    operation_id: str,
    parameters: dict[str, Any],
) -> OperationResult:
    """
    Execute an operation on any connector type.

    This is the main entry point called by API routes and agent tools.
    Handles connection lifecycle automatically.

    Args:
        connector_type: Type of connector (vmware, rest, soap)
        connector_id: Unique connector identifier
        config: Connector configuration
        credentials: User credentials
        operation_id: Operation to execute (e.g., "list_virtual_machines")
        parameters: Parameters for the operation

    Returns:
        OperationResult with success status and data/error
    """
    connector = await get_connector_instance(connector_type, connector_id, config, credentials)

    try:
        await connector.connect()
        result = await connector.execute(operation_id, parameters)
        return result
    finally:
        await connector.disconnect()


# Connection pool for keeping sessions alive across multiple calls
_connector_pool: dict[tuple, BaseConnector] = {}


async def get_pooled_connector(
    connector_type: str,
    connector_id: str,
    user_id: str,
    config: dict[str, Any],
    credentials: dict[str, Any],
) -> BaseConnector:
    """
    Get or create a pooled connector instance.

    Keeps connections alive for session reuse (important for VMware).

    Args:
        connector_type: Type of connector
        connector_id: Connector ID
        user_id: User ID (for per-user sessions)
        config: Connector configuration
        credentials: User credentials

    Returns:
        Connected connector instance (cached or new)
    """
    cache_key = (connector_id, user_id)

    # Check pool
    if cache_key in _connector_pool:
        connector = _connector_pool[cache_key]
        if connector.is_connected:
            return connector

    # Create new connector
    connector = await get_connector_instance(connector_type, connector_id, config, credentials)
    await connector.connect()

    # Cache it
    _connector_pool[cache_key] = connector

    return connector


def clear_connector_pool(connector_id: str | None = None) -> None:
    """Clear pooled connections (for cleanup or session expiry)."""
    global _connector_pool

    if connector_id:
        # Clear specific connector
        to_remove = [k for k in _connector_pool if k[0] == connector_id]
        for key in to_remove:
            _connector_pool.pop(key, None)
    else:
        # Clear all
        _connector_pool.clear()


__all__ = [
    "clear_connector_pool",
    "execute_connector_operation",
    "get_connector_instance",
    "get_pooled_connector",
    "register_connector_type",
]
