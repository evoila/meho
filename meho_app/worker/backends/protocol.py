# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Protocol definitions for the ephemeral ingestion worker.

Defines the IngestionBackend protocol (dispatch/get_status/cancel),
job state enum, resource profile, and job status dataclass.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class JobState(StrEnum):
    """State of an ingestion job execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ResourceProfile:
    """Resource requirements for an ingestion worker.

    Frozen (immutable) after creation -- callers cannot accidentally mutate.

    Attributes:
        memory_gb: Memory limit in GB for the worker container.
        cpu: Number of CPU cores to allocate.
        gpu: Whether GPU acceleration is requested.
        timeout_seconds: Maximum wall-clock time before the job is killed.
        size_category: Human-readable size label (tiny/small/medium/large/huge).
    """

    memory_gb: int
    cpu: int
    gpu: bool
    timeout_seconds: int
    size_category: str


@dataclass
class JobStatus:
    """Status of a running or completed ingestion job.

    Attributes:
        state: Current job state.
        execution_id: Backend-specific execution identifier.
        started_at: When the job started running (None if still pending).
        finished_at: When the job completed (None if still running).
        error_message: Error details if the job failed.
        exit_code: Process exit code if available.
    """

    state: JobState
    execution_id: str
    started_at: datetime | None = field(default=None)
    finished_at: datetime | None = field(default=None)
    error_message: str | None = field(default=None)
    exit_code: int | None = field(default=None)


@runtime_checkable
class IngestionBackend(Protocol):
    """Protocol for ephemeral ingestion worker backends.

    All backends (Kubernetes, Cloud Run, Docker, local subprocess) must
    implement these three methods. The protocol is runtime-checkable so
    the dispatcher can verify backend implementations at startup.

    Implementations:
        - KubernetesBackend (K8s Job API)
        - CloudRunBackend (GCP Cloud Run Jobs API)
        - DockerBackend (SSH + docker run)
        - LocalBackend (subprocess)
    """

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch an ingestion job to the backend.

        Args:
            job_id: Unique job identifier from the MEHO knowledge module.
            input_url: Presigned URL or path to the source document.
            output_url: Presigned URL or path for the Arrow IPC output.
            profile: Resource requirements for the worker container.
            env_overrides: Additional environment variables for the worker.

        Returns:
            Backend-specific execution ID for status tracking.
        """
        ...

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get current status of a dispatched job.

        Args:
            execution_id: Backend-specific execution ID from dispatch().

        Returns:
            Current job status with state, timestamps, and error info.
        """
        ...

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running job.

        Args:
            execution_id: Backend-specific execution ID from dispatch().
        """
        ...
