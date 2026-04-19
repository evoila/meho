# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub topology schema definition.

Defines entity types for GitHub resources.
GitHub Organization is the single topology entity -- one per connector.

Entity Types:
- GitHub Organization (org containing repositories and CI/CD workflows, unscoped)

Relationship Rules:
- None defined in schema -- cross-system edges (GitHub -> ArgoCD,
  GitHub -> K8s) are deferred to Phase 52 (Pipeline Trace).
"""

from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    Volatility,
)

# =============================================================================
# Entity Type Definitions
# =============================================================================

_GITHUB_ORG = EntityTypeDefinition(
    name="GitHub Organization",
    scoped=False,
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "GitHub organization containing repositories and CI/CD workflows",
        "Check workflow runs and deployments for CI/CD status",
    ],
    common_queries=[
        "What repositories are in this GitHub org?",
        "Show me recent workflow runs",
        "What deployments happened recently?",
        "Why did the last build fail?",
    ],
)


# =============================================================================
# Complete GitHub Schema
# =============================================================================

GITHUB_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="github",
    entity_types={
        "GitHub Organization": _GITHUB_ORG,
    },
    relationship_rules={},
)
