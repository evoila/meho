# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""In-process registry for background ingestion asyncio tasks.

A single :class:`IngestionTaskRegistry` instance tracks background ingestion
tasks by ``job_id`` so that cancel / resume endpoints on the same worker can
look up a running task and cancel it cooperatively.

**Process-local only.** Each FastAPI worker has its own registry; a cancel
request that lands on a different worker than the one running the task will
find no entry and fall back to a 202 no-op (see
``meho_app/api/routes_knowledge.py::cancel_ingestion_job``). Cross-worker
cancellation would require a shared transport (Redis pub/sub, a DB-backed
cancel flag polled at checkpoint boundaries, ...) and is explicitly out of
scope for PR #342.

The class wraps what used to be a module-level ``dict`` plus a helper function
in ``routes_knowledge.py``. Making it a class gives us:

* an injectable seam for tests (``IngestionTaskRegistry()`` in a fixture);
* a named home for the "process-local only" caveat;
* a home for the ``pop`` callback hook so ``add_done_callback`` wiring lives
  next to the data it mutates.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class IngestionTaskRegistry:
    """Tracks background ingestion tasks by ``job_id`` within a single process.

    Not safe across multiple uvicorn workers: each worker holds its own
    registry instance. Always treat a missing ``job_id`` as "the task might be
    running on another worker, I don't know".
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def register(self, job_id: str, coro: Any) -> asyncio.Task[Any]:
        """Wrap ``coro`` in a task, store it under ``job_id``, and return it.

        Automatically removes the entry when the task completes (success,
        failure, or cancellation) so callers never need to clean up manually.
        """
        task = asyncio.create_task(coro)
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))
        return task

    def get(self, job_id: str) -> asyncio.Task[Any] | None:
        """Return the live task for ``job_id`` if this worker owns it."""
        return self._tasks.get(job_id)

    def pop(self, job_id: str) -> asyncio.Task[Any] | None:
        """Remove and return the task for ``job_id`` if present."""
        return self._tasks.pop(job_id, None)

    def __contains__(self, job_id: object) -> bool:
        return job_id in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)


_default_registry: IngestionTaskRegistry | None = None


def get_task_registry() -> IngestionTaskRegistry:
    """Return the process-wide default registry, creating it on first use.

    Route handlers use this accessor so tests can monkey-patch
    ``_default_registry`` with a fresh instance to isolate state.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = IngestionTaskRegistry()
    return _default_registry


def reset_task_registry() -> None:
    """Drop the process-wide registry. Intended for test isolation only."""
    global _default_registry
    _default_registry = None
