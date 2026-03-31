# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Cloud Run Jobs backend for ephemeral ingestion.

Dispatches document conversion to GCP Cloud Run Jobs via the
google-cloud-run async SDK. Each execution processes a single
document and exits, reclaiming all memory.

The google.cloud.run_v2 SDK is imported lazily inside methods to
avoid import-time failure when the SDK is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from meho_app.worker.backends.protocol import (
    JobState,
    JobStatus,
    ResourceProfile,
)


@dataclass
class _EnvVar:
    """Lightweight env var for building Cloud Run request without SDK import."""

    name: str
    value: str


@dataclass
class _ContainerOverride:
    """Container override for Cloud Run request."""

    env: list[_EnvVar] = field(default_factory=list)


@dataclass
class _Duration:
    """Protobuf Duration equivalent."""

    seconds: int


@dataclass
class _Overrides:
    """Run job overrides."""

    container_overrides: list[_ContainerOverride] = field(default_factory=list)
    task_count: int = 1
    timeout: _Duration | None = None


@dataclass
class _RunJobRequest:
    """Run job request."""

    name: str = ""
    overrides: _Overrides | None = None


class CloudRunBackend:
    """IngestionBackend that dispatches work to Cloud Run Jobs.

    Requires a pre-created Cloud Run Job in the target GCP project.
    Each dispatch() call triggers a new execution with container
    overrides for environment variables and timeout.

    Attributes:
        project: GCP project ID.
        region: GCP region (e.g. us-central1).
        job_name: Pre-created Cloud Run Job name.
    """

    def __init__(
        self,
        project: str,
        region: str,
        job_name: str,
    ) -> None:
        self.project = project
        self.region = region
        self.job_name = job_name

    def _get_jobs_client(self) -> Any:
        """Create a Cloud Run Jobs async client.

        Returns:
            A JobsAsyncClient instance.
        """
        from google.cloud import run_v2  # type: ignore[import-untyped]

        return run_v2.JobsAsyncClient()

    def _get_executions_client(self) -> Any:
        """Create a Cloud Run Executions async client.

        Returns:
            An ExecutionsAsyncClient instance.
        """
        from google.cloud import run_v2  # type: ignore[import-untyped]

        return run_v2.ExecutionsAsyncClient()

    def _build_run_job_request(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> _RunJobRequest:
        """Build a RunJobRequest structure.

        Uses lightweight dataclasses to avoid importing the google SDK
        at call time. The SDK's RunJobRequest accepts these as duck-typed
        arguments.

        Args:
            job_id: Unique job identifier.
            input_url: Source document URL.
            output_url: Output Arrow IPC URL.
            profile: Resource requirements.
            env_overrides: Additional environment variables.

        Returns:
            A RunJobRequest-like dataclass.
        """
        # Build environment variable overrides
        env_vars = [
            _EnvVar(name="WORKER_JOB_ID", value=job_id),
            _EnvVar(name="WORKER_INPUT_URL", value=input_url),
            _EnvVar(name="WORKER_OUTPUT_URL", value=output_url),
            _EnvVar(name="OMP_NUM_THREADS", value="4"),
            _EnvVar(name="MALLOC_TRIM_THRESHOLD_", value="131072"),
        ]

        if env_overrides:
            for key, value in env_overrides.items():
                env_vars.append(_EnvVar(name=key, value=value))

        container_override = _ContainerOverride(env=env_vars)

        overrides = _Overrides(
            container_overrides=[container_override],
            task_count=1,
            timeout=_Duration(seconds=profile.timeout_seconds),
        )

        job_full_name = f"projects/{self.project}/locations/{self.region}/jobs/{self.job_name}"

        return _RunJobRequest(
            name=job_full_name,
            overrides=overrides,
        )

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch an ingestion job to Cloud Run.

        Triggers a new execution of the pre-created Cloud Run Job with
        container overrides for environment variables and timeout.

        Args:
            job_id: Unique job identifier.
            input_url: Presigned URL or GCS path to the source document.
            output_url: Presigned URL or GCS path for the Arrow IPC output.
            profile: Resource requirements (used for timeout).
            env_overrides: Additional environment variables for the worker.

        Returns:
            Cloud Run execution name as execution_id.
        """
        request = self._build_run_job_request(
            job_id=job_id,
            input_url=input_url,
            output_url=output_url,
            profile=profile,
            env_overrides=env_overrides,
        )

        client = self._get_jobs_client()
        operation = await client.run_job(request=request)

        # Extract execution name from the operation metadata
        execution_name: str = operation.metadata.name
        return execution_name

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get status of a Cloud Run execution.

        Maps Cloud Run execution state:
        - not reconciling + succeeded_count > 0 -> SUCCEEDED
        - not reconciling + failed_count > 0 -> FAILED
        - reconciling -> RUNNING
        - else -> PENDING

        Args:
            execution_id: Cloud Run execution name from dispatch().

        Returns:
            Current job status.
        """
        client = self._get_executions_client()
        execution = await client.get_execution(name=execution_id)

        reconciling: bool = getattr(execution, "reconciling", False)
        succeeded_count: int = getattr(execution, "succeeded_count", 0) or 0
        failed_count: int = getattr(execution, "failed_count", 0) or 0

        if not reconciling and succeeded_count > 0:
            state = JobState.SUCCEEDED
        elif not reconciling and failed_count > 0:
            state = JobState.FAILED
        elif reconciling:
            state = JobState.RUNNING
        else:
            state = JobState.PENDING

        return JobStatus(
            state=state,
            execution_id=execution_id,
        )

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running Cloud Run execution.

        Args:
            execution_id: Cloud Run execution name from dispatch().
        """
        client = self._get_executions_client()
        await client.cancel_execution(name=execution_id)
