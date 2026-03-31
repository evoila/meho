# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Connector using Google Cloud Python SDKs (TASK-102)

Implements the BaseConnector interface using mixin pattern for organization.
Uses official Google Cloud client libraries for native API access.

Note: GCP SDK is synchronous, so we use asyncio.to_thread() to avoid blocking.
"""

import asyncio
import base64
import json
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
from meho_app.modules.connectors.gcp.handlers import (
    ArtifactRegistryHandlerMixin,
    CloudBuildHandlerMixin,
    ComputeHandlerMixin,
    GKEHandlerMixin,
    MonitoringHandlerMixin,
    NetworkHandlerMixin,
)
from meho_app.modules.connectors.gcp.operations import GCP_OPERATIONS
from meho_app.modules.connectors.gcp.types import GCP_TYPES

logger = get_logger(__name__)


class GCPConnector(
    BaseConnector,
    ComputeHandlerMixin,
    GKEHandlerMixin,
    NetworkHandlerMixin,
    MonitoringHandlerMixin,
    CloudBuildHandlerMixin,
    ArtifactRegistryHandlerMixin,
):
    """
    Google Cloud Platform connector using official SDKs.

    Provides native access to GCP for:
    - Compute Engine: VM management (list, power on/off, details)
    - GKE: Kubernetes cluster management
    - Networking: VPC, subnets, firewalls
    - Cloud Monitoring: Metrics and alerts
    - Cloud Build: Build listing, details, logs, triggers, cancel/retry
    - Artifact Registry: Repository listing, Docker image browsing with version history

    Organization:
    - Compute operations: in compute_handlers.py
    - GKE operations: in gke_handlers.py
    - Network operations: in network_handlers.py
    - Monitoring operations: in monitoring_handlers.py
    - Cloud Build operations: in cloud_build_handlers.py
    - Artifact Registry operations: in artifact_registry_handlers.py

    Example:
        connector = GCPConnector(
            connector_id="abc123",
            config={
                "project_id": "my-gcp-project",
                "default_region": "us-central1",
                "default_zone": "us-central1-a",
            },
            credentials={
                "service_account_json": '{"type": "service_account", ...}',
                # OR base64 encoded:
                # "service_account_json_base64": "eyJ0eXBlIjoi..."
            }
        )

        async with connector:
            result = await connector.execute("list_instances", {})
            print(result.data)
    """

    def __init__(self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]):
        super().__init__(connector_id, config, credentials)

        # GCP clients (initialized on connect)
        self._credentials: Any = None
        self._compute_client: Any = None
        self._instances_client: Any = None
        self._disks_client: Any = None
        self._snapshots_client: Any = None
        self._networks_client: Any = None
        self._subnetworks_client: Any = None
        self._firewalls_client: Any = None
        self._container_client: Any = None
        self._monitoring_client: Any = None
        self._alert_policy_client: Any = None
        self._cloud_build_client: Any = None
        self._artifact_registry_client: Any = None
        self._storage_client: Any = None  # For reading build logs from Cloud Storage

        # Configuration
        self._project_id = config.get("project_id")
        self._default_region = config.get("default_region", "us-central1")
        self._default_zone = config.get("default_zone", "us-central1-a")
        self._all_zones = config.get("all_zones", False)

    @property
    def project_id(self) -> str:
        """Get the GCP project ID."""
        return self._project_id or ""

    @property
    def default_region(self) -> str:
        """Get the default region."""
        return self._default_region

    @property
    def default_zone(self) -> str:
        """Get the default zone."""
        return self._default_zone

    # =========================================================================
    # AUTHENTICATION
    # =========================================================================

    def _get_credentials(self) -> Any:
        """
        Get Google Cloud credentials from stored credentials.

        Supports:
        - service_account_json: Raw JSON string
        - service_account_json_base64: Base64-encoded JSON

        Returns:
            google.oauth2.service_account.Credentials
        """
        try:
            from google.oauth2 import service_account
        except ImportError:
            raise ImportError(
                "google-auth is required for GCP connector. "
                "Install with: pip install google-auth google-cloud-compute "
                "google-cloud-container google-cloud-monitoring"
            ) from None

        # Try raw JSON first
        sa_json = self.credentials.get("service_account_json")
        if sa_json:
            sa_info = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
            return service_account.Credentials.from_service_account_info(sa_info)

        # Try base64-encoded JSON
        sa_json_b64 = self.credentials.get("service_account_json_base64")
        if sa_json_b64:
            sa_json_str = base64.b64decode(sa_json_b64).decode("utf-8")
            sa_info = json.loads(sa_json_str)
            return service_account.Credentials.from_service_account_info(sa_info)

        # No credentials provided - try Application Default Credentials
        try:
            import google.auth

            credentials, project = google.auth.default()
            if not self._project_id and project:
                self._project_id = project
            return credentials
        except Exception as e:
            raise ValueError(
                "No GCP credentials provided. Please provide either:\n"
                "- service_account_json: Raw JSON key file content\n"
                "- service_account_json_base64: Base64-encoded JSON key\n"
                f"Application Default Credentials also failed: {e}"
            ) from e

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """Connect to GCP and initialize clients."""
        try:
            logger.info(f"🔌 Connecting to GCP project: {self._project_id or 'unknown'}")

            # Get credentials (this parses JSON, doesn't make network calls)
            self._credentials = self._get_credentials()

            if not self._project_id:
                raise ValueError("project_id is required in connector config")

            # Initialize clients in a thread to avoid blocking
            # (client constructors may make network calls for auth)
            # Note: GCP SDK can be slow on first call (~45-50s) due to cold start
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._initialize_clients),
                    timeout=60.0,  # 60 second timeout (GCP SDK can be slow)
                )
            except TimeoutError:
                logger.error("❌ GCP client initialization timed out after 60 seconds")
                raise TimeoutError("GCP client initialization timed out") from None

            self._is_connected = True
            logger.info(f"✅ Connected to GCP project: {self._project_id}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to connect to GCP: {e}", exc_info=True)
            raise

    def _initialize_clients(self) -> None:
        """Initialize GCP clients (runs in thread pool)."""
        try:
            from google.cloud import (
                artifactregistry_v1,
                compute_v1,
                container_v1,
                monitoring_v3,
                storage,
            )
            from google.cloud.devtools import cloudbuild_v1
        except ImportError as e:
            raise ImportError(
                f"Required GCP SDK packages not installed: {e}\n"
                "Install with: pip install google-cloud-compute "
                "google-cloud-container google-cloud-monitoring "
                "google-cloud-build google-cloud-artifact-registry "
                "google-cloud-storage"
            ) from e

        # Compute Engine clients
        self._instances_client = compute_v1.InstancesClient(credentials=self._credentials)
        self._disks_client = compute_v1.DisksClient(credentials=self._credentials)
        self._snapshots_client = compute_v1.SnapshotsClient(credentials=self._credentials)

        # Network clients
        self._networks_client = compute_v1.NetworksClient(credentials=self._credentials)
        self._subnetworks_client = compute_v1.SubnetworksClient(credentials=self._credentials)
        self._firewalls_client = compute_v1.FirewallsClient(credentials=self._credentials)

        # GKE client
        self._container_client = container_v1.ClusterManagerClient(credentials=self._credentials)

        # Monitoring clients
        self._monitoring_client = monitoring_v3.MetricServiceClient(credentials=self._credentials)
        self._alert_policy_client = monitoring_v3.AlertPolicyServiceClient(
            credentials=self._credentials
        )

        # Cloud Build client
        self._cloud_build_client = cloudbuild_v1.CloudBuildClient(credentials=self._credentials)

        # Artifact Registry client
        self._artifact_registry_client = artifactregistry_v1.ArtifactRegistryClient(
            credentials=self._credentials
        )

        # Cloud Storage client (for reading build logs)
        self._storage_client = storage.Client(credentials=self._credentials)

    async def disconnect(self) -> None:
        """Disconnect from GCP (cleanup clients)."""
        # GCP clients don't require explicit disconnection
        # but we clear references for garbage collection
        self._credentials = None
        self._instances_client = None
        self._disks_client = None
        self._snapshots_client = None
        self._networks_client = None
        self._subnetworks_client = None
        self._firewalls_client = None
        self._container_client = None
        self._monitoring_client = None
        self._alert_policy_client = None
        self._cloud_build_client = None
        self._artifact_registry_client = None
        self._storage_client = None
        self._is_connected = False
        logger.info("🔌 Disconnected from GCP")

    async def test_connection(self) -> bool:
        """Test if connection is alive by listing zones."""
        try:
            if not self._is_connected:
                return False

            # Run the synchronous GCP SDK call in a thread with timeout
            # Note: GCP SDK can be slow on first call (~45-50s) due to cold start
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._test_connection_sync),
                    timeout=60.0,  # 60 second timeout (GCP SDK can be slow)
                )
            except TimeoutError:
                logger.error("❌ GCP connection test timed out after 60 seconds")
                return False

        except Exception as e:
            logger.error(f"❌ GCP connection test failed: {e}", exc_info=True)
            return False

    def _test_connection_sync(self) -> bool:
        """Synchronous connection test (runs in thread pool)."""
        try:
            from google.cloud import compute_v1
        except ImportError as e:
            logger.error(f"GCP SDK not installed: {e}")
            raise ImportError(
                "google-cloud-compute is required. Install with: pip install google-cloud-compute"
            ) from e

        try:
            zones_client = compute_v1.ZonesClient(credentials=self._credentials)

            # Just get one zone to verify connectivity
            request = compute_v1.ListZonesRequest(
                project=self._project_id,
                max_results=1,
            )
            zones = list(zones_client.list(request=request))
            if len(zones) > 0:
                logger.info(f"✅ GCP connection test passed: project {self._project_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ GCP sync connection test failed: {e}", exc_info=True)
            return False

    # =========================================================================
    # OPERATION EXECUTION
    # =========================================================================

    async def execute(self, operation_id: str, parameters: dict[str, Any]) -> OperationResult:
        """
        Execute a GCP operation.

        Args:
            operation_id: ID of the operation (e.g., "list_instances")
            parameters: Operation-specific parameters

        Returns:
            OperationResult with success status and data/error
        """
        if not self._is_connected:
            return OperationResult(
                success=False,
                error="Not connected to GCP",
                operation_id=operation_id,
            )

        start_time = time.time()

        try:
            # Find and execute the operation handler
            handler_name = f"_handle_{operation_id}"
            handler = getattr(self, handler_name, None)

            if handler is None:
                return OperationResult(
                    success=False,
                    error=f"Unknown operation: {operation_id}",
                    operation_id=operation_id,
                )

            # Execute handler
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"✅ {operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_info = self._parse_gcp_error(e, operation_id)
            logger.error(f"❌ {operation_id} failed: {error_info['message']}", exc_info=True)
            return OperationResult(
                success=False,
                error=error_info["message"],
                error_code=error_info.get("code"),
                error_details=error_info.get("details"),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _parse_gcp_error(self, error: Exception, operation_id: str) -> dict[str, Any]:
        """
        Parse GCP API errors into structured, actionable error information.

        Specifically handles permission errors to help users understand
        what IAM permissions are missing.

        Args:
            error: The exception that was raised
            operation_id: The operation that failed

        Returns:
            Dict with 'message', 'code', and 'details' keys
        """
        error_str = str(error)
        error_type = type(error).__name__

        # Try to import GCP exception types
        try:
            from google.api_core import exceptions as gcp_exceptions

            # Permission denied (403)
            if isinstance(error, (gcp_exceptions.Forbidden, gcp_exceptions.PermissionDenied)):
                # Extract the missing permission from the error message
                missing_permission = self._extract_missing_permission(error_str, operation_id)
                return {
                    "code": "PERMISSION_DENIED",
                    "message": (
                        f"Permission denied for operation '{operation_id}'. "
                        f"The service account lacks required permissions. "
                        f"{missing_permission}"
                    ),
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                        "suggestion": missing_permission,
                        "raw_error": error_str[:500],  # Truncate long errors
                    },
                }

            # Not found (404) - resource doesn't exist
            if isinstance(error, gcp_exceptions.NotFound):
                return {
                    "code": "NOT_FOUND",
                    "message": f"Resource not found for operation '{operation_id}'. {error_str}",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                    },
                }

            # Invalid argument (400)
            if isinstance(error, gcp_exceptions.InvalidArgument):
                return {
                    "code": "INVALID_ARGUMENT",
                    "message": f"Invalid parameters for operation '{operation_id}'. {error_str}",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                    },
                }

            # Quota exceeded (429)
            if isinstance(error, gcp_exceptions.ResourceExhausted):
                return {
                    "code": "QUOTA_EXCEEDED",
                    "message": f"Quota exceeded for operation '{operation_id}'. Please try again later or request a quota increase.",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                    },
                }

            # Unauthenticated (401)
            if isinstance(error, gcp_exceptions.Unauthenticated):
                return {
                    "code": "UNAUTHENTICATED",
                    "message": "Authentication failed. Please verify your service account credentials are valid and not expired.",
                    "details": {
                        "error_type": error_type,
                        "operation": operation_id,
                    },
                }

        except ImportError:
            pass  # GCP exceptions not available

        # Default error handling
        return {
            "code": "UNKNOWN",
            "message": error_str,
            "details": {
                "error_type": error_type,
                "operation": operation_id,
            },
        }

    def _extract_missing_permission(self, error_str: str, operation_id: str) -> str:
        """
        Extract and suggest the missing IAM permission from a GCP error.

        Args:
            error_str: The error message string
            operation_id: The operation that failed

        Returns:
            Human-readable suggestion for fixing the permission issue
        """
        # Common permission patterns in GCP error messages
        import re

        # Try to extract permission from error message
        # GCP errors often contain: "requires permission 'compute.instances.list'"
        permission_match = re.search(
            r"permission[s]?\s*['\"]?([a-zA-Z0-9._]+)['\"]?", error_str, re.IGNORECASE
        )
        if permission_match:
            permission = permission_match.group(1)
            return f"Required permission: '{permission}'. Add this permission to your service account's IAM role."

        # Map operations to likely required permissions
        permission_hints = {
            "list_instances": "compute.instances.list (roles/compute.viewer)",
            "get_instance": "compute.instances.get (roles/compute.viewer)",
            "start_instance": "compute.instances.start (roles/compute.instanceAdmin)",
            "stop_instance": "compute.instances.stop (roles/compute.instanceAdmin)",
            "reset_instance": "compute.instances.reset (roles/compute.instanceAdmin)",
            "list_disks": "compute.disks.list (roles/compute.viewer)",
            "list_snapshots": "compute.snapshots.list (roles/compute.viewer)",
            "create_snapshot": "compute.snapshots.create (roles/compute.storageAdmin)",
            "list_clusters": "container.clusters.list (roles/container.viewer)",
            "get_cluster": "container.clusters.get (roles/container.viewer)",
            "list_networks": "compute.networks.list (roles/compute.networkViewer)",
            "list_firewalls": "compute.firewalls.list (roles/compute.networkViewer)",
            "get_time_series": "monitoring.timeSeries.list (roles/monitoring.viewer)",
            "list_alert_policies": "monitoring.alertPolicies.list (roles/monitoring.viewer)",
            "list_builds": "cloudbuild.builds.list (roles/cloudbuild.builds.viewer)",
            "get_build": "cloudbuild.builds.get (roles/cloudbuild.builds.viewer)",
            "list_build_triggers": "cloudbuild.builds.list (roles/cloudbuild.builds.viewer)",
            "get_build_logs": "storage.objects.get (roles/storage.objectViewer on logs bucket)",
            "cancel_build": "cloudbuild.builds.update (roles/cloudbuild.builds.editor)",
            "retry_build": "cloudbuild.builds.create (roles/cloudbuild.builds.editor)",
            "list_artifact_repositories": "artifactregistry.repositories.list (roles/artifactregistry.reader)",
            "list_docker_images": "artifactregistry.repositories.get + artifactregistry.dockerImages.list (roles/artifactregistry.reader)",
        }

        if operation_id in permission_hints:
            hint = permission_hints[operation_id]
            return f"Likely required: {hint}"

        return "Check that your service account has the appropriate IAM roles for this operation."

    # =========================================================================
    # OPERATION & TYPE DEFINITIONS
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get all GCP operation definitions."""
        return GCP_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get all GCP type definitions."""
        return GCP_TYPES
