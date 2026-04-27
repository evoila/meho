# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD topology schema definition.

Defines entity types for ArgoCD resources.
ArgoCD Server is the single topology entity -- one per connector.
K8s resource types visible in the ArgoCD resource tree are also declared
here with SameAsEligibility to enable SAME_AS edge resolution with K8s
topology entities.

Entity Types:
- ArgoCD Server (GitOps server managing K8s applications, unscoped)
- Deployment, StatefulSet, DaemonSet, Service, Ingress
  (K8s resources visible in ArgoCD resource tree, with SAME_AS eligibility)

Relationship Rules:
- None defined in schema -- MANAGED_BY and SAME_AS edges are cross-connector
  (ArgoCD -> K8s resources) and emitted dynamically from the
  resource tree handler via _emit_managed_by_edges / _emit_same_as_edges.
"""

from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    SameAsEligibility,
    Volatility,
)

# =============================================================================
# Entity Type Definitions
# =============================================================================

_ARGOCD_SERVER = EntityTypeDefinition(
    name="ArgoCD Server",
    scoped=False,
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "ArgoCD GitOps server managing K8s applications",
        "Find managed resources via managed_by relationship (reverse)",
    ],
    common_queries=[
        "What K8s resources does this ArgoCD manage?",
        "What is the ArgoCD server version?",
        "Show me applications managed by this ArgoCD",
    ],
)


# =============================================================================
# K8s Resource Types Visible in ArgoCD Resource Tree
# =============================================================================
# These enable SAME_AS resolution with K8s topology entities.
# The DeterministicResolver uses SameAsEligibility.can_match to find
# matching entity types across connectors.

_ARGOCD_DEPLOYMENT = EntityTypeDefinition(
    name="Deployment",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(can_match=["Deployment"]),
)

_ARGOCD_STATEFULSET = EntityTypeDefinition(
    name="StatefulSet",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(can_match=["StatefulSet"]),
)

_ARGOCD_DAEMONSET = EntityTypeDefinition(
    name="DaemonSet",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(can_match=["DaemonSet"]),
)

_ARGOCD_SERVICE = EntityTypeDefinition(
    name="Service",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(can_match=["Service"]),
)

_ARGOCD_INGRESS = EntityTypeDefinition(
    name="Ingress",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(can_match=["Ingress"]),
)


# =============================================================================
# Complete ArgoCD Schema
# =============================================================================

ARGOCD_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="argocd",
    entity_types={
        "ArgoCD Server": _ARGOCD_SERVER,
        "Deployment": _ARGOCD_DEPLOYMENT,
        "StatefulSet": _ARGOCD_STATEFULSET,
        "DaemonSet": _ARGOCD_DAEMONSET,
        "Service": _ARGOCD_SERVICE,
        "Ingress": _ARGOCD_INGRESS,
    },
    relationship_rules={},
)
