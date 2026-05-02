# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD extraction schema for topology auto-discovery.

Defines declarative extraction rules for ArgoCD resources.
These rules specify how to extract entities and relationships from
ArgoCD connector operation results using JMESPath expressions.

Supported Entity Types:
    - Application: ArgoCD applications with member_of relationship to AppProject
    - AppProject: ArgoCD projects (top-level grouping)

Relationship Types:
    - member_of: Application -> AppProject (project membership)

Data Formats:
    ArgoCD connector returns JSON data from the ArgoCD REST API.
    Application fields: name, namespace, project, health status, sync status.
    Project fields: name, description.
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

PROP_METADATA_NAMESPACE = "metadata.namespace"
PROP_SPEC_PROJECT = "spec.project"

# =============================================================================
# ArgoCD Extraction Schema
# =============================================================================

ARGOCD_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="argocd",
    entity_rules=[
        # =====================================================================
        # Application Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Application",
            source_operations=[
                "list_applications",
                "get_application",
            ],
            # ArgoCD list operations return items in an array
            items_path="items",
            name_path="metadata.name",
            scope_paths={
                "namespace": PROP_METADATA_NAMESPACE,
                "project": PROP_SPEC_PROJECT,
            },
            description=DescriptionTemplate(
                template="ArgoCD Application {metadata.name}, namespace {metadata.namespace}, project {spec.project}, health: {status.health.status}, sync: {status.sync.status}",
                fallback="ArgoCD Application",
            ),
            attributes=[
                AttributeExtraction(name="namespace", path=PROP_METADATA_NAMESPACE),
                AttributeExtraction(name="project", path=PROP_SPEC_PROJECT, default="default"),
                AttributeExtraction(name="health_status", path="status.health.status"),
                AttributeExtraction(name="sync_status", path="status.sync.status"),
                AttributeExtraction(
                    name="repo_url",
                    path="spec.source.repoURL",
                ),
                AttributeExtraction(
                    name="target_revision",
                    path="spec.source.targetRevision",
                ),
                AttributeExtraction(
                    name="path",
                    path="spec.source.path",
                ),
                AttributeExtraction(
                    name="destination_server",
                    path="spec.destination.server",
                ),
                AttributeExtraction(
                    name="destination_namespace",
                    path="spec.destination.namespace",
                ),
                AttributeExtraction(name="labels", path="metadata.labels", default={}),
                AttributeExtraction(
                    name="creation_timestamp",
                    path="metadata.creationTimestamp",
                ),
            ],
            relationships=[
                # Application is member of AppProject
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AppProject",
                    target_path=PROP_SPEC_PROJECT,
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # AppProject Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AppProject",
            source_operations=[
                "list_projects",
                "get_project",
            ],
            # ArgoCD list operations return items in an array
            items_path="items",
            name_path="metadata.name",
            scope_paths={
                "namespace": PROP_METADATA_NAMESPACE,
            },
            description=DescriptionTemplate(
                template="ArgoCD AppProject {metadata.name}, {spec.description}",
                fallback="ArgoCD AppProject",
            ),
            attributes=[
                AttributeExtraction(name="namespace", path=PROP_METADATA_NAMESPACE),
                AttributeExtraction(name="description", path="spec.description"),
                AttributeExtraction(
                    name="source_repos",
                    path="spec.sourceRepos",
                    default=[],
                ),
                AttributeExtraction(
                    name="destinations",
                    path="spec.destinations",
                    default=[],
                ),
                AttributeExtraction(name="labels", path="metadata.labels", default={}),
                AttributeExtraction(
                    name="creation_timestamp",
                    path="metadata.creationTimestamp",
                ),
            ],
            relationships=[],  # AppProjects are top-level, no outgoing relationships
        ),
    ],
)
