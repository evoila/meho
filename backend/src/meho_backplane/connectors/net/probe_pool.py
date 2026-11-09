# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A small thread pool of its own for the blocking network probes.

``net.ping``, ``net.trace``, ``net.path_mtu`` and ``net.tls_inspect`` use
blocking sockets that can wait up to a minute for a silent peer. MEHO runs
them here, not on the default thread pool of the event loop. The default
pool also runs the Vault login, so a burst of slow probes must never fill
it.

Each tenant gets a share of the pool: at most :data:`PROBE_TENANT_SHARE`
probes at one time, running or waiting. The share is below the number of
threads, so one tenant never holds every thread. A probe over its tenant's
share is refused at once with :class:`ProbePoolBusyError` (error code
``probe_pool_busy``); it does not wait in the queue. A probe that does wait
in the queue counts that time against its own timeout: when it has waited
longer than its timeout, it does not start and is refused the same way.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Final
from uuid import UUID

__all__ = [
    "PROBE_POOL_BUSY",
    "PROBE_POOL_WORKERS",
    "PROBE_TENANT_SHARE",
    "ProbePoolBusyError",
    "run_blocking_probe",
]

#: Probe threads at one time. Well below the size of the default pool.
PROBE_POOL_WORKERS: Final[int] = 4

#: Probes one tenant may have in the pool at one time (running or waiting).
PROBE_TENANT_SHARE: Final[int] = 2

#: The error code of a refused probe.
PROBE_POOL_BUSY: Final[str] = "probe_pool_busy"

_POOL: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=PROBE_POOL_WORKERS, thread_name_prefix="net-probe"
)

# Probes per tenant in the pool. The release runs on a pool thread, so a
# lock guards the counts.
_IN_POOL: dict[UUID, int] = {}
_IN_POOL_LOCK: Final = threading.Lock()


class ProbePoolBusyError(RuntimeError):
    """The probe did not run: its tenant's share of the probe pool was in use.

    The dispatcher returns it as the ``probe_pool_busy`` error. No socket was
    opened, so it is not a reachability answer.
    """

    error_code: Final[str] = PROBE_POOL_BUSY

    def __init__(self, reason: str) -> None:
        super().__init__(f"{PROBE_POOL_BUSY}: {reason}")
        self.reason = reason


def _take_slot(tenant_id: UUID) -> None:
    with _IN_POOL_LOCK:
        in_pool = _IN_POOL.get(tenant_id, 0)
        if in_pool >= PROBE_TENANT_SHARE:
            raise ProbePoolBusyError(
                f"this tenant already has {in_pool} network probes running or waiting, "
                f"the most it may have at one time. Retry when one of them has ended."
            )
        _IN_POOL[tenant_id] = in_pool + 1


def _release_slot(tenant_id: UUID) -> None:
    with _IN_POOL_LOCK:
        left = _IN_POOL.get(tenant_id, 1) - 1
        if left > 0:
            _IN_POOL[tenant_id] = left
        else:
            _IN_POOL.pop(tenant_id, None)


def _run_if_in_time[T](
    func: Callable[..., T], args: tuple[Any, ...], queued_at: float, timeout: float
) -> T:
    waited = time.monotonic() - queued_at
    if waited > timeout:
        raise ProbePoolBusyError(
            f"the probe waited {waited:.1f} seconds for a free probe thread, "
            f"longer than its timeout of {timeout:g} seconds, so it did not start."
        )
    return func(*args)


async def run_blocking_probe[T](
    func: Callable[..., T], /, *args: Any, tenant_id: UUID, timeout: float
) -> T:
    """Run the blocking *func* on the probe pool and return its result.

    *tenant_id* is the tenant the probe runs for; it counts against that
    tenant's share. *timeout* is the probe's own timeout; a probe that waits
    longer than that for a thread does not start. Raises
    :class:`ProbePoolBusyError` in both cases.
    """
    _take_slot(tenant_id)
    try:
        future: Future[T] = _POOL.submit(_run_if_in_time, func, args, time.monotonic(), timeout)
    except BaseException:
        _release_slot(tenant_id)
        raise
    # The slot is held until the thread is done, also when the caller stops
    # waiting: the probe keeps the thread until its socket times out.
    future.add_done_callback(lambda _f: _release_slot(tenant_id))
    return await asyncio.wrap_future(future)
