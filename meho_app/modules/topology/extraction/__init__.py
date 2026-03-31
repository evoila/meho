# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology extraction schema registry and schema-based extractor.

This module provides:
1. Declarative extraction schemas for topology auto-discovery
2. Schema-based extractor that uses JMESPath to extract entities

Each schema defines how to extract entities and relationships from connector
API responses using JMESPath expressions.

Unlike the schema/ module (which defines WHAT entities exist and WHICH
relationships are valid), this module defines HOW to extract them from
API responses.

Usage:
    from meho_app.modules.topology.extraction import (
        get_extraction_schema,
        get_schema_extractor,
    )

    # Use schema-based extractor (recommended)
    extractor = get_schema_extractor()
    entities, relationships = extractor.extract(
        connector_type="kubernetes",
        operation_id=None,
        result_data={"kind": "PodList", "items": [...]},
        connector_id="abc123",
    )

    # Or get extraction schema directly
    schema = get_extraction_schema("kubernetes")
    if schema:
        rules = schema.find_matching_rules(
            operation_id="list_pods",
            result_data={"kind": "PodList", "items": [...]},
        )

Available Schemas:
    - kubernetes: K8s resources (Pod, Deployment, Service, etc.)
    - vmware: vSphere resources (VM, Host, Cluster, Datastore)
    - gcp: Google Cloud resources (Instance, Disk, Network, Subnet, Firewall, GKECluster)
    - proxmox: Proxmox VE resources (VM, Container, Node, Storage)
    - prometheus: Prometheus scrape targets (ScrapeTarget)
    - argocd: ArgoCD resources (Application, AppProject)
    - github: GitHub resources (Repository, Workflow)
    - aws: AWS resources (EC2Instance, EKSCluster, ECSCluster, VPC, Subnet, SecurityGroup)

See Also:
    - meho_app.modules.topology.schema: Entity type validation schemas
    - TASK-157 for architecture details
"""

from typing import Dict, Optional, Set  # noqa: UP035 -- typing import for Python 3.11 compat

from .extractor import (
    SchemaBasedExtractor,
    get_schema_extractor,
    reset_schema_extractor,
)
from .gcp import GCP_EXTRACTION_SCHEMA
from .kubernetes import KUBERNETES_EXTRACTION_SCHEMA
from .proxmox import PROXMOX_EXTRACTION_SCHEMA
from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)
from .vmware import VMWARE_EXTRACTION_SCHEMA
from .prometheus import PROMETHEUS_EXTRACTION_SCHEMA
from .argocd import ARGOCD_EXTRACTION_SCHEMA
from .aws import AWS_EXTRACTION_SCHEMA
from .azure import AZURE_EXTRACTION_SCHEMA
from .github import GITHUB_EXTRACTION_SCHEMA

# =============================================================================
# Extraction Schema Registry
# =============================================================================

EXTRACTION_SCHEMAS: dict[str, ConnectorExtractionSchema] = {
    "kubernetes": KUBERNETES_EXTRACTION_SCHEMA,
    "vmware": VMWARE_EXTRACTION_SCHEMA,
    "gcp": GCP_EXTRACTION_SCHEMA,
    "proxmox": PROXMOX_EXTRACTION_SCHEMA,
    "prometheus": PROMETHEUS_EXTRACTION_SCHEMA,
    "argocd": ARGOCD_EXTRACTION_SCHEMA,
    "github": GITHUB_EXTRACTION_SCHEMA,
    "azure": AZURE_EXTRACTION_SCHEMA,
    "aws": AWS_EXTRACTION_SCHEMA,
}


def get_extraction_schema(connector_type: str) -> ConnectorExtractionSchema | None:
    """
    Get extraction schema for a connector type.

    Args:
        connector_type: The connector type (e.g., "kubernetes", "vmware")

    Returns:
        ConnectorExtractionSchema if found, None otherwise

    Example:
        schema = get_extraction_schema("kubernetes")
        if schema:
            rules = schema.find_matching_rules(op_id, response_data)
    """
    return EXTRACTION_SCHEMAS.get(connector_type)


def get_all_extraction_schemas() -> dict[str, ConnectorExtractionSchema]:
    """
    Get all registered extraction schemas.

    Returns:
        Dictionary mapping connector type to schema
    """
    return EXTRACTION_SCHEMAS.copy()


def get_supported_extraction_types() -> set[str]:
    """
    Get all connector types that have extraction schemas.

    Returns:
        Set of connector type names
    """
    return set(EXTRACTION_SCHEMAS.keys())


def is_extraction_available(connector_type: str) -> bool:
    """
    Check if an extraction schema exists for a connector type.

    Args:
        connector_type: The connector type to check

    Returns:
        True if schema exists, False otherwise
    """
    return connector_type in EXTRACTION_SCHEMAS


# =============================================================================
# Public Exports
# =============================================================================

__all__ = [
    # Schema constants
    "EXTRACTION_SCHEMAS",
    "GCP_EXTRACTION_SCHEMA",
    "KUBERNETES_EXTRACTION_SCHEMA",
    "PROXMOX_EXTRACTION_SCHEMA",
    "VMWARE_EXTRACTION_SCHEMA",
    "PROMETHEUS_EXTRACTION_SCHEMA",
    "ARGOCD_EXTRACTION_SCHEMA",
    "GITHUB_EXTRACTION_SCHEMA",
    "AWS_EXTRACTION_SCHEMA",
    "AZURE_EXTRACTION_SCHEMA",
    "AttributeExtraction",
    # Rule dataclasses
    "ConnectorExtractionSchema",
    "DescriptionTemplate",
    "EntityExtractionRule",
    "RelationshipExtraction",
    # Schema-based extractor
    "SchemaBasedExtractor",
    "get_all_extraction_schemas",
    # Registry functions
    "get_extraction_schema",
    "get_schema_extractor",
    "get_supported_extraction_types",
    "is_extraction_available",
    "reset_schema_extractor",
]
