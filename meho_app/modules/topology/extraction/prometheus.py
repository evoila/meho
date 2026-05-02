# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus extraction schema for topology auto-discovery.

Defines declarative extraction rules for Prometheus scrape targets.
These rules specify how to extract ScrapeTarget entities from
Prometheus list_targets responses.

ScrapeTarget entities are linked to K8s/VMware entities via IP-based
entity resolution (SAME_AS edges), enabling cross-system correlation
in the topology graph.

Supported Entity Types:
    - ScrapeTarget: Prometheus scrape target (host:port being scraped)

Relationships:
    - None declared in schema -- cross-system SAME_AS edges are created
      automatically by the IP-based entity resolution system (v1.66)
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
)

# =============================================================================
# Prometheus Extraction Schema
# =============================================================================

PROMETHEUS_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="prometheus",
    entity_rules=[
        # =====================================================================
        # ScrapeTarget Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="ScrapeTarget",
            source_operations=["list_targets"],
            items_path="targets",
            name_path="instance",
            scope_paths={"job": "job"},
            description=DescriptionTemplate(
                template="Prometheus scrape target {instance} (job: {job}, health: {health})",
                fallback="Prometheus ScrapeTarget",
            ),
            attributes=[
                AttributeExtraction(name="job", path="job"),
                AttributeExtraction(name="health", path="health"),
                AttributeExtraction(name="labels_url", path="labels_url"),
                AttributeExtraction(name="namespace", path="namespace"),
                AttributeExtraction(name="pod", path="pod"),
                AttributeExtraction(name="node", path="node"),
                AttributeExtraction(name="ip_address", path="ip_address"),
            ],
            relationships=[],  # Cross-system SAME_AS handled by IP-based entity resolution
        ),
    ],
)
