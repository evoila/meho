# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Docker backend for ephemeral ingestion via SSH.

Runs document conversion in a Docker container on a remote host
using docker-py with SSH transport. All docker-py calls go through
asyncio.to_thread() because docker-py is synchronous.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meho_app.worker.backends.protocol import (
    JobState,
    JobStatus,
    ResourceProfile,
)


def _create_docker_client(docker_host: str) -> Any:
    """Create a Docker client (lazy import to avoid crash if docker-py not installed)."""
    import docker  # type: ignore[import-untyped]

    return docker.DockerClient(
        base_url=docker_host,
        use_ssh_client=True,
        timeout=30,
    )


class DockerBackend:
    """IngestionBackend that runs containers on a remote Docker host via SSH.

    Uses docker-py with SSH transport to connect to a remote machine
    (e.g., a GPU VM) and run the MEHO worker image as a container.
    All docker-py calls are wrapped in asyncio.to_thread() since the
    docker-py SDK is synchronous.

    Attributes:
        docker_host: Docker host URL (e.g. ssh://user@gpu-vm).
        image: Docker image for the worker container.
    """

    def __init__(
        self,
        docker_host: str,
        image: str,
    ) -> None:
        self.docker_host = docker_host
        self.image = image
        self._containers: dict[str, str] = {}

    def _get_client(self) -> Any:
        """Create a Docker client with SSH transport.

        Returns:
            A configured DockerClient instance.
        """
        return _create_docker_client(self.docker_host)

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch an ingestion job to a remote Docker host.

        Runs a container with resource limits from the profile and
        environment variables for the worker.

        Args:
            job_id: Unique job identifier.
            input_url: Presigned URL or path to the source document.
            output_url: Presigned URL or path for the Arrow IPC output.
            profile: Resource requirements for the container.
            env_overrides: Additional environment variables for the worker.

        Returns:
            Container name as execution_id.
        """
        container_name = f"meho-ingest-{job_id[:8]}"

        # Build environment variables
        env: dict[str, str] = {
            "WORKER_JOB_ID": job_id,
            "WORKER_INPUT_URL": input_url,
            "WORKER_OUTPUT_URL": output_url,
            "OMP_NUM_THREADS": "4",
            "MALLOC_TRIM_THRESHOLD_": "131072",
        }

        if env_overrides:
            env.update(env_overrides)

        def _run() -> str:
            """Blocking function to run container via docker-py."""
            client = self._get_client()
            container = client.containers.run(
                image=self.image,
                command=["python", "-m", "meho_app.worker"],
                environment=env,
                name=container_name,
                mem_limit=f"{profile.memory_gb}g",
                cpu_count=profile.cpu,
                detach=True,
                auto_remove=False,
            )
            return str(container.name)

        result_name = await asyncio.to_thread(_run)
        self._containers[result_name] = job_id

        return result_name

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get status of a Docker container.

        Maps container status:
        - "running" -> RUNNING
        - "exited" with exit code 0 -> SUCCEEDED
        - "exited" with exit code != 0 -> FAILED
        - else -> PENDING

        Args:
            execution_id: Container name from dispatch().

        Returns:
            Current job status.
        """

        def _status() -> JobStatus:
            """Blocking function to check container status."""
            client = self._get_client()
            container = client.containers.get(execution_id)
            container_status: str = container.status

            if container_status == "running":
                return JobStatus(
                    state=JobState.RUNNING,
                    execution_id=execution_id,
                )

            if container_status == "exited":
                exit_code: int = container.attrs.get("State", {}).get("ExitCode", -1)
                if exit_code == 0:
                    return JobStatus(
                        state=JobState.SUCCEEDED,
                        execution_id=execution_id,
                        exit_code=exit_code,
                    )
                return JobStatus(
                    state=JobState.FAILED,
                    execution_id=execution_id,
                    exit_code=exit_code,
                    error_message=f"Container exited with code {exit_code}",
                )

            return JobStatus(
                state=JobState.PENDING,
                execution_id=execution_id,
            )

        return await asyncio.to_thread(_status)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running Docker container.

        Stops the container and removes it.

        Args:
            execution_id: Container name from dispatch().
        """

        def _cancel() -> None:
            """Blocking function to stop and remove container."""
            client = self._get_client()
            container = client.containers.get(execution_id)
            container.stop()
            container.remove()

        await asyncio.to_thread(_cancel)
