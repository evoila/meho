# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Artifact Registry Handlers (Phase 49)

Handlers for Artifact Registry operations: list repositories and
list Docker images with version history grouped by image name.
"""

import asyncio
import itertools
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.serializers import (
    serialize_artifact_repository,
    serialize_docker_image,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class ArtifactRegistryHandlerMixin:
    """Mixin providing Artifact Registry operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _artifact_registry_client: Any
        _credentials: Any
        project_id: str
        default_region: str

    # =========================================================================
    # ARTIFACT REGISTRY OPERATIONS (READ)
    # =========================================================================

    async def _handle_list_artifact_repositories(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List Artifact Registry repositories showing format, location, and size."""
        from google.cloud import artifactregistry_v1

        location = params.get("location") or self.default_region
        limit = params.get("limit", 50)

        parent = f"projects/{self.project_id}/locations/{location}"

        request = artifactregistry_v1.ListRepositoriesRequest(
            parent=parent,
            page_size=limit,
        )

        # Use _artifact_registry_client if available, otherwise create
        # a temporary one (before Plan 03 wiring)
        client = getattr(self, "_artifact_registry_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: artifactregistry_v1.ArtifactRegistryClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        pager = await asyncio.to_thread(lambda: client.list_repositories(request=request))

        repos = []
        for repo in itertools.islice(pager, limit):
            repos.append(serialize_artifact_repository(repo))

        return repos

    async def _handle_list_docker_images(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List Docker images with tags, digests, upload time, size,
        and version history grouped by image name.

        Results are grouped by image_name so each entry represents a unique
        image with all its versions (tags/digests) nested inside.
        """
        from google.cloud import artifactregistry_v1

        repository = params["repository"]
        location = params.get("location") or self.default_region
        limit = params.get("limit", 50)

        parent = f"projects/{self.project_id}/locations/{location}/repositories/{repository}"

        request = artifactregistry_v1.ListDockerImagesRequest(
            parent=parent,
            page_size=limit,
        )

        # Use _artifact_registry_client if available, otherwise create
        # a temporary one (before Plan 03 wiring)
        client = getattr(self, "_artifact_registry_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: artifactregistry_v1.ArtifactRegistryClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        pager = await asyncio.to_thread(lambda: client.list_docker_images(request=request))

        # Serialize all images
        serialized_images = []
        for img in itertools.islice(pager, limit):
            serialized_images.append(serialize_docker_image(img))

        # Group by image_name for version history view
        grouped: dict[str, dict[str, Any]] = {}
        for img in serialized_images:
            key = img["image_name"]
            if key not in grouped:
                grouped[key] = {"image_name": key, "versions": []}
            grouped[key]["versions"].append(img)

        return list(grouped.values())
