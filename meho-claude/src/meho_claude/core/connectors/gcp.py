"""GCP connector implementing BaseConnector via Google Cloud SDK.

Registered as "gcp" in the connector registry. Queries Compute Instances,
GKE Clusters, Cloud Monitoring Metrics, Cloud SQL instances, and VPC Networks
from a single GCP project.

Uses asyncio.to_thread() for all sync Google Cloud SDK calls.
Fresh credentials + SDK clients per execute() call (per-call pattern).
Auth: ADC primary, service account JSON fallback.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from google.auth import default as google_auth_default
from google.cloud import compute_v1, container_v1, monitoring_v3
from google.oauth2.service_account import Credentials as service_account_Credentials
from google.protobuf.json_format import MessageToDict
from googleapiclient import discovery

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import register_connector

# 9 READ operations across 5 GCP resource types
_GCP_OPERATIONS = [
    # Compute Engine
    {
        "operation_id": "compute-list-instances",
        "display_name": "List Compute Instances",
        "description": "List all Compute Engine instances across all zones",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["gcp", "compute", "instances"],
    },
    {
        "operation_id": "compute-get-instance",
        "display_name": "Get Compute Instance",
        "description": "Get details for a specific Compute Engine instance",
        "trust_tier": "READ",
        "input_schema": {
            "zone": {"type": "string", "required": True},
            "instance_name": {"type": "string", "required": True},
        },
        "tags": ["gcp", "compute", "instances"],
    },
    # GKE
    {
        "operation_id": "gke-list-clusters",
        "display_name": "List GKE Clusters",
        "description": "List all GKE clusters across all regions",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["gcp", "gke", "clusters"],
    },
    {
        "operation_id": "gke-get-cluster",
        "display_name": "Get GKE Cluster",
        "description": "Get details for a specific GKE cluster",
        "trust_tier": "READ",
        "input_schema": {
            "location": {"type": "string", "required": True},
            "cluster_name": {"type": "string", "required": True},
        },
        "tags": ["gcp", "gke", "clusters"],
    },
    # Cloud Monitoring
    {
        "operation_id": "monitoring-query-metrics",
        "display_name": "Query Cloud Monitoring Metrics",
        "description": "Query Cloud Monitoring time series with configurable metric type and time range",
        "trust_tier": "READ",
        "input_schema": {
            "metric_type": {"type": "string", "required": True},
            "resource_filter": {"type": "string", "required": False},
            "duration_minutes": {"type": "integer", "required": False},
        },
        "tags": ["gcp", "monitoring", "metrics"],
    },
    # Cloud SQL
    {
        "operation_id": "cloudsql-list-instances",
        "display_name": "List Cloud SQL Instances",
        "description": "List all Cloud SQL instances in the project",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["gcp", "cloudsql", "databases"],
    },
    {
        "operation_id": "cloudsql-get-instance",
        "display_name": "Get Cloud SQL Instance",
        "description": "Get details for a specific Cloud SQL instance",
        "trust_tier": "READ",
        "input_schema": {
            "instance_name": {"type": "string", "required": True},
        },
        "tags": ["gcp", "cloudsql", "databases"],
    },
    # VPC
    {
        "operation_id": "vpc-list-networks",
        "display_name": "List VPC Networks",
        "description": "List all VPC networks in the project",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["gcp", "vpc", "networks"],
    },
    {
        "operation_id": "vpc-list-subnetworks",
        "display_name": "List VPC Subnetworks",
        "description": "List all VPC subnetworks across all regions",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["gcp", "vpc", "subnetworks"],
    },
]


@register_connector("gcp")
class GCPConnector(BaseConnector):
    """GCP connector querying 5 resource types via Google Cloud SDK.

    Uses ADC (Application Default Credentials) primary with service account
    JSON fallback. Fresh credentials + SDK clients per execute() call.
    All synchronous SDK calls wrapped in asyncio.to_thread().
    """

    def __init__(self, config: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config, credentials)

    def _get_credentials(self):
        """Get Google Cloud credentials.

        Uses service_account_path if set in config, otherwise falls back
        to Application Default Credentials (ADC).

        Returns:
            google.auth.credentials.Credentials instance.
        """
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]

        if self.config.service_account_path:
            return service_account_Credentials.from_service_account_file(
                self.config.service_account_path,
                scopes=scopes,
            )

        creds, _ = google_auth_default(scopes=scopes)
        return creds

    def _get_project_id(self) -> str:
        """Return the GCP project ID from config.

        Raises:
            ValueError: If config.project_id is None.
        """
        if not self.config.project_id:
            raise ValueError(
                f"GCP connector '{self.config.name}' requires project_id in config"
            )
        return self.config.project_id

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by verifying ADC auth against Compute Engine.

        Uses aggregated_list with a minimal request to verify credentials work.

        Returns:
            Dict with status and project on success,
            or status and error message on failure.
        """
        try:
            project_id = self._get_project_id()

            def _test() -> dict[str, Any]:
                creds = self._get_credentials()
                client = compute_v1.InstancesClient(credentials=creds)
                request = compute_v1.AggregatedListInstancesRequest(
                    project=project_id,
                    max_results=1,
                )
                # Just iterate to trigger the API call
                for _ in client.aggregated_list(request=request):
                    break
                return {"status": "ok", "project": project_id}

            return await asyncio.to_thread(_test)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def discover_operations(self) -> list[Operation]:
        """Return hardcoded operations for all 5 GCP resource types.

        All 9 operations are READ tier.
        """
        operations = []
        for op_def in _GCP_OPERATIONS:
            operations.append(
                Operation(
                    connector_name=self.config.name,
                    operation_id=op_def["operation_id"],
                    display_name=op_def["display_name"],
                    description=op_def["description"],
                    trust_tier=op_def["trust_tier"],
                    input_schema=op_def["input_schema"],
                    tags=op_def["tags"],
                )
            )
        return operations

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a GCP operation, routing by operation_id prefix.

        Creates fresh credentials + SDK clients per call.
        All SDK calls wrapped in asyncio.to_thread().
        """
        project_id = self._get_project_id()
        op_id = operation.operation_id

        if op_id == "compute-list-instances":
            return await asyncio.to_thread(self._exec_compute_list_instances, project_id)
        elif op_id == "compute-get-instance":
            return await asyncio.to_thread(
                self._exec_compute_get_instance, project_id,
                params["zone"], params["instance_name"],
            )
        elif op_id == "gke-list-clusters":
            return await asyncio.to_thread(self._exec_gke_list_clusters, project_id)
        elif op_id == "gke-get-cluster":
            return await asyncio.to_thread(
                self._exec_gke_get_cluster, project_id,
                params["location"], params["cluster_name"],
            )
        elif op_id == "monitoring-query-metrics":
            return await asyncio.to_thread(
                self._exec_monitoring_query, project_id, params,
            )
        elif op_id == "cloudsql-list-instances":
            return await asyncio.to_thread(self._exec_cloudsql_list, project_id)
        elif op_id == "cloudsql-get-instance":
            return await asyncio.to_thread(
                self._exec_cloudsql_get, project_id, params["instance_name"],
            )
        elif op_id == "vpc-list-networks":
            return await asyncio.to_thread(self._exec_vpc_list_networks, project_id)
        elif op_id == "vpc-list-subnetworks":
            return await asyncio.to_thread(self._exec_vpc_list_subnetworks, project_id)
        else:
            raise ValueError(f"Unknown operation: {op_id}")

    # --- Compute Engine ---

    def _exec_compute_list_instances(self, project_id: str) -> dict[str, Any]:
        """List all Compute Engine instances across all zones."""
        creds = self._get_credentials()
        client = compute_v1.InstancesClient(credentials=creds)
        request = compute_v1.AggregatedListInstancesRequest(project=project_id)

        instances = []
        for zone_name, scoped_list in client.aggregated_list(request=request):
            if scoped_list.instances:
                for instance in scoped_list.instances:
                    instances.append(self._proto_to_dict(instance))

        return {"data": instances}

    def _exec_compute_get_instance(
        self, project_id: str, zone: str, instance_name: str,
    ) -> dict[str, Any]:
        """Get details for a specific Compute Engine instance."""
        creds = self._get_credentials()
        client = compute_v1.InstancesClient(credentials=creds)
        instance = client.get(project=project_id, zone=zone, instance=instance_name)
        return {"data": self._proto_to_dict(instance)}

    # --- GKE ---

    def _exec_gke_list_clusters(self, project_id: str) -> dict[str, Any]:
        """List all GKE clusters across all regions."""
        creds = self._get_credentials()
        client = container_v1.ClusterManagerClient(credentials=creds)
        response = client.list_clusters(
            request={"parent": f"projects/{project_id}/locations/-"}
        )
        clusters = [self._proto_to_dict(c) for c in response.clusters]
        return {"data": clusters}

    def _exec_gke_get_cluster(
        self, project_id: str, location: str, cluster_name: str,
    ) -> dict[str, Any]:
        """Get details for a specific GKE cluster."""
        creds = self._get_credentials()
        client = container_v1.ClusterManagerClient(credentials=creds)
        cluster = client.get_cluster(
            name=f"projects/{project_id}/locations/{location}/clusters/{cluster_name}"
        )
        return {"data": self._proto_to_dict(cluster)}

    # --- Cloud Monitoring ---

    def _exec_monitoring_query(
        self, project_id: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query Cloud Monitoring time series data.

        Constructs TimeInterval with seconds-based timestamps.
        Default duration is 60 minutes.
        """
        creds = self._get_credentials()
        client = monitoring_v3.MetricServiceClient(credentials=creds)

        metric_type = params["metric_type"]
        resource_filter = params.get("resource_filter", "")
        duration_minutes = params.get("duration_minutes", 60)

        now = time.time()
        seconds = int(now)
        nanos = int((now - seconds) * 1e9)

        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": seconds, "nanos": nanos},
            start_time={
                "seconds": int(seconds - duration_minutes * 60),
                "nanos": 0,
            },
        )

        filter_str = f'metric.type = "{metric_type}"'
        if resource_filter:
            filter_str += f" AND {resource_filter}"

        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": filter_str,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )

        time_series = [self._serialize_time_series(ts) for ts in results]
        return {"data": time_series}

    def _serialize_time_series(self, ts: Any) -> dict[str, Any]:
        """Serialize a monitoring TimeSeries to a plain dict."""
        try:
            return MessageToDict(ts._pb)
        except Exception:
            # Fallback: extract key fields manually
            result: dict[str, Any] = {}
            try:
                result["metric"] = MessageToDict(ts.metric._pb) if hasattr(ts.metric, "_pb") else str(ts.metric)
            except Exception:
                result["metric"] = str(ts.metric)
            try:
                result["resource"] = MessageToDict(ts.resource._pb) if hasattr(ts.resource, "_pb") else str(ts.resource)
            except Exception:
                result["resource"] = str(ts.resource)
            result["points"] = []
            try:
                for point in ts.points:
                    result["points"].append(MessageToDict(point._pb) if hasattr(point, "_pb") else str(point))
            except Exception:
                pass
            return result

    # --- Cloud SQL (Discovery API) ---

    def _exec_cloudsql_list(self, project_id: str) -> dict[str, Any]:
        """List all Cloud SQL instances using the Discovery API."""
        creds = self._get_credentials()
        service = discovery.build("sqladmin", "v1beta4", credentials=creds, cache_discovery=False)
        response = service.instances().list(project=project_id).execute()
        items = response.get("items", [])
        return {"data": items}

    def _exec_cloudsql_get(self, project_id: str, instance_name: str) -> dict[str, Any]:
        """Get details for a specific Cloud SQL instance."""
        creds = self._get_credentials()
        service = discovery.build("sqladmin", "v1beta4", credentials=creds, cache_discovery=False)
        instance = service.instances().get(project=project_id, instance=instance_name).execute()
        return {"data": instance}

    # --- VPC ---

    def _exec_vpc_list_networks(self, project_id: str) -> dict[str, Any]:
        """List all VPC networks."""
        creds = self._get_credentials()
        client = compute_v1.NetworksClient(credentials=creds)
        networks = [self._proto_to_dict(n) for n in client.list(project=project_id)]
        return {"data": networks}

    def _exec_vpc_list_subnetworks(self, project_id: str) -> dict[str, Any]:
        """List all VPC subnetworks across all regions."""
        creds = self._get_credentials()
        client = compute_v1.SubnetworksClient(credentials=creds)
        request = compute_v1.AggregatedListSubnetworksRequest(project=project_id)

        subnets = []
        for region_name, scoped_list in client.aggregated_list(request=request):
            if scoped_list.subnetworks:
                for subnet in scoped_list.subnetworks:
                    subnets.append(self._proto_to_dict(subnet))

        return {"data": subnets}

    # --- Serialization ---

    def _proto_to_dict(self, proto_obj: Any) -> dict[str, Any]:
        """Serialize a protobuf object to a plain dict.

        Uses MessageToDict for protobuf objects (those with DESCRIPTOR).
        Falls back to returning dict as-is or converting via str for others.
        """
        if isinstance(proto_obj, dict):
            return proto_obj

        if hasattr(proto_obj, "DESCRIPTOR"):
            return MessageToDict(proto_obj._pb if hasattr(proto_obj, "_pb") else proto_obj)

        # Non-protobuf: try to extract attributes
        if hasattr(proto_obj, "__dict__"):
            result = {}
            for key, val in proto_obj.__dict__.items():
                if not key.startswith("_"):
                    result[key] = val
            return result if result else {"value": str(proto_obj)}

        return {"value": str(proto_obj)}

    # --- Trust tier ---

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first."""
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """No-op -- GCP clients created per-execute call."""
        pass
