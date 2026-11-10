# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A small thread pool of its own for the blocking network probes.

``net.ping``, ``net.trace``, ``net.path_mtu`` and ``net.tls_inspect`` use
blocking sockets that can wait up to a minute for a silent peer. MEHO runs
them here, not on the default thread pool of the event loop. The default
pool also runs the Vault login, so a burst of slow probes must never fill
it.

Each tenant gets a share of the pool: at most :data:`PROBE_TENANT_SHARE`
probes in the pool at one time, running or waiting for a thread. The share
is below the number of threads, so one tenant never holds every thread. A
probe over its tenant's share waits for a free place in that share outside
the pool, so it holds no thread while it waits. All time a probe waits, for
its share and then for a thread, counts against its own timeout. When that
time runs out, the probe does not start and is refused with
:class:`ProbePoolBusyError` (error code ``probe_pool_busy``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
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

#: Probes one tenant may have in the pool at one time (running or waiting
#: for a thread). More probes of the tenant wait outside the pool.
PROBE_TENANT_SHARE: Final[int] = 2

#: The error code of a refused probe.
PROBE_POOL_BUSY: Final[str] = "probe_pool_busy"

_POOL: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=PROBE_POOL_WORKERS, thread_name_prefix="net-probe"
)

# Probes per tenant in the pool, and the probes of each tenant that wait for
# a place in its share (first come, first served). The release runs on a
# pool thread, so a lock guards both.
_IN_POOL: dict[UUID, int] = {}
_WAITING: dict[UUID, deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]]] = {}
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


def _grant(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


async def _take_slot(tenant_id: UUID, timeout: float) -> None:
    """Take a place in the tenant's share; wait at most *timeout* seconds."""
    with _IN_POOL_LOCK:
        in_pool = _IN_POOL.get(tenant_id, 0)
        if in_pool < PROBE_TENANT_SHARE and not _WAITING.get(tenant_id):
            _IN_POOL[tenant_id] = in_pool + 1
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        entry = (loop, waiter)
        _WAITING.setdefault(tenant_id, deque()).append(entry)
    try:
        async with asyncio.timeout(timeout):
            await waiter
    except BaseException as exc:
        with _IN_POOL_LOCK:
            queue = _WAITING.get(tenant_id, deque())
            granted = entry not in queue
            if not granted:
                queue.remove(entry)
                if not queue:
                    _WAITING.pop(tenant_id, None)
        if granted:
            # A place was handed over just as the wait ended: give it on.
            _release_slot(tenant_id)
        if isinstance(exc, TimeoutError):
            raise ProbePoolBusyError(
                f"this tenant already had {PROBE_TENANT_SHARE} network probes running "
                f"or waiting, and none ended within the probe's timeout of {timeout:g} "
                f"seconds, so it did not start. Retry when one of them has ended."
            ) from None
        raise


def _release_slot(tenant_id: UUID) -> None:
    """Free a place in the tenant's share, or hand it to the next waiting probe.

    Runs on a pool thread or on the event loop.
    """
    with _IN_POOL_LOCK:
        queue = _WAITING.get(tenant_id)
        while queue:
            loop, waiter = queue.popleft()
            if not queue:
                _WAITING.pop(tenant_id, None)
            try:
                loop.call_soon_threadsafe(_grant, waiter)
            except RuntimeError:
                # The waiter's event loop is closed; try the next one.
                continue
            return
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
    tenant's share; over the share it waits outside the pool. *timeout* is
    the probe's own timeout; a probe that waits longer than that, for its
    share and for a thread together, does not start. Raises
    :class:`ProbePoolBusyError` in that case.
    """
    started_at = time.monotonic()
    await _take_slot(tenant_id, timeout)
    try:
        future: Future[T] = _POOL.submit(_run_if_in_time, func, args, started_at, timeout)
    except BaseException:
        _release_slot(tenant_id)
        raise
    # The slot is held until the thread is done, also when the caller stops
    # waiting: the probe keeps the thread until its socket times out.
    future.add_done_callback(lambda _f: _release_slot(tenant_id))
    return await asyncio.wrap_future(future)
