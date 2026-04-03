# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""CloudRun coordinator service for ephemeral ingestion workers.

Manages GCS signed URLs, Cloud Run Job dispatch, and job status.
Runs on Cloud Run with min-instances=0 (scales to zero when idle).

Usage: python -m meho_app.worker.coordinator
       or: uvicorn meho_app.worker.coordinator:app --host 0.0.0.0 --port 8080

Environment variables:
    GCS_BUCKET: GCS bucket for document/result storage.
    CLOUDRUN_JOB_NAME: Full Cloud Run Job resource name
        (projects/{project}/locations/{region}/jobs/{job}).
    COORDINATOR_API_KEY: Shared secret for Bearer token authentication.
    SIGNED_URL_EXPIRATION_HOURS: Signed URL validity in hours (default 4).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

logger = logging.getLogger("meho_app.worker.coordinator")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_GCS_BUCKET: str = os.environ.get("GCS_BUCKET", "")
_CLOUDRUN_JOB_NAME: str = os.environ.get("CLOUDRUN_JOB_NAME", "")
_COORDINATOR_API_KEY: str = os.environ.get("COORDINATOR_API_KEY", "")
_SIGNED_URL_EXPIRATION_HOURS: int = int(os.environ.get("SIGNED_URL_EXPIRATION_HOURS", "4"))

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer()


def _verify_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """Verify Bearer token against COORDINATOR_API_KEY.

    Args:
        credentials: HTTP Bearer credentials from the request.

    Returns:
        The validated token string.

    Raises:
        HTTPException: If the token is missing or does not match.
    """
    if not _COORDINATOR_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Coordinator API key not configured (set COORDINATOR_API_KEY)",
        )
    if credentials.credentials != _COORDINATOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


# ---------------------------------------------------------------------------
# Job state tracking (in-memory cache, NOT source of truth)
# ---------------------------------------------------------------------------


class CoordinatorJobState(StrEnum):
    """Job state as tracked by the coordinator."""

    CREATED = "created"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class _JobRecord(BaseModel):
    """In-memory job metadata cache.

    This is a CACHE, not source of truth. On cold start, the coordinator
    has no prior state. Job status is derived from Cloud Run execution
    status API. The in-memory dict avoids redundant Cloud Run API calls
    for recently created jobs.
    """

    job_id: str
    filename: str
    content_type: str
    page_count: int
    state: CoordinatorJobState = CoordinatorJobState.CREATED
    upload_url: str = ""
    download_url: str = ""
    execution_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


_jobs: dict[str, _JobRecord] = {}

# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    """Request body for POST /jobs/create."""

    filename: str = Field(..., description="Original document filename")
    content_type: str = Field(default="application/pdf", description="MIME type of the document")
    page_count: int = Field(default=0, description="Number of pages (for resource estimation)")


class CreateJobResponse(BaseModel):
    """Response for POST /jobs/create."""

    job_id: str
    upload_url: str
    download_url: str


class DispatchResponse(BaseModel):
    """Response for POST /jobs/{job_id}/dispatch."""

    execution_id: str


class JobStatusResponse(BaseModel):
    """Response for GET /jobs/{job_id}/status."""

    state: str
    execution_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_url: str | None = None
    error_message: str | None = None


class CallbackRequest(BaseModel):
    """Request body for POST /jobs/{job_id}/callback."""

    state: str = Field(..., description="Final state: succeeded or failed")
    error_message: str | None = Field(default=None, description="Error details if failed")


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str = "ok"
    service: str = "meho-coordinator"


# ---------------------------------------------------------------------------
# GCS signed URL generation
# ---------------------------------------------------------------------------


def _generate_signed_urls(
    bucket_name: str,
    job_id: str,
    filename: str,
    expiry_hours: int = 4,
) -> tuple[str, str]:
    """Generate GCS signed URLs for document upload and result download.

    Args:
        bucket_name: GCS bucket name.
        job_id: Unique job identifier (used as path prefix).
        filename: Original document filename.
        expiry_hours: Signed URL validity in hours.

    Returns:
        Tuple of (upload_url, download_url).
    """
    from google.cloud import (  # type: ignore[attr-defined]  # google-cloud-storage stubs not available
        storage,
    )

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    input_blob = bucket.blob(f"ingestion/{job_id}/input/{filename}")
    output_blob = bucket.blob(f"ingestion/{job_id}/output/results.arrow")

    upload_url: str = input_blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=expiry_hours),
        method="PUT",
        content_type="application/pdf",
    )
    download_url: str = output_blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=expiry_hours),
        method="GET",
    )
    return upload_url, download_url


