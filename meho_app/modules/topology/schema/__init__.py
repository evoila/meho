# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology schema registry.

This module provides a centralized registry for connector-specific topology schemas.
Each schema defines valid entity types and relationships for a connector type.

Usage:
    from meho_app.modules.topology.schema import get_topology_schema

    schema = get_topology_schema("kubernetes")
    if schema:
        # Validate entity type
        if not schema.is_valid_entity_type("Pod"):
            raise ValueError("Invalid entity type")

        # Validate relationship
        if not schema.is_valid_relationship("Pod", "runs_on", "Node"):
            raise ValueError("Invalid relationship")

        # Build canonical ID
        canonical_id = schema.build_canonical_id("Pod", {"namespace": "prod"}, "nginx")

Available Schemas:
    - kubernetes: K8s resources (Pod, Deployment, Service, etc.)
    - vmware: vSphere resources (VM, Host, Cluster, Datastore, etc.)
    - gcp: Google Cloud resources (Instance, Disk, GKECluster, etc.)
    - proxmox: Proxmox VE resources (VM, Container, Node, Storage)
    - argocd: ArgoCD resources (ArgoCD Server)
    - github: GitHub resources (Organization)
    - aws: AWS resources (EC2Instance, EKSCluster, ECSCluster, VPC, Subnet, SecurityGroup)
    - network_diagnostics: Network diagnostic entities (ExternalURL, IPAddress, TLSCertificate)
"""

from typing import Dict, Optional, Set  # noqa: UP035 -- typing import for Python 3.11 compat

from .argocd import ARGOCD_TOPOLOGY_SCHEMA
from .aws import AWS_TOPOLOGY_SCHEMA
from .azure import AZURE_TOPOLOGY_SCHEMA
from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    RelationshipRule,
    SameAsEligibility,
    Volatility,
)
from .gcp import GCP_TOPOLOGY_SCHEMA
from .github import GITHUB_TOPOLOGY_SCHEMA
from .kubernetes import KUBERNETES_TOPOLOGY_SCHEMA
from .network_diagnostics import NETWORK_DIAGNOSTICS_TOPOLOGY_SCHEMA
from .proxmox import PROXMOX_TOPOLOGY_SCHEMA
from .vmware import VMWARE_TOPOLOGY_SCHEMA

# =============================================================================
# Schema Registry
# =============================================================================

TOPOLOGY_SCHEMAS: dict[str, ConnectorTopologySchema] = {
    "kubernetes": KUBERNETES_TOPOLOGY_SCHEMA,
    "vmware": VMWARE_TOPOLOGY_SCHEMA,
    "gcp": GCP_TOPOLOGY_SCHEMA,
    "proxmox": PROXMOX_TOPOLOGY_SCHEMA,
    "argocd": ARGOCD_TOPOLOGY_SCHEMA,
    "github": GITHUB_TOPOLOGY_SCHEMA,
    "azure": AZURE_TOPOLOGY_SCHEMA,
    "aws": AWS_TOPOLOGY_SCHEMA,
    "network_diagnostics": NETWORK_DIAGNOSTICS_TOPOLOGY_SCHEMA,
}


def get_topology_schema(connector_type: str) -> ConnectorTopologySchema | None:
    """
    Get topology schema for a connector type.

    Args:
        connector_type: The connector type (e.g., "kubernetes", "vmware")

    Returns:
        ConnectorTopologySchema if found, None otherwise

    Example:
        schema = get_topology_schema("kubernetes")
        if schema:
            is_valid = schema.is_valid_entity_type("Pod")
    """
    return TOPOLOGY_SCHEMAS.get(connector_type)


def get_all_schemas() -> dict[str, ConnectorTopologySchema]:
    """
    Get all registered topology schemas.

    Returns:
        Dictionary mapping connector type to schema
    """
    return TOPOLOGY_SCHEMAS.copy()


def get_supported_connector_types() -> set[str]:
    """
    Get all connector types that have topology schemas.

    Returns:
        Set of connector type names
    """
    return set(TOPOLOGY_SCHEMAS.keys())


def is_schema_available(connector_type: str) -> bool:
    """
    Check if a topology schema exists for a connector type.

    Args:
        connector_type: The connector type to check

    Returns:
        True if schema exists, False otherwise
    """
    return connector_type in TOPOLOGY_SCHEMAS


# =============================================================================
# Public Exports
# =============================================================================

__all__ = [
    "ARGOCD_TOPOLOGY_SCHEMA",
    "AWS_TOPOLOGY_SCHEMA",
    "AZURE_TOPOLOGY_SCHEMA",
    "GCP_TOPOLOGY_SCHEMA",
    "GITHUB_TOPOLOGY_SCHEMA",
    "KUBERNETES_TOPOLOGY_SCHEMA",
    "NETWORK_DIAGNOSTICS_TOPOLOGY_SCHEMA",
    "PROXMOX_TOPOLOGY_SCHEMA",
    # Schema constants
    "TOPOLOGY_SCHEMAS",
    "VMWARE_TOPOLOGY_SCHEMA",
    # Base classes
    "ConnectorTopologySchema",
    "EntityTypeDefinition",
    "RelationshipRule",
    "SameAsEligibility",
    "Volatility",
    "get_all_schemas",
    "get_supported_connector_types",
    # Registry functions
    "get_topology_schema",
    "is_schema_available",
]
