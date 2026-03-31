"""Proxmox entity extractor for topology auto-discovery.

Extracts VMs, Containers, Nodes, Storage, and Ceph Pools from Proxmox
API results into TopologyEntity models.

Registered as "proxmox" in the extractor registry via @register_extractor.

CRITICAL: VM and Container entities store ip_address and hostname in raw_attributes
for the CorrelationEngine to create SAME_AS correlations with K8s nodes via
IP/hostname matching.
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
    "list-containers",
    "list-nodes",
    "list-storage",
    "list-ceph-pools",
}


@register_extractor("proxmox")
class ProxmoxEntityExtractor(BaseEntityExtractor):
    """Extract topology entities from Proxmox API responses.

    Handles 5 resource types: VMs, Containers, Nodes, Storage, Ceph Pools.
    Non-extractable operations (get-vm, write ops) return empty results.
    """

    def extract(
        self,
        connector_name: str,
        connector_type: str,
        operation_id: str,
        result_data: dict[str, Any],
    ) -> ExtractionResult:
        """Extract entities and relationships from Proxmox API data.

        Routes to type-specific extraction methods based on operation_id.
        Non-extractable operations and missing data return empty ExtractionResult.
        """
        entities: list[TopologyEntity] = []
        relationships: list[TopologyRelationship] = []

        if operation_id not in _EXTRACTABLE_OPERATIONS:
            return ExtractionResult(
                entities=[],
                relationships=[],
                source_connector=connector_name,
                source_operation=operation_id,
            )

        # Get items from result
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
        elif operation_id == "list-containers":
            self._extract_containers(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-nodes":
            self._extract_nodes(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-storage":
            self._extract_storage(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-ceph-pools":
            self._extract_ceph_pools(connector_name, connector_type, items, entities, relationships)

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
        """Extract VM entities with ip_address/hostname for cross-system correlation.

        CRITICAL: ip_address and hostname in raw_attributes enable CorrelationEngine
        to match Proxmox VMs with K8s nodes via SAME_AS correlations.
        """
        for item in items:
            try:
                name = item.get("name")
                vmid = item.get("vmid")

                if not name or vmid is None:
                    continue

                node = item.get("node", "")

                # Normalize None to empty string for correlation matching
                ip_address = item.get("ip_address") or ""
                hostname = item.get("hostname") or ""

                vm_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="proxmox_vm",
                    connector_type=connector_type,
                    canonical_id=f"proxmox:{connector_name}:vm:{vmid}",
                    scope={"connector_name": connector_name, "connector_type": connector_type},
                    description=f"Proxmox VM {name} (VMID {vmid})",
                    raw_attributes={
                        "vmid": vmid,
                        "name": name,
                        "status": item.get("status", ""),
                        "node": node,
                        "maxmem": item.get("maxmem", 0),
                        "maxcpu": item.get("maxcpu", 0),
                        "ip_address": ip_address,
                        "hostname": hostname,
                    },
                )
                entities.append(vm_entity)

                # Create member_of relationship: VM -> Node
                if node:
                    node_canonical = f"proxmox:{connector_name}:node:{node}"
                    relationships.append(
                        TopologyRelationship(
                            from_entity_id=vm_entity.canonical_id,
                            to_entity_id=node_canonical,
                            relationship_type="member_of",
                        )
                    )

            except Exception:
                logger.warning(
                    "proxmox_vm_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_containers(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract LXC container entities.

        Same pattern as VMs but with entity_type="proxmox_container" and "ct:" prefix.
        """
        for item in items:
            try:
                name = item.get("name")
                vmid = item.get("vmid")

                if not name or vmid is None:
                    continue

                node = item.get("node", "")

                # Normalize None to empty string
                ip_address = item.get("ip_address") or ""
                hostname = item.get("hostname") or ""

                ct_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="proxmox_container",
                    connector_type=connector_type,
                    canonical_id=f"proxmox:{connector_name}:ct:{vmid}",
                    scope={"connector_name": connector_name, "connector_type": connector_type},
                    description=f"Proxmox Container {name} (VMID {vmid})",
                    raw_attributes={
                        "vmid": vmid,
                        "name": name,
                        "status": item.get("status", ""),
                        "node": node,
                        "maxmem": item.get("maxmem", 0),
                        "maxcpu": item.get("maxcpu", 0),
                        "ip_address": ip_address,
                        "hostname": hostname,
                    },
                )
                entities.append(ct_entity)

                # Create member_of relationship: Container -> Node
                if node:
                    node_canonical = f"proxmox:{connector_name}:node:{node}"
                    relationships.append(
                        TopologyRelationship(
                            from_entity_id=ct_entity.canonical_id,
                            to_entity_id=node_canonical,
                            relationship_type="member_of",
                        )
                    )

            except Exception:
                logger.warning(
                    "proxmox_container_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_nodes(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract Proxmox node entities."""
        for item in items:
            try:
                node_name = item.get("node")
                if not node_name:
                    continue

                # IP may come from 'ip' field or be absent
                ip_address = item.get("ip") or ""

                node_entity = TopologyEntity(
                    name=node_name,
                    connector_name=connector_name,
                    entity_type="proxmox_node",
                    connector_type=connector_type,
                    canonical_id=f"proxmox:{connector_name}:node:{node_name}",
                    scope={"connector_name": connector_name, "connector_type": connector_type},
                    description=f"Proxmox Node {node_name}",
                    raw_attributes={
                        "node": node_name,
                        "status": item.get("status", ""),
                        "maxcpu": item.get("maxcpu", 0),
                        "maxmem": item.get("maxmem", 0),
                        "ip_address": ip_address,
                    },
                )
                entities.append(node_entity)

            except Exception:
                logger.warning(
                    "proxmox_node_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("node", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_storage(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract storage entities."""
        for item in items:
            try:
                storage_name = item.get("storage")
                if not storage_name:
                    continue

                st_entity = TopologyEntity(
                    name=storage_name,
                    connector_name=connector_name,
                    entity_type="proxmox_storage",
                    connector_type=connector_type,
                    canonical_id=f"proxmox:{connector_name}:storage:{storage_name}",
                    scope={"connector_name": connector_name, "connector_type": connector_type},
                    description=f"Proxmox Storage {storage_name}",
                    raw_attributes={
                        "type": item.get("type", ""),
                        "total": item.get("total", 0),
                        "used": item.get("used", 0),
                        "avail": item.get("avail", 0),
                        "enabled": item.get("enabled", 0),
                    },
                )
                entities.append(st_entity)

            except Exception:
                logger.warning(
                    "proxmox_storage_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("storage", "unknown") if isinstance(item, dict) else "unknown",
                )

    def _extract_ceph_pools(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract Ceph pool entities."""
        for item in items:
            try:
                pool_name = item.get("name")
                if not pool_name:
                    continue

                pool_entity = TopologyEntity(
                    name=pool_name,
                    connector_name=connector_name,
                    entity_type="proxmox_ceph_pool",
                    connector_type=connector_type,
                    canonical_id=f"proxmox:{connector_name}:ceph:{pool_name}",
                    scope={"connector_name": connector_name, "connector_type": connector_type},
                    description=f"Proxmox Ceph Pool {pool_name}",
                    raw_attributes={
                        "name": pool_name,
                        "size": item.get("size", 0),
                        "min_size": item.get("min_size", 0),
                        "pg_num": item.get("pg_num", 0),
                        "bytes_used": item.get("bytes_used", 0),
                    },
                )
                entities.append(pool_entity)

            except Exception:
                logger.warning(
                    "proxmox_ceph_pool_extraction_skipped",
                    connector_name=connector_name,
                    item_name=item.get("name", "unknown") if isinstance(item, dict) else "unknown",
                )