# ---------------------------------------------------------------------------
# Cloud Run Job dispatch
# ---------------------------------------------------------------------------


async def _dispatch_cloud_run_job(
    job_name: str,
    job_id: str,
    input_url: str,
    output_url: str,
) -> str:
    """Dispatch a Cloud Run Job execution with environment overrides.

    Args:
        job_name: Full Cloud Run Job resource name.
        job_id: MEHO job identifier.
        input_url: GCS signed URL for the input document.
        output_url: GCS signed URL for the output results.

    Returns:
        Cloud Run execution name as execution_id.
    """
    from google.cloud import run_v2

    client = run_v2.JobsAsyncClient()

    # Build request with environment overrides
    request = run_v2.RunJobRequest(
        name=job_name,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[
                        run_v2.EnvVar(name="WORKER_JOB_ID", value=job_id),
                        run_v2.EnvVar(name="WORKER_INPUT_URL", value=input_url),
                        run_v2.EnvVar(name="WORKER_OUTPUT_URL", value=output_url),
                        run_v2.EnvVar(name="OMP_NUM_THREADS", value="4"),
                        run_v2.EnvVar(name="MALLOC_TRIM_THRESHOLD_", value="131072"),
                    ],
                ),
            ],
            task_count=1,
        ),
    )

    operation = await client.run_job(request=request)
    execution_name: str = operation.metadata.name
    return execution_name


# ---------------------------------------------------------------------------
# Cloud Run execution status
# ---------------------------------------------------------------------------


async def _get_execution_status(execution_id: str) -> dict[str, Any]:
    """Get status of a Cloud Run execution.

    Maps Cloud Run execution state to coordinator job state.

    Args:
        execution_id: Cloud Run execution name.

    Returns:
        Dict with state, started_at, completed_at fields.
    """
    from google.cloud import run_v2

    client = run_v2.ExecutionsAsyncClient()
    execution = await client.get_execution(name=execution_id)

    reconciling: bool = getattr(execution, "reconciling", False)
    succeeded_count: int = getattr(execution, "succeeded_count", 0) or 0
    failed_count: int = getattr(execution, "failed_count", 0) or 0

    if not reconciling and succeeded_count > 0:
        state = CoordinatorJobState.SUCCEEDED
    elif not reconciling and failed_count > 0:
        state = CoordinatorJobState.FAILED
    elif reconciling:
        state = CoordinatorJobState.RUNNING
    else:
        state = CoordinatorJobState.DISPATCHED

    # Extract timestamps if available
    start_time = getattr(execution, "start_time", None)
    completion_time = getattr(execution, "completion_time", None)

    return {
        "state": state,
        "started_at": start_time,
        "completed_at": completion_time,
    }


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MEHO Ingestion Coordinator",
    description="CloudRun coordinator for ephemeral ingestion workers",
    version="1.0.0",
)


@app.get("/health")
async def health_check() -> HealthResponse:
    """Health check endpoint (required for Cloud Run).

    Returns:
        Health status response.
    """
    return HealthResponse()


@app.post(
    "/jobs/create",
    dependencies=[Depends(_verify_api_key)],
    responses={503: {"description": "GCS_BUCKET not configured"}},
)
async def create_job(
    request: CreateJobRequest,
) -> CreateJobResponse:
    """Create a new ingestion job with GCS signed URLs.

    Generates a unique job_id, creates GCS signed URLs for document
    upload (PUT) and result download (GET), and caches job metadata.

    Args:
        request: Job creation parameters.

    Returns:
        Job ID and signed URLs for the caller.
    """
    if not _GCS_BUCKET:
        raise HTTPException(
            status_code=503,
            detail="GCS_BUCKET not configured",
        )

    job_id = str(uuid.uuid4())

    upload_url, download_url = _generate_signed_urls(
        bucket_name=_GCS_BUCKET,
        job_id=job_id,
        filename=request.filename,
        expiry_hours=_SIGNED_URL_EXPIRATION_HOURS,
    )

    record = _JobRecord(
        job_id=job_id,
        filename=request.filename,
        content_type=request.content_type,
        page_count=request.page_count,
        upload_url=upload_url,
        download_url=download_url,
    )
    _jobs[job_id] = record

    logger.info(
        "job_created",
        extra={"job_id": job_id, "filename": request.filename},
    )

    return CreateJobResponse(
        job_id=job_id,
        upload_url=upload_url,
        download_url=download_url,
    )


