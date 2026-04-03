# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Dispatcher for routing ingestion jobs to the configured backend.

Provides create_backend() factory (lazy imports, same pattern as connectors/pool.py)
and IngestionDispatcher class that estimates resources and delegates to backends.
"""

from meho_app.core.config import get_config
from meho_app.worker.backends.protocol import (
    IngestionBackend,
    JobStatus,
    ResourceProfile,
)
from meho_app.worker.resource_estimator import estimate_resources


def create_backend() -> IngestionBackend:
    """Create the configured ingestion backend using lazy imports.

    Reads ``config.ingestion_backend`` and returns the matching backend
    instance. Uses lazy imports to avoid loading heavy SDKs (kubernetes-asyncio,
    google-cloud-run, docker) until actually needed.

    Returns:
        An IngestionBackend implementation.

    Raises:
        ValueError: If the configured backend type is unknown.
    """
    config = get_config()
    backend_type = config.ingestion_backend

    if backend_type == "local":
        from meho_app.worker.backends.local import LocalBackend

        return LocalBackend()

    if backend_type == "kubernetes":
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        return KubernetesBackend(
            namespace=config.k8s_ingestion_namespace,
            image=config.worker_image,
            server_url=config.k8s_ingestion_server,
            token=config.k8s_ingestion_token,
            ca_cert=config.k8s_ingestion_ca_cert,
            service_account=config.k8s_ingestion_service_account,
        )

    if backend_type == "cloudrun":
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        return CloudRunBackend(
            project=config.cloudrun_project,
            region=config.cloudrun_region,
            job_name=config.cloudrun_job_name,
        )

    if backend_type == "docker":
        from meho_app.worker.backends.docker import DockerBackend

        return DockerBackend(
            docker_host=config.docker_ingestion_host,
            image=config.worker_image,
        )

    raise ValueError(f"Unknown ingestion backend: {backend_type}")


class IngestionDispatcher:
    """Routes ingestion jobs to the configured backend with resource estimation.

    Wraps ``create_backend()`` and ``estimate_resources()`` so callers only need
    to provide the page count -- the dispatcher handles sizing and delegation.
    """

    def __init__(self) -> None:
        self._backend: IngestionBackend = create_backend()

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        page_count: int,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch an ingestion job to the configured backend.

        Estimates resource requirements from page count and passes
        the ResourceProfile to the backend.

        Args:
            job_id: Unique job identifier from the knowledge module.
            input_url: Presigned URL to the source document.
            output_url: Presigned URL for the Arrow IPC output.
            page_count: Number of pages in the document.
            env_overrides: Additional environment variables for the worker.

        Returns:
            Backend-specific execution ID for status tracking.
        """
        profile: ResourceProfile = estimate_resources(page_count)
        return await self._backend.dispatch(
            job_id=job_id,
            input_url=input_url,
            output_url=output_url,
            profile=profile,
            env_overrides=env_overrides,
        )

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get current status of a dispatched job.

        Args:
            execution_id: Backend-specific execution ID from dispatch().

        Returns:
            Current job status.
        """
        return await self._backend.get_status(execution_id)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running job.

        Args:
            execution_id: Backend-specific execution ID from dispatch().
        """
        await self._backend.cancel(execution_id)
