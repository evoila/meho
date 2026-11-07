# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The step an async ingest job is on, reported by the pipeline (#5786).

A large API file import can run for many minutes. The job status shows
which step runs now, so an operator can tell a slow import from a stuck
one. The steps, in order:

* ``parse`` -- read each API file and check its version.
* ``register`` -- parse each file and write its operations.
* ``grouping`` -- ask the language model to sort the operations into
  groups.

The pipeline calls :func:`report_ingest_phase` when a step starts.
:func:`~meho_backplane.operations.ingest.jobs.run_ingest_job` installs
the reporter for its job with :func:`ingest_phase_reporter`. The
reporter lives in a context variable, so the pipeline needs no job id
and the sync ingest path (no job) reports to nobody. This module
imports nothing from ``jobs`` or ``pipeline``, so both can import it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

__all__ = [
    "IngestJobPhase",
    "ingest_phase_reporter",
    "report_ingest_phase",
]

IngestJobPhase = Literal["parse", "register", "grouping"]

PhaseReporter = Callable[[IngestJobPhase], Awaitable[None]]

_reporter: ContextVar[PhaseReporter | None] = ContextVar(
    "ingest_job_phase_reporter",
    default=None,
)


async def report_ingest_phase(phase: IngestJobPhase) -> None:
    """Tell the running ingest job that *phase* starts now. No job: no-op."""
    reporter = _reporter.get()
    if reporter is not None:
        await reporter(phase)


@contextmanager
def ingest_phase_reporter(reporter: PhaseReporter) -> Iterator[None]:
    """Send every :func:`report_ingest_phase` call in this block to *reporter*."""
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)
