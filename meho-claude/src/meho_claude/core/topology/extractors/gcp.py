"""GCP entity extractor for topology auto-discovery.

Extracts Compute Instances, GKE Clusters, Cloud SQL Instances, and VPC Networks
from GCP API results into TopologyEntity models.

Registered as "gcp" in the extractor registry via @register_extractor.

CRITICAL: GKE cluster entities store the endpoint IP in raw_attributes.ip_address
which enables the CorrelationEngine to auto-create SAME_AS correlations with
K8s connectors that share the same API server IP. Compute instances store
ip_address and hostname for cross-system correlation with VMware VMs.
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_claude.core.topology.extractor import BaseEntityExtractor, register_extractor
from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyEntity,
    TopologyRelationship,
)

logger = structlog.get_logger()

# Operation IDs that this extractor handles.
# Monitoring (time-series data, not entities) and get-* operations are NOT extractable.
_EXTRACTABLE_OPERATIONS = {
    "compute-list-instances",
    "gke-list-clusters",
    "cloudsql-list-instances",
    "vpc-list-networks",
}


@register_extractor("gcp")
class GCPEntityExtractor(BaseEntityExtractor):
    """Extract topology entities from GCP API responses.

    Handles 4 resource types: Compute Instances, GKE Clusters,
    Cloud SQL Instances, VPC Networks. Unknown and non-extractable
    operations return empty results.
    """

    def extract(
        self,
        connector_name: str,
        connector_type: str,
        operation_id: str,
        result_data: dict[str, Any],
    ) -> ExtractionResult:
        """Extract entities from GCP API result data.

        Routes to type-specific extraction methods based on operation_id.
        Non-extractable operations and missing data return empty ExtractionResult.
        """
        entities: list[TopologyEntity] = []
        relationships: list[TopologyRelationship] = []

        # Get items from result structure
        items = result_data.get("data", [])
        if not items or not isinstance(items, list):
            return ExtractionResult(
                entities=[],
                relationships=[],
                source_connector=connector_name,
                source_operation=operation_id,
            )

        if operation_id == "compute-list-instances":
            self._extract_instances(connector_name, connector_type, items, entities)
        elif operation_id == "gke-list-clusters":
            self._extract_gke_clusters(connector_name, connector_type, items, entities)
        elif operation_id == "cloudsql-list-instances":
            self._extract_sql_instances(connector_name, connector_type, items, entities)
        elif operation_id == "vpc-list-networks":
            self._extract_vpc_networks(connector_name, connector_type, items, entities)
        # Non-extractable operations (monitoring, get-*, etc.): return empty result

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_connector=connector_name,
            source_operation=operation_id,
        )

    def _extract_instances(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
    ) -> None:
        """Extract Compute Engine instances.

        CRITICAL: ip_address and hostname enable CorrelationEngine matching
        with VMware VMs and K8s nodes.
        """
        for item in items:
            try:
                name = item.get("name")
                if not name:
                    continue

                # Extract IP from networkInterfaces
                network_interfaces = item.get("networkInterfaces", [])
                ip_address = ""
                external_ip = ""

                if network_interfaces:
                    first_nic = network_interfaces[0]
                    ip_address = first_nic.get("networkIP") or ""
                    access_configs = first_nic.get("accessConfigs", [])
                    if access_configs:
                        external_ip = access_configs[0].get("natIP") or ""

                # hostname defaults to instance name
                hostname = name

                instance_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="gcp_instance",
                    connector_type=connector_type,
                    canonical_id=f"gcp:{connector_name}:instance:{name}",
                    description=f"GCP Compute instance {name}",
                    raw_attributes={
                        "name": name,
                        "zone": item.get("zone", ""),
                        "machine_type": item.get("machineType", ""),
                        "status": item.get("status", ""),
                        "ip_address": ip_address,
                        "external_ip": external_ip,
                        "hostname": hostname,
                    },
                )
                entities.append(instance_entity)

            except Exception:
                logger.warning(
                    "gcp_instance_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_gke_clusters(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
    ) -> None:
        """Extract GKE cluster entities.

        CRITICAL: The endpoint IP stored in ip_address enables CorrelationEngine
        to match this GKE cluster with a K8s connector that has the same API
        server IP. This is how GKE <-> K8s SAME_AS works.
        """
        for item in items:
            try:
                name = item.get("name")
                if not name:
                    continue

                endpoint = item.get("endpoint") or ""
                node_count = item.get("currentNodeCount") or item.get("initialNodeCount") or 0

                cluster_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="gcp_gke_cluster",
                    connector_type=connector_type,
                    canonical_id=f"gcp:{connector_name}:gke:{name}",
                    description=f"GKE cluster {name}",
                    raw_attributes={
                        "name": name,
                        "location": item.get("location", ""),
                        "status": item.get("status", ""),
                        "endpoint": endpoint,
                        "node_count": node_count,
                        "ip_address": endpoint,  # For SAME_AS correlation with K8s
                    },
                )
                entities.append(cluster_entity)

            except Exception:
                logger.warning(
                    "gcp_gke_cluster_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_sql_instances(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
    ) -> None:
        """Extract Cloud SQL instances."""
        for item in items:
            try:
                name = item.get("name")
                if not name:
                    continue

                # Extract primary IP from ipAddresses list
                ip_address = ""
                ip_addresses = item.get("ipAddresses", [])
                if ip_addresses:
                    # Use first IP (typically PRIMARY)
                    ip_address = ip_addresses[0].get("ipAddress") or ""

                sql_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="gcp_sql_instance",
                    connector_type=connector_type,
                    canonical_id=f"gcp:{connector_name}:sql:{name}",
                    description=f"Cloud SQL instance {name}",
                    raw_attributes={
                        "name": name,
                        "database_version": item.get("databaseVersion", ""),
                        "region": item.get("region", ""),
                        "state": item.get("state", ""),
                        "ip_address": ip_address,
                    },
                )
                entities.append(sql_entity)

            except Exception:
                logger.warning(
                    "gcp_sql_instance_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_vpc_networks(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
    ) -> None:
        """Extract VPC network entities."""
        for item in items:
            try:
                name = item.get("name")
                if not name:
                    continue

                vpc_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="gcp_vpc_network",
                    connector_type=connector_type,
                    canonical_id=f"gcp:{connector_name}:vpc:{name}",
                    description=f"GCP VPC network {name}",
                    raw_attributes={
                        "name": name,
                        "auto_create_subnetworks": item.get("autoCreateSubnetworks", False),
                        "routing_config": item.get("routingConfig", {}),
                    },
                )
                entities.append(vpc_entity)

            except Exception:
                logger.warning(
                    "gcp_vpc_network_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )
