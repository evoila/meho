# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Local subprocess backend for ingestion.

Wraps the existing subprocess-based Docling conversion
(meho_app.modules.knowledge.subprocess_converter) as an asyncio.Task
with JobStatus tracking. The "worker" runs in the same process as the
API using subprocess isolation for the actual Docling conversion.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from meho_app.worker.backends.protocol import (
    JobState,
    JobStatus,
    ResourceProfile,
)


class LocalBackend:
    """IngestionBackend that processes documents locally using asyncio tasks.

    Each dispatch() call creates an asyncio.Task that wraps the existing
    subprocess converter. The task downloads the input file, runs Docling
    conversion in a subprocess, chunks and serializes the output as Arrow
    IPC, and writes it to the output path.

    Attributes:
        _tasks: Map from execution_id to the asyncio.Task running the job.
        _results: Map from execution_id to error message (None = success).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, str | None] = {}
        self._started_at: dict[str, datetime] = {}
        self._finished_at: dict[str, datetime | None] = {}

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch a local ingestion job.

        Creates an asyncio.Task that processes the document using the
        existing subprocess converter pipeline.

        Args:
            job_id: Unique job identifier.
            input_url: File path (file://) or presigned URL to the source document.
            output_url: File path (file://) or presigned URL for Arrow IPC output.
            profile: Resource requirements (used for timeout).
            env_overrides: Additional environment variables (ignored for local backend).

        Returns:
            The job_id as execution_id.
        """
        self._started_at[job_id] = datetime.now(UTC)
        self._finished_at[job_id] = None
        self._results[job_id] = None

        task = asyncio.create_task(
            self._process(job_id, input_url, output_url, profile),
            name=f"local-ingest-{job_id[:8]}",
        )
        self._tasks[job_id] = task

        # Add a done callback to record completion
        task.add_done_callback(lambda t: self._on_task_done(job_id, t))

        return job_id

    def _on_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        """Record task completion or failure.

        Args:
            job_id: The job identifier.
            task: The completed asyncio.Task.
        """
        self._finished_at[job_id] = datetime.now(UTC)
        if task.cancelled():
            self._results[job_id] = "Job cancelled"
        elif task.exception() is not None:
            self._results[job_id] = str(task.exception())
        # else: success -- _results remains None

    async def _process(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
    ) -> None:
        """Process a document locally using subprocess isolation.

        Reads the source file, runs Docling conversion via subprocess,
        chunks the result, serializes to Arrow IPC, and writes to output.

        Args:
            job_id: Unique job identifier.
            input_url: Source document path or URL.
            output_url: Output Arrow IPC path or URL.
            profile: Resource requirements for timeout settings.
        """
        import pathlib

        from meho_app.modules.knowledge.subprocess_converter import (
            convert_file_in_subprocess,
        )
        from meho_app.worker.arrow_codec import serialize_chunks

        # Read input file
        input_path = input_url.removeprefix("file://")
        file_bytes = await asyncio.to_thread(pathlib.Path(input_path).read_bytes)
        filename = pathlib.Path(input_path).name
        mime_type = (
            "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"
        )

        # Convert using subprocess isolation
        doc = await convert_file_in_subprocess(
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            timeout_seconds=profile.timeout_seconds,
        )

        # Chunk and serialize (simplified -- full pipeline would chunk + embed)
        # For local backend, we produce a minimal Arrow output
        chunks: list[tuple[str, dict]] = []
        for _item_idx, item in enumerate(doc.texts):
            text = item.text if hasattr(item, "text") else str(item)
            if text.strip():
                chunks.append((text, {"document_name": filename}))

        # Generate zero embeddings for local mode (embedding done separately)
        embeddings = [[0.0] * 1024 for _ in chunks]

        output_bytes = serialize_chunks(chunks, embeddings)

        # Write output
        output_path = output_url.removeprefix("file://")
        await asyncio.to_thread(pathlib.Path(output_path).write_bytes, output_bytes)

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get status of a local ingestion job.

        Args:
            execution_id: The job_id returned by dispatch().

        Returns:
            JobStatus with the current state of the job.
        """
        if execution_id not in self._tasks:
            return JobStatus(
                state=JobState.FAILED,
                execution_id=execution_id,
                error_message=f"Unknown job: {execution_id}",
            )

        task = self._tasks[execution_id]
        started = self._started_at.get(execution_id)
        finished = self._finished_at.get(execution_id)

        if not task.done():
            return JobStatus(
                state=JobState.RUNNING,
                execution_id=execution_id,
                started_at=started,
            )

        if task.cancelled():
            return JobStatus(
                state=JobState.CANCELLED,
                execution_id=execution_id,
                started_at=started,
                finished_at=finished,
                error_message="Job cancelled",
            )

        error = self._results.get(execution_id)
        if error is not None:
            return JobStatus(
                state=JobState.FAILED,
                execution_id=execution_id,
                started_at=started,
                finished_at=finished,
                error_message=error,
            )

        return JobStatus(
            state=JobState.SUCCEEDED,
            execution_id=execution_id,
            started_at=started,
            finished_at=finished,
        )

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running local ingestion job.

        Args:
            execution_id: The job_id returned by dispatch().
        """
        task = self._tasks.get(execution_id)
        if task is not None and not task.done():
            task.cancel()