@app.post(
    "/jobs/{job_id}/dispatch",
    dependencies=[Depends(_verify_api_key)],
    responses={
        404: {"description": "Job not found"},
        409: {"description": "Job already dispatched"},
        503: {"description": "CLOUDRUN_JOB_NAME not configured"},
    },
)
async def dispatch_job(job_id: str) -> DispatchResponse:
    """Dispatch the Cloud Run Job for an existing job.

    Creates environment overrides with WORKER_INPUT_URL, WORKER_OUTPUT_URL,
    WORKER_JOB_ID and calls the Cloud Run Jobs API.

    Args:
        job_id: Job identifier from create_job.

    Returns:
        Cloud Run execution ID.
    """
    if not _CLOUDRUN_JOB_NAME:
        raise HTTPException(
            status_code=503,
            detail="CLOUDRUN_JOB_NAME not configured",
        )

    record = _jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if record.state != CoordinatorJobState.CREATED:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} already dispatched (state: {record.state})",
        )

    # Build GCS paths for the worker
    input_gcs_url = f"gs://{_GCS_BUCKET}/ingestion/{job_id}/input/{record.filename}"
    output_gcs_url = f"gs://{_GCS_BUCKET}/ingestion/{job_id}/output/results.arrow"

    execution_id = await _dispatch_cloud_run_job(
        job_name=_CLOUDRUN_JOB_NAME,
        job_id=job_id,
        input_url=input_gcs_url,
        output_url=output_gcs_url,
    )

    record.execution_id = execution_id
    record.state = CoordinatorJobState.DISPATCHED
    record.started_at = datetime.now(UTC)

    logger.info(
        "job_dispatched",
        extra={"job_id": job_id, "execution_id": execution_id},
    )

    return DispatchResponse(execution_id=execution_id)


@app.get(
    "/jobs/{job_id}/status",
    dependencies=[Depends(_verify_api_key)],
    responses={404: {"description": "Job not found"}},
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Get current status of an ingestion job.

    If an execution_id exists, queries the Cloud Run Executions API
    for the current status. The Cloud Run execution is the source of
    truth; the in-memory cache is secondary.

    Args:
        job_id: Job identifier from create_job.

    Returns:
        Current job status with optional result URL.
    """
    record = _jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # If we have an execution, query Cloud Run for actual status
    if record.execution_id:
        try:
            cr_status = await _get_execution_status(record.execution_id)
            record.state = CoordinatorJobState(cr_status["state"])
            if cr_status.get("started_at"):
                record.started_at = cr_status["started_at"]
            if cr_status.get("completed_at"):
                record.completed_at = cr_status["completed_at"]
        except Exception:
            logger.exception(
                "failed_to_get_execution_status",
                extra={"job_id": job_id, "execution_id": record.execution_id},
            )
            # Fall back to cached state

    # Include result URL if succeeded
    result_url: str | None = None
    if record.state == CoordinatorJobState.SUCCEEDED:
        result_url = record.download_url

    return JobStatusResponse(
        state=record.state.value,
        execution_id=record.execution_id,
        started_at=record.started_at,
        completed_at=record.completed_at,
        result_url=result_url,
        error_message=record.error_message,
    )


@app.post(
    "/jobs/{job_id}/callback",
    dependencies=[Depends(_verify_api_key)],
    responses={404: {"description": "Job not found"}},
)
async def job_callback(
    job_id: str,
    request: Request,
    body: CallbackRequest,
) -> dict[str, str]:
    """Receive worker completion/failure callback.

    Updates in-memory job state (cache only). The actual source of
    truth is Cloud Run execution status.

    Args:
        job_id: Job identifier.
        request: FastAPI request (unused but required for signature).
        body: Callback payload with final state.

    Returns:
        Acknowledgement.
    """
    record = _jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if body.state == "succeeded":
        record.state = CoordinatorJobState.SUCCEEDED
        record.completed_at = datetime.now(UTC)
    elif body.state == "failed":
        record.state = CoordinatorJobState.FAILED
        record.completed_at = datetime.now(UTC)
        record.error_message = body.error_message

    logger.info(
        "job_callback_received",
        extra={
            "job_id": job_id,
            "state": body.state,
            "error": body.error_message,
        },
    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)  # noqa: S104
