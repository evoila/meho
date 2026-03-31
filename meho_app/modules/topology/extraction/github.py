# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub extraction schema for topology auto-discovery.

Defines declarative extraction rules for GitHub resources.
These rules specify how to extract entities and relationships from
GitHub connector operation results using JMESPath expressions.

Supported Entity Types:
    - Repository: GitHub repositories with name, full_name, default_branch
    - Workflow: GitHub Actions workflows with member_of relationship to Repository

Relationship Types:
    - member_of: Workflow -> Repository (workflow belongs to repository)

Data Formats:
    GitHub connector returns JSON data from the GitHub REST API.
    Repository fields: name, full_name, default_branch, description.
    Workflow fields: name, state, path.

Note: Pull requests are intentionally excluded from topology extraction.
    They are too transient for meaningful topology representation --
    PRs are short-lived and would create excessive entity churn.
"""
from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)


# =============================================================================
# GitHub Extraction Schema
# =============================================================================

GITHUB_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="github",

    entity_rules=[
        # =====================================================================
        # Repository Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Repository",
            source_operations=[
                "list_repositories",
                "get_repository",
            ],

            # GitHub list operations return arrays directly
            items_path=None,

            name_path="full_name",
            scope_paths={
                "owner": "owner.login",
            },

            description=DescriptionTemplate(
                template="GitHub Repository {full_name}, default branch: {default_branch}, {visibility}, {language}",
                fallback="GitHub Repository",
            ),

            attributes=[
                AttributeExtraction(name="name", path="name"),
                AttributeExtraction(name="full_name", path="full_name"),
                AttributeExtraction(name="owner", path="owner.login"),
                AttributeExtraction(name="default_branch", path="default_branch", default="main"),
                AttributeExtraction(name="description", path="description"),
                AttributeExtraction(name="visibility", path="visibility"),
                AttributeExtraction(name="language", path="language"),
                AttributeExtraction(name="html_url", path="html_url"),
                AttributeExtraction(name="clone_url", path="clone_url"),
                AttributeExtraction(name="topics", path="topics", default=[]),
                AttributeExtraction(name="archived", path="archived", default=False),
                AttributeExtraction(name="disabled", path="disabled", default=False),
                AttributeExtraction(name="created_at", path="created_at"),
                AttributeExtraction(name="updated_at", path="updated_at"),
                AttributeExtraction(name="pushed_at", path="pushed_at"),
            ],

            relationships=[],  # Repositories are top-level, no outgoing relationships
        ),

        # =====================================================================
        # Workflow Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Workflow",
            source_operations=[
                "list_workflows",
                "get_workflow",
            ],

            # GitHub workflows endpoint returns {total_count, workflows: [...]}
            items_path="workflows",

            name_path="name",
            scope_paths={
                "repository": "repository_full_name",
            },

            description=DescriptionTemplate(
                template="GitHub Workflow {name}, state: {state}, path: {path}",
                fallback="GitHub Workflow",
            ),

            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="name", path="name"),
                AttributeExtraction(name="path", path="path"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(name="html_url", path="html_url"),
                AttributeExtraction(name="badge_url", path="badge_url"),
                AttributeExtraction(name="created_at", path="created_at"),
                AttributeExtraction(name="updated_at", path="updated_at"),
                # Repository context (may be injected by connector serializer)
                AttributeExtraction(name="repository_full_name", path="repository_full_name"),
            ],

            relationships=[
                # Workflow is member of Repository
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Repository",
                    target_path="repository_full_name",
                    optional=True,
                ),
            ],
        ),
    ],
)
