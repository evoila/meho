# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Connector using native async Azure SDKs (Phase 92).

Implements the BaseConnector interface using mixin pattern for organization.
Uses official Azure SDK `.aio` async clients for native async support.

Note: Unlike GCP (which uses asyncio.to_thread()), Azure SDK has first-class
async support via azure-mgmt-*.aio modules. No thread pool overhead.
"""

import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.handlers import (
    AKSHandlerMixin,
    ComputeHandlerMixin,
    MonitorHandlerMixin,
    NetworkHandlerMixin,
    StorageHandlerMixin,
    WebHandlerMixin,
)
from meho_app.modules.connectors.azure.operations import AZURE_OPERATIONS
from meho_app.modules.connectors.azure.types import AZURE_TYPES
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)

logger = get_logger(__name__)


class AzureConnector(
    BaseConnector,
    ComputeHandlerMixin,
    MonitorHandlerMixin,
    AKSHandlerMixin,
    NetworkHandlerMixin,
    StorageHandlerMixin,
    WebHandlerMixin,
):
    """Azure cloud connector using native async Azure SDKs.

    Provides native access to Azure for:
    - Compute: VM management, managed disks
    - Monitor: Metrics, alerts, activity log
    - AKS: Kubernetes cluster management, node pools
    - Networking: VNets, subnets, NSGs, load balancers
    - Storage: Storage accounts
    - Web: App Service, Function Apps

    Organization:
    - Compute operations: in compute_handlers.py
    - Monitor operations: in monitor_handlers.py
    - AKS operations: in aks_handlers.py
    - Network operations: in network_handlers.py
    - Storage operations: in storage_handlers.py
    - Web operations: in web_handlers.py

    Example:
        connector = AzureConnector(
            connector_id="abc123",
            config={
                "subscription_id": "00000000-0000-0000-0000-000000000000",
                "resource_group_filter": "my-rg",  # optional
            },
            credentials={
                "tenant_id": "00000000-0000-0000-0000-000000000000",
                "client_id": "00000000-0000-0000-0000-000000000000",
                "client_secret": "secret",
            },
        )

        async with connector:
            result = await connector.execute("list_vms", {})
            print(result.data)
    """

    def __init__(
        self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> None:
        super().__init__(connector_id, config, credentials)

        # Azure subscription and optional resource group filter
        self._subscription_id: str = config["subscription_id"]
        self._resource_group_filter: str | None = config.get("resource_group_filter")

        # Credential (initialized on connect)
        self._credential: Any = None

        # Management clients (initialized on connect, per D-10)
        self._compute_client: Any = None
        self._monitor_client: Any = None
        self._container_client: Any = None
        self._network_client: Any = None
        self._storage_client: Any = None
        self._web_client: Any = None
        self._resource_client: Any = None

    @property
    def subscription_id(self) -> str:
        """Get the Azure subscription ID."""
        return self._subscription_id

    @property
    def resource_group_filter(self) -> str | None:
        """Get the optional resource group filter."""
        return self._resource_group_filter

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """Connect to Azure and initialize async management clients.

        Creates a ClientSecretCredential then initializes 6 management clients
        plus 1 resource client, all sharing the same credential.

        Returns:
            True if connection successful.

        Raises:
            ImportError: If Azure SDK packages are not installed.
            ValueError: If required credentials are missing.
            Exception: If connection fails.
        """
        try:
            logger.info(f"Connecting to Azure subscription: {self._subscription_id}")

            # Validate required credentials
            tenant_id = self.credentials.get("tenant_id")
            client_id = self.credentials.get("client_id")
            client_secret = self.credentials.get("client_secret")

            if not all([tenant_id, client_id, client_secret]):
                raise ValueError(
                    "Azure credentials require tenant_id, client_id, and client_secret"
                )

            # Create async credential (lazy import)
            try:
                from azure.identity.aio import ClientSecretCredential
            except ImportError:
                raise ImportError(
                    "azure-identity is required for Azure connector. "
                    "Install with: pip install azure-identity azure-mgmt-compute "
                    "azure-mgmt-monitor azure-mgmt-containerservice "
                    "azure-mgmt-network azure-mgmt-storage azure-mgmt-web "
                    "azure-mgmt-resource"
                ) from None

            self._credential = ClientSecretCredential(
                tenant_id=str(tenant_id),
                client_id=str(client_id),
                client_secret=str(client_secret),
            )

            # Initialize management clients (all use native async .aio modules)
            self._initialize_clients()

            self._is_connected = True
            logger.info(f"Connected to Azure subscription: {self._subscription_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Azure: {e}", exc_info=True)
            raise

    def _initialize_clients(self) -> None:
        """Initialize Azure async management clients.

        All clients share the same credential and subscription ID.
        Uses lazy imports to avoid loading Azure SDK when not needed.
        """
        try:
            from azure.mgmt.compute.aio import ComputeManagementClient
            from azure.mgmt.containerservice.aio import ContainerServiceClient
            from azure.mgmt.monitor.aio import MonitorManagementClient
            from azure.mgmt.network.aio import NetworkManagementClient
            from azure.mgmt.resource.resources.aio import ResourceManagementClient
            from azure.mgmt.storage.aio import StorageManagementClient
            from azure.mgmt.web.aio import WebSiteManagementClient
        except ImportError as e:
            raise ImportError(
                f"Required Azure SDK packages not installed: {e}\n"
                "Install with: pip install azure-identity azure-mgmt-compute "
                "azure-mgmt-monitor azure-mgmt-containerservice "
                "azure-mgmt-network azure-mgmt-storage azure-mgmt-web "
                "azure-mgmt-resource"
            ) from e

        self._compute_client = ComputeManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._monitor_client = MonitorManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._container_client = ContainerServiceClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._network_client = NetworkManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._storage_client = StorageManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._web_client = WebSiteManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )
        self._resource_client = ResourceManagementClient(
            credential=self._credential,
            subscription_id=self._subscription_id,
        )

    async def disconnect(self) -> None:
        """Disconnect from Azure and close all clients.

        CRITICAL: Close management clients first, then close the credential last.
        Per D-10, the credential must outlive all clients that reference it.
        """
        # Close management clients first (order doesn't matter among them)
        clients = [
            self._compute_client,
            self._monitor_client,
            self._container_client,
            self._network_client,
            self._storage_client,
            self._web_client,
            self._resource_client,
        ]
        for client in clients:
            if client is not None:
                try:
                    await client.close()
                except Exception as e:
                    logger.warning(f"Error closing Azure client: {e}")

        # Close credential LAST (per D-10)
        if self._credential is not None:
            try:
                await self._credential.close()
            except Exception as e:
                logger.warning(f"Error closing Azure credential: {e}")

        # Clear references
        self._compute_client = None
        self._monitor_client = None
        self._container_client = None
        self._network_client = None
        self._storage_client = None
        self._web_client = None
        self._resource_client = None
        self._credential = None
        self._is_connected = False

        logger.info("Disconnected from Azure")

    async def test_connection(self) -> bool:
        """Test if connection is alive by listing 1 VM.

        Returns:
            True if connection is healthy.
        """
        try:
            if not self._is_connected or self._compute_client is None:
                return False

            # Try listing 1 VM to verify connectivity
            async for _vm in self._compute_client.virtual_machines.list_all():
                break  # Only need 1 to confirm connectivity

            logger.info(f"Azure connection test passed: subscription {self._subscription_id}")
            return True

        except Exception as e:
            logger.error(f"Azure connection test failed: {e}", exc_info=True)
            return False

    # =========================================================================
    # OPERATION EXECUTION
    # =========================================================================

    async def _execute_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> OperationResult:
        """Execute an Azure operation.

        Uses dynamic dispatch via getattr to route to handler methods.

        Args:
            operation_id: ID of the operation (e.g., "list_vms").
            parameters: Operation-specific parameters.

        Returns:
            OperationResult with success status and data/error.
        """
        if not self._is_connected:
            return OperationResult(
                success=False,
                error="Not connected to Azure",
                operation_id=operation_id,
            )

        start_time = time.time()

        try:
            # Find and execute the operation handler
            handler = getattr(self, f"_handle_{operation_id}", None)

            if handler is None:
                return OperationResult(
                    success=False,
                    error=f"Unknown operation: {operation_id}",
                    operation_id=operation_id,
                )

            # Execute handler
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_info = self._parse_azure_error(e, operation_id)
            logger.error(f"{operation_id} failed: {error_info['message']}", exc_info=True)
            return OperationResult(
                success=False,
                error=error_info["message"],
                error_code=error_info.get("code"),
                error_details=error_info.get("details"),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    # =========================================================================
    # ERROR HANDLING
    # =========================================================================

    def _parse_azure_error(self, error: Exception, operation_id: str) -> dict[str, Any]:
        """Parse Azure SDK exceptions into structured, actionable error information.

        Maps Azure exceptions to MEHO error codes per D-19.

        Args:
            error: The exception that was raised.
            operation_id: The operation that failed.

        Returns:
            Dict with 'message', 'code', and 'details' keys.
        """
        error_str = str(error)
        error_type = type(error).__name__

        try:
            from azure.core.exceptions import (
                ClientAuthenticationError,
                HttpResponseError,
                ResourceNotFoundError,
                ServiceRequestError,
            )

            # Authentication error
            if isinstance(error, ClientAuthenticationError):
                return {
                    "code": "PERMISSION_DENIED",
                    "message": (
                        f"Authentication failed for operation '{operation_id}'. "
                        f"Verify your service principal credentials (tenant_id, client_id, client_secret) "
                        f"are correct and not expired."
                    ),
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                        "raw_error": error_str[:500],
                    },
                }

            # Resource not found (404)
            if isinstance(error, ResourceNotFoundError):
                return {
                    "code": "NOT_FOUND",
                    "message": f"Resource not found for operation '{operation_id}'. {error_str}",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                    },
                }

            # HTTP response errors (covers throttling and other HTTP errors)
            if isinstance(error, HttpResponseError):
                status_code = getattr(error, "status_code", None)

                # Throttled (429)
                if status_code == 429:
                    return {
                        "code": "THROTTLED",
                        "message": (
                            f"Request throttled for operation '{operation_id}'. "
                            f"Azure API rate limit exceeded. Please try again later."
                        ),
                        "details": {
                            "error_type": error_type,
                            "operation": operation_id,
                            "status_code": 429,
                        },
                    }

                # Extract Azure error code if available
                azure_error_code = None
                if hasattr(error, "error") and error.error:
                    azure_error_code = getattr(error.error, "code", None)

                return {
                    "code": azure_error_code or "HTTP_ERROR",
                    "message": f"Azure API error for operation '{operation_id}': {error_str}",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                        "status_code": status_code,
                        "azure_error_code": azure_error_code,
                        "raw_error": error_str[:500],
                    },
                }

            # Service request error (connection issues)
            if isinstance(error, ServiceRequestError):
                return {
                    "code": "CONNECTION_ERROR",
                    "message": (
                        f"Connection error for operation '{operation_id}'. "
                        f"Cannot reach Azure API. Check network connectivity."
                    ),
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                        "raw_error": error_str[:500],
                    },
                }

        except ImportError:
            pass  # Azure exceptions not available

        # Default error handling
        return {
            "code": "UNKNOWN",
            "message": error_str,
            "details": {
                "error_type": error_type,
                "operation": operation_id,
            },
        }

    # =========================================================================
    # OPERATION & TYPE DEFINITIONS
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get all Azure operation definitions."""
        return AZURE_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get all Azure type definitions."""
        return AZURE_TYPES
