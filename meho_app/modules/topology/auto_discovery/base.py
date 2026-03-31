# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Base extractor interface for topology auto-discovery.

Defines the core data structures and abstract base class for extracting
entities and relationships from connector operation results.

Extractors are pure functions - no async, no DB access. They just parse data.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ExtractedEntity:
    """
    An entity extracted from a connector operation result.

    Entities are infrastructure components discovered during API calls:
    - VMs, Hosts, Clusters (VMware)
    - Pods, Nodes, Deployments (Kubernetes)
    - Instances, Disks, VPCs (GCP)
    - VMs, Containers, Nodes (Proxmox)

    Attributes:
        name: Unique name of the entity within its connector
        description: Rich description for embedding generation
        connector_id: ID of the connector that discovered this entity
        entity_type: Type of entity (Pod, VM, Host, etc.) from extraction schema
        scope: Scope information (namespace, cluster, datacenter, etc.)
        connector_name: Human-readable connector name (for display)
        raw_attributes: Original data from the API response

    Example:
        ExtractedEntity(
            name="web-server-01",
            description="VMware VM web-server-01, 4 vCPU, 8192MB RAM, CentOS 7, IP: 192.168.1.10",
            connector_id="abc123",
            entity_type="VM",
            scope={},
            connector_name="Production vCenter",
            raw_attributes={"power_state": "poweredOn", "host": "esxi-01", ...},
        )
    """

    name: str
    description: str
    connector_id: str

    # Type information from extraction schema
    entity_type: str = "Unknown"
    scope: dict[str, Any] = field(default_factory=dict)

    # Optional fields
    connector_name: str | None = None
    raw_attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedEntity":
        """Create from dictionary with backward compatibility."""
        return cls(
            name=data["name"],
            description=data["description"],
            connector_id=data["connector_id"],
            entity_type=data.get("entity_type", "Unknown"),
            scope=data.get("scope", {}),
            connector_name=data.get("connector_name"),
            raw_attributes=data.get("raw_attributes", {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "ExtractedEntity":
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class ExtractedRelationship:
    """
    A relationship between two entities extracted from connector data.

    Relationships represent structural/hierarchical connections:
    - runs_on: VM runs on Host, Pod runs on Node
    - member_of: Host is member of Cluster, Pod is member of Deployment
    - uses: VM uses Datastore, Instance uses Disk
    - routes_to: Ingress routes to Service
    - depends_on: Service depends on another Service
    - connects_to: Network connection between entities

    Direction is always FROM → TO:
    - "web-01 runs_on esxi-02" means web-01 executes on esxi-02
    - "esxi-02 member_of prod-cluster" means esxi-02 is part of prod-cluster

    Attributes:
        from_entity_name: Source entity name
        to_entity_name: Target entity name
        relationship_type: Type of relationship (runs_on, member_of, uses, etc.)
        from_entity_type: Type of source entity (for validation)
        to_entity_type: Type of target entity (for validation)

    Example:
        ExtractedRelationship(
            from_entity_name="web-server-01",
            to_entity_name="esxi-host-02",
            relationship_type="runs_on",
            from_entity_type="VM",
            to_entity_type="Host",
        )
    """

    from_entity_name: str
    to_entity_name: str
    relationship_type: str

    # Type information for validation
    from_entity_type: str | None = None
    to_entity_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedRelationship":
        """Create from dictionary with backward compatibility."""
        return cls(
            from_entity_name=data["from_entity_name"],
            to_entity_name=data["to_entity_name"],
            relationship_type=data["relationship_type"],
            from_entity_type=data.get("from_entity_type"),
            to_entity_type=data.get("to_entity_type"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "ExtractedRelationship":
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))


class BaseExtractor(ABC):
    """
    Abstract base class for topology entity extractors.

    Each connector type (VMware, Kubernetes, GCP, Proxmox) has its own
    extractor that knows how to parse operation results into entities
    and relationships.

    Extractors are:
    - Pure: No side effects, no async, no DB access
    - Stateless: Same input always produces same output
    - Focused: Only parse data, don't store or validate

    Example implementation:
        class VMwareExtractor(BaseExtractor):
            def can_extract(self, operation_id: str) -> bool:
                return operation_id in ["list_virtual_machines", "list_hosts"]

            def extract(self, operation_id, result_data, connector_id, connector_name):
                if operation_id == "list_virtual_machines":
                    return self._extract_vms(result_data, connector_id, connector_name)
                # ...
    """

    @abstractmethod
    def can_extract(self, operation_id: str) -> bool:
        """
        Check if this extractor can handle the given operation.

        Args:
            operation_id: The operation identifier (e.g., "list_virtual_machines")

        Returns:
            True if this extractor knows how to extract from this operation
        """
        pass

    @abstractmethod
    def extract(
        self,
        operation_id: str,
        result_data: Any,
        connector_id: str,
        connector_name: str | None = None,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelationship]]:
        """
        Extract entities and relationships from operation result.

        Args:
            operation_id: The operation that produced this data
            result_data: The raw result data from the connector
            connector_id: ID of the connector
            connector_name: Optional human-readable connector name

        Returns:
            Tuple of (entities, relationships) extracted from the data

        Note:
            - May return empty lists if no entities found
            - Should not raise on malformed data, just skip bad records
            - Relationships may reference entities not in the result
              (e.g., a VM references a Host that wasn't in this call)
        """
        pass

    def get_supported_operations(self) -> list[str]:
        """
        Get list of operation IDs this extractor supports.

        Override this to provide introspection capabilities.

        Returns:
            List of operation IDs that can_extract() returns True for
        """
        return []
