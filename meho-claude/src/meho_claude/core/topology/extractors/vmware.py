"""VMware entity extractor for topology auto-discovery.

Extracts VMs, Hosts, Clusters, Datastores, and Networks from VMware
PropertyCollector results into TopologyEntity models.

Registered as "vmware" in the extractor registry via @register_extractor.

CRITICAL: VM entities store provider_id as "vsphere://<lowercase-instanceUuid>"
which must match the K8s node provider_id format exactly for the CorrelationEngine
to auto-create SAME_AS correlations at confidence 1.0.
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

# Operation IDs that this extractor handles
_EXTRACTABLE_OPERATIONS = {
    "list-vms",
    "list-hosts",
    "list-clusters",
    "list-datastores",
    "list-networks",
}


@register_extractor("vmware")
class VMwareEntityExtractor(BaseEntityExtractor):
    """Extract topology entities from VMware PropertyCollector responses.

    Handles all 5 object types: VMs, Hosts, Clusters, Datastores, Networks.
    Unknown operations return empty results.
    """

    def extract(
        self,
        connector_name: str,
        connector_type: str,
        operation_id: str,
        result_data: dict[str, Any],
    ) -> ExtractionResult:
        """Extract entities and relationships from VMware PropertyCollector data.

        Routes to type-specific extraction methods based on operation_id.
        Unknown operations and missing data return empty ExtractionResult.
        """
        entities: list[TopologyEntity] = []
        relationships: list[TopologyRelationship] = []

        # Get items from PropertyCollector result structure
        items = result_data.get("data", [])
        if not items or not isinstance(items, list):
            return ExtractionResult(
                entities=[],
                relationships=[],
                source_connector=connector_name,
                source_operation=operation_id,
            )

        if operation_id == "list-vms":
            self._extract_vms(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-hosts":
            self._extract_hosts(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-clusters":
            self._extract_clusters(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-datastores":
            self._extract_datastores(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-networks":
            self._extract_networks(connector_name, connector_type, items, entities, relationships)
        # Unknown operations: return empty result (no crash)

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_connector=connector_name,
            source_operation=operation_id,
        )

    def _extract_vms(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract VM entities with provider_id for cross-system correlation.

        CRITICAL: provider_id is stored as "vsphere://<lowercase-instanceUuid>"
        which matches the K8s node format for SAME_AS auto-correlation.
        """
        for item in items:
            try:
                name = item.get("name")
                instance_uuid = item.get("config.instanceUuid")

                if not name:
                    continue  # Skip malformed items

                # Use instanceUuid as canonical_id (unique per vCenter)
                canonical_id = instance_uuid if instance_uuid else item.get("_moref", "")
                if not canonical_id:
                    continue

                # CRITICAL: provider_id must be lowercase vsphere:// format
                provider_id = ""
                if instance_uuid:
                    provider_id = f"vsphere://{instance_uuid.lower()}"

                # Extract IP and hostname, normalizing None to empty string
                ip_address = item.get("guest.ipAddress") or ""
                hostname = item.get("guest.hostName") or ""

                vm_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="vmware_vm",
                    connector_type=connector_type,
                    canonical_id=canonical_id,
                    description=f"VMware VM {name}",
                    raw_attributes={
                        "provider_id": provider_id,
                        "ip_address": ip_address,
                        "hostname": hostname,
                        "power_state": item.get("summary.runtime.powerState", ""),
                        "connection_state": item.get("summary.runtime.connectionState", ""),
                        "cpu": item.get("config.hardware.numCPU", 0),
                        "memory_mb": item.get("config.hardware.memoryMB", 0),
                        "guest_os": item.get("config.guestFullName", ""),
                        "tools_status": item.get("guest.toolsStatus", ""),
                        # Store references for future relationship reconciliation
                        "host_ref": item.get("summary.runtime.host", ""),
                        "datastore_refs": item.get("datastore", []),
                        "network_refs": item.get("network", []),
                        "resource_pool_ref": item.get("resourcePool", ""),
                    },
                )
                entities.append(vm_entity)

            except Exception:
                logger.warning(
                    "vmware_vm_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_hosts(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract ESXi host entities."""
        for item in items:
            try:
                name = item.get("name")
                moref = item.get("_moref", "")

                if not name or not moref:
                    continue

                # Use hostname as hostname, and name as potential IP if it looks like one
                hostname = name
                ip_address = ""

                host_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="vmware_host",
                    connector_type=connector_type,
                    canonical_id=moref,
                    description=f"ESXi host {name}",
                    raw_attributes={
                        "hostname": hostname,
                        "ip_address": ip_address,
                        "cpu_model": item.get("summary.hardware.cpuModel", ""),
                        "cpu_cores": item.get("summary.hardware.numCpuCores", 0),
                        "memory_size": item.get("summary.hardware.memorySize", 0),
                        "connection_state": item.get("summary.runtime.connectionState", ""),
                        "power_state": item.get("summary.runtime.powerState", ""),
                        "parent_ref": item.get("parent", ""),
                    },
                )
                entities.append(host_entity)

            except Exception:
                logger.warning(
                    "vmware_host_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_clusters(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract cluster entities."""
        for item in items:
            try:
                name = item.get("name")
                moref = item.get("_moref", "")

                if not name or not moref:
                    continue

                cluster_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="vmware_cluster",
                    connector_type=connector_type,
                    canonical_id=moref,
                    description=f"vSphere cluster {name}",
                    raw_attributes={
                        "num_hosts": item.get("summary.numHosts", 0),
                        "num_effective_hosts": item.get("summary.numEffectiveHosts", 0),
                        "total_cpu": item.get("summary.totalCpu", 0),
                        "total_memory": item.get("summary.totalMemory", 0),
                    },
                )
                entities.append(cluster_entity)

            except Exception:
                logger.warning(
                    "vmware_cluster_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_datastores(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract datastore entities."""
        for item in items:
            try:
                name = item.get("name")
                moref = item.get("_moref", "")

                if not name or not moref:
                    continue

                ds_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="vmware_datastore",
                    connector_type=connector_type,
                    canonical_id=moref,
                    description=f"Datastore {name}",
                    raw_attributes={
                        "type": item.get("summary.type", ""),
                        "capacity": item.get("summary.capacity", 0),
                        "free_space": item.get("summary.freeSpace", 0),
                        "accessible": item.get("summary.accessible", False),
                    },
                )
                entities.append(ds_entity)

            except Exception:
                logger.warning(
                    "vmware_datastore_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_networks(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract network entities."""
        for item in items:
            try:
                name = item.get("name")
                moref = item.get("_moref", "")

                if not name or not moref:
                    continue

                net_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="vmware_network",
                    connector_type=connector_type,
                    canonical_id=moref,
                    description=f"Network {name}",
                    raw_attributes={
                        "accessible": item.get("summary.accessible", False),
                    },
                )
                entities.append(net_entity)

            except Exception:
                logger.warning(
                    "vmware_network_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )
