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

:func:`embedding_thread_count` reads the CPU limit of the process's own
cgroup and of every cgroup above it (cgroup v2 ``cpu.max``, or cgroup v1
``cpu.cfs_quota_us`` + ``cpu.cfs_period_us``) and returns the whole
number of CPUs the smallest limit allows, at least 1. Without a limit it
returns ``None``, and the ONNX runtime keeps its own default.

The own cgroup comes from ``/proc/self/cgroup``. In a pod the container
has its own cgroup namespace, so that path is ``/`` and the limit is the
root ``cpu.max``. On a plain host, for example a process started with
``systemd-run -p CPUQuota=100%``, the limit sits on a cgroup a few levels
down, and the walk finds it there.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

__all__ = ["embedding_thread_count"]

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_CGROUP = Path("/proc/self/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _own_cgroup(proc_cgroup: Path, *, v2: bool) -> PurePosixPath:
    """Return the process's own cgroup path, ``/`` when it cannot be read.

    ``/proc/self/cgroup`` has one line per hierarchy:
    ``<id>:<controllers>:<path>``. cgroup v2 has one hierarchy, written
    ``0::<path>``. cgroup v1 names the controllers, for example
    ``3:cpu,cpuacct:<path>``.
    """
    for line in (_read(proc_cgroup) or "").splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if not path.startswith("/"):
            continue
        if v2 and hierarchy == "0" and controllers == "":
            return PurePosixPath(path)
        if not v2 and "cpu" in controllers.split(","):
            return PurePosixPath(path)
    return PurePosixPath("/")


def _cgroup_dirs(cgroup_root: Path, own: PurePosixPath) -> Iterator[Path]:
    """Yield the directory of the own cgroup and of each cgroup above it."""
    for part in (own, *own.parents):
        yield cgroup_root / part.relative_to("/")


def _parse_limit(quota_text: str, period_text: str) -> tuple[int, int] | None:
    """Return ``(quota, period)`` in microseconds, or ``None`` for no limit."""
    try:
        quota, period = int(quota_text), int(period_text)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        # cgroup v1 writes -1 for "no limit".
        return None
    return quota, period


def _v2_limit(cgroup_dir: Path) -> tuple[int, int] | None:
    text = _read(cgroup_dir / "cpu.max")
    if text is None:
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    return _parse_limit(parts[0], parts[1])


def _v1_limit(cgroup_dir: Path) -> tuple[int, int] | None:
    quota_text = _read(cgroup_dir / "cpu.cfs_quota_us")
    period_text = _read(cgroup_dir / "cpu.cfs_period_us")
    if quota_text is None or period_text is None:
        return None
    return _parse_limit(quota_text, period_text)


def _cpu_limits(cgroup_root: Path, proc_cgroup: Path) -> list[tuple[int, int]]:
    """Return every ``(quota, period)`` set on the own cgroup or a cgroup above it."""
    # Every cgroup v2 directory has ``cgroup.controllers``, the root too.
    # The root has no ``cpu.max`` on a host, only in a container.
    if (cgroup_root / "cgroup.controllers").exists():
        own = _own_cgroup(proc_cgroup, v2=True)
        limits = (_v2_limit(cgroup_dir) for cgroup_dir in _cgroup_dirs(cgroup_root, own))
    else:
        own = _own_cgroup(proc_cgroup, v2=False)
        limits = (_v1_limit(cgroup_dir) for cgroup_dir in _cgroup_dirs(cgroup_root / "cpu", own))
    return [limit for limit in limits if limit is not None]


def embedding_thread_count(
    cgroup_root: Path = _CGROUP_ROOT, proc_cgroup: Path = _PROC_CGROUP
) -> int | None:
    """Return the number of threads the embedding model should use.

    The whole number of CPUs the smallest CPU limit on the process's
    cgroups allows (rounded down, at least 1), and never more than the
    CPUs this process may run on. ``None`` when no limit is set or none
    can be read.
    """
    limits = _cpu_limits(cgroup_root, proc_cgroup)
    if not limits:
        return None
    threads = min(max(1, quota // period) for quota, period in limits)
    usable = os.process_cpu_count()
    if usable is not None:
        threads = min(threads, usable)
    return threads
