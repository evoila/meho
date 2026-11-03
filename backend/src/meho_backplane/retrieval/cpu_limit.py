# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How many threads the embedding model may use: the container's CPU limit.

The ONNX runtime behind fastembed starts one thread per core of the
*host* by default. It does not read the container's CPU limit. A pod
with a limit of one CPU on a 32-core node then runs 32 busy threads,
uses up its CPU time in a few milliseconds of each scheduler period,
and the kernel pauses the *whole* process for the rest of the period.
The event loop is paused too, so ``/healthz`` and ``/ready`` stop
answering. A large spec ingest embeds one text per operation, so this
went on for minutes.

:func:`embedding_thread_count` reads the CPU limit of the container
(cgroup v2 ``cpu.max``, or cgroup v1 ``cpu.cfs_quota_us`` +
``cpu.cfs_period_us``) and returns the whole number of CPUs it allows,
at least 1. Without a limit it returns ``None``, and the ONNX runtime
keeps its own default.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["embedding_thread_count"]

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _quota_and_period(cgroup_root: Path) -> tuple[int, int] | None:
    """Return ``(quota, period)`` in microseconds, or ``None`` when there is no limit."""
    v2 = _read(cgroup_root / "cpu.max")
    if v2 is not None:
        parts = v2.split()
        if len(parts) != 2 or parts[0] == "max":
            return None
        quota_text, period_text = parts
    else:
        quota_text = _read(cgroup_root / "cpu" / "cpu.cfs_quota_us") or ""
        period_text = _read(cgroup_root / "cpu" / "cpu.cfs_period_us") or ""
    try:
        quota, period = int(quota_text), int(period_text)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        # cgroup v1 writes -1 for "no limit".
        return None
    return quota, period


def embedding_thread_count(cgroup_root: Path = _CGROUP_ROOT) -> int | None:
    """Return the number of threads the embedding model should use.

    The whole number of CPUs the container's CPU limit allows (rounded
    down, at least 1), and never more than the CPUs this process may
    run on. ``None`` when no limit is set or it cannot be read.
    """
    limit = _quota_and_period(cgroup_root)
    if limit is None:
        return None
    quota, period = limit
    threads = max(1, quota // period)
    usable = os.process_cpu_count()
    if usable is not None:
        threads = min(threads, usable)
    return threads
