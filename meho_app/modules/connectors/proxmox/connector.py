# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox VE Connector using proxmoxer (TASK-100)

Implements the BaseConnector interface using mixin pattern for organization.
Uses proxmoxer for native Proxmox API access.
"""

import asyncio
import inspect
import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)

# Import all handler mixins
from meho_app.modules.connectors.proxmox.handlers import (
    ContainerHandlerMixin,
    NodeHandlerMixin,
    StorageHandlerMixin,
    VMHandlerMixin,
)
from meho_app.modules.connectors.proxmox.operations import PROXMOX_OPERATIONS
from meho_app.modules.connectors.proxmox.types import PROXMOX_TYPES

logger = get_logger(__name__)


class ProxmoxConnector(
    BaseConnector,
    VMHandlerMixin,
    ContainerHandlerMixin,
    NodeHandlerMixin,
    StorageHandlerMixin,
):
    """
    Proxmox VE connector using proxmoxer.

    Provides native access to Proxmox VE for:
    - VM management (list, power on/off, details)
    - LXC container management (unique to Proxmox)
    - Node (host) operations
    - Storage and resource monitoring

    Organization:
    - VM operations: in vm_handlers.py
    - Container operations: in container_handlers.py
    - Node operations: in node_handlers.py
    - Storage operations: in storage_handlers.py

    Example:
        connector = ProxmoxConnector(
            connector_id="abc123",
            config={
                "host": "proxmox.example.com",
                "port": 8006,
                "verify_ssl": False,
            },
            credentials={
                "username": "root@pam",
                "password": "secret",
                # OR use API token:
                # "token_name": "mytoken",
                # "token_value": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            }
        )

        async with connector:
            result = await connector.execute("list_vms", {})
            print(result.data)
    """

    def __init__(
        self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> None:
        super().__init__(connector_id, config, credentials)
        self._proxmox: Any = None

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:  # NOSONAR (cognitive complexity)
        """Connect to Proxmox VE server."""
        try:
            from proxmoxer import ProxmoxAPI
        except ImportError:
            raise ImportError(
                "proxmoxer is required for Proxmox connector. "
                "Install with: pip install proxmoxer requests"
            ) from None

        host = self.config.get("host")
        if not host:
            raise ValueError("host is required in config")

        port = self.config.get("port", 8006)

        # Handle both naming conventions for SSL verification
        # disable_ssl_verification=True means verify_ssl=False
        if "disable_ssl_verification" in self.config:
            verify_ssl = not self.config.get("disable_ssl_verification", False)
        else:
            verify_ssl = self.config.get("verify_ssl", True)

        # Check for API token auth (recommended)
        # Support both naming conventions: token_name/token_value and api_token_id/api_token_secret
        token_name = self.credentials.get("token_name") or self.credentials.get("api_token_id")
        token_value = self.credentials.get("token_value") or self.credentials.get(
            "api_token_secret"
        )

        # Or username/password auth
        username = self.credentials.get("username")
        password = self.credentials.get("password")

        logger.info(f"🔌 Connecting to Proxmox: {host}:{port}")

        try:
            if token_name and token_value:
                # API Token authentication
                # The api_token_id may be in format "user@realm!token_name"
                # We need to parse it: user goes to 'user', token_name goes to 'token_name'
                if "!" in token_name:
                    # Full format: root@pam!meho-token
                    user_part, token_part = token_name.split("!", 1)
                    api_user = user_part
                    api_token_name = token_part
                else:
                    # Just token name provided, use username or default
                    api_user = username or "root@pam"
                    api_token_name = token_name

                logger.info(f"🔐 API token auth: user={api_user}, token={api_token_name}")

                self._proxmox = ProxmoxAPI(
                    host,
                    port=port,
                    user=api_user,
                    token_name=api_token_name,
                    token_value=token_value,
                    verify_ssl=verify_ssl,
                )
                logger.info(f"✅ Connected to Proxmox via API token: {host}")
            elif username and password:
                # Username/Password authentication
                self._proxmox = ProxmoxAPI(
                    host,
                    port=port,
                    user=username,
                    password=password,
                    verify_ssl=verify_ssl,
                )
                logger.info(f"✅ Connected to Proxmox via password: {host}")
            else:
                raise ValueError(
                    "Either (token_name + token_value) or (username + password) "
                    "are required in credentials"
                )

            self._is_connected = True
            return True

        except Exception as e:
            logger.error(f"❌ Proxmox connection failed: {e}")
            self._is_connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from Proxmox."""
        # proxmoxer doesn't require explicit disconnect
        self._proxmox = None
        self._is_connected = False
        logger.info("🔌 Disconnected from Proxmox")

    async def test_connection(self) -> bool:
        """Test Proxmox connection."""
        try:
            if not self._proxmox:
                await self.connect()

            # Try to get version info
            version = self._proxmox.version.get()
            logger.info(f"✅ Proxmox test: version {version.get('version', 'unknown')}")
            return True
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}")
            return False

    # =========================================================================
    # OPERATION & TYPE DISCOVERY
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get Proxmox operations for registration."""
        return PROXMOX_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get Proxmox types for registration."""
        return PROXMOX_TYPES

    # =========================================================================
    # OPERATION EXECUTION (Routes to handler mixins)
    # =========================================================================

    async def _execute_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> OperationResult:
        """Execute a Proxmox operation."""

        start_time = time.time()

        # Map operation_id to handler method (from mixins)
        handlers = {
            # NODE OPERATIONS (NodeHandlerMixin)
            "list_nodes": self._list_nodes,
            "get_node": self._get_node,
            "get_node_status": self._get_node_status,
            "get_node_resources": self._get_node_resources,
            "get_cluster_status": self._get_cluster_status,
            "get_cluster_resources": self._get_cluster_resources,
            # VM OPERATIONS (VMHandlerMixin)
            "list_vms": self._list_vms,
            "get_vm": self._get_vm,
            "get_vm_status": self._get_vm_status,
            "start_vm": self._start_vm,
            "stop_vm": self._stop_vm,
            "shutdown_vm": self._shutdown_vm,
            "restart_vm": self._restart_vm,
            "reset_vm": self._reset_vm,
            "suspend_vm": self._suspend_vm,
            "resume_vm": self._resume_vm,
            "get_vm_config": self._get_vm_config,
            "clone_vm": self._clone_vm,
            "migrate_vm": self._migrate_vm,
            # VM SNAPSHOT OPERATIONS
            "list_vm_snapshots": self._list_vm_snapshots,
            "create_vm_snapshot": self._create_vm_snapshot,
            "delete_vm_snapshot": self._delete_vm_snapshot,
            "rollback_vm_snapshot": self._rollback_vm_snapshot,
            # CONTAINER OPERATIONS (ContainerHandlerMixin)
            "list_containers": self._list_containers,
            "get_container": self._get_container,
            "get_container_status": self._get_container_status,
            "start_container": self._start_container,
            "stop_container": self._stop_container,
            "shutdown_container": self._shutdown_container,
            "restart_container": self._restart_container,
            "get_container_config": self._get_container_config,
            "clone_container": self._clone_container,
            "migrate_container": self._migrate_container,
            # CONTAINER SNAPSHOT OPERATIONS
            "list_container_snapshots": self._list_container_snapshots,
            "create_container_snapshot": self._create_container_snapshot,
            "delete_container_snapshot": self._delete_container_snapshot,
            "rollback_container_snapshot": self._rollback_container_snapshot,
            # STORAGE OPERATIONS (StorageHandlerMixin)
            "list_storage": self._list_storage,
            "get_storage": self._get_storage,
            "get_storage_content": self._get_storage_content,
            "get_storage_status": self._get_storage_status,
        }

        handler = handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                operation_id=operation_id,
            )

        try:
            if inspect.iscoroutinefunction(handler):
                data = await handler(parameters)
            else:
                data = await asyncio.to_thread(handler, parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"✅ {operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=data,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"❌ {operation_id} failed: {e}", exc_info=True)

            return OperationResult(
                success=False,
                error=str(e),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
