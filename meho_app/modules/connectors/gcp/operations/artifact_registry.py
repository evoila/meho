# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Artifact Registry Operation Definitions (Phase 49)

Operations for browsing Artifact Registry repositories and Docker images
with tags, digests, sizes, and version history grouped by image name.
"""

from meho_app.modules.connectors.base import OperationDefinition

ARTIFACT_REGISTRY_OPERATIONS = [
    # Artifact Registry Operations (READ)
    OperationDefinition(
        operation_id="list_artifact_repositories",
        name="List Artifact Repositories",
        description=(
            "List Artifact Registry repositories showing storage locations, "
            "format (e.g., DOCKER, MAVEN, NPM), description, and size. "
            "Defaults to the connector's configured region if no location "
            "is specified."
        ),
        category="registry",
        parameters=[
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": (
                    "GCP region to list repositories in "
                    "(e.g., 'us-central1'). Defaults to the connector's "
                    "default region."
                ),
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of repositories to return (default: 50)",
            },
        ],
        example="list_artifact_repositories(location='us-central1')",
    ),
    OperationDefinition(
        operation_id="list_docker_images",
        name="List Docker Images",
        description=(
            "List Docker images in an Artifact Registry repository with tags, "
            "digests, upload time, size, and version history grouped by image "
            "name. Each entry in the result represents a unique image name "
            "with all its versions (tags/digests) nested inside."
        ),
        category="registry",
        parameters=[
            {
                "name": "repository",
                "type": "string",
                "required": True,
                "description": (
                    "Repository name (e.g., 'my-repo'). This is the short "
                    "name, not the full resource path."
                ),
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": (
                    "GCP region where the repository is located "
                    "(e.g., 'us-central1'). Defaults to the connector's "
                    "default region."
                ),
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of images to return (default: 50)",
            },
        ],
        example="list_docker_images(repository='my-app', location='us-central1')",
    ),
]
