# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run the CPU work of an upload in a separate worker process.

MEHO serves every tenant from one event loop. Unpacking, checking and
packing a large archive takes seconds of CPU time. A worker thread does
not help: the regular expressions of the secret check hold Python's
interpreter lock for a whole search. So the work runs in a worker
process, and the event loop only waits for the answer.

Each call gets its own new worker process, which stops after the call.
So when a worker process dies (for example, it ran out of memory), only
that one upload fails. A shared pool would fail every upload in it. A
new process costs a fraction of a second, which is small next to the
work itself.

At most :data:`WORKERS` calls run at a time; more wait for a free place.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any, Final

__all__ = ["WORKERS", "run_in_worker"]

#: How many uploads run at the same time.
WORKERS: Final[int] = 2

# "spawn" starts each worker as a fresh interpreter. A fork of the server
# process would copy its threads' locks in any state.
_SPAWN: Final = multiprocessing.get_context("spawn")

_places: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _free_places() -> asyncio.Semaphore:
    """The places of the running event loop. MEHO has one; tests start many."""
    global _places
    loop = asyncio.get_running_loop()
    if _places is None or _places[0] is not loop:
        _places = (loop, asyncio.Semaphore(WORKERS))
    return _places[1]


def _release_when_done(
    loop: asyncio.AbstractEventLoop, places: asyncio.Semaphore
) -> Callable[[Future[Any]], None]:
    def release(_: Future[Any]) -> None:
        # A closed loop has nobody left to wait for a place.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(places.release)

    return release


async def run_in_worker[**P, R](fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``fn(*args, **kwargs)`` in a new worker process and return its result.

    *fn* must be a module-level function, and its arguments and result
    must pickle. An exception that *fn* raises comes back as it is. When
    the worker process dies, this call raises
    :class:`concurrent.futures.process.BrokenProcessPool`.
    """
    loop = asyncio.get_running_loop()
    places = _free_places()
    await places.acquire()
    try:
        pool = ProcessPoolExecutor(max_workers=1, mp_context=_SPAWN)
        future = pool.submit(fn, *args, **kwargs)
    except BaseException:
        places.release()
        raise
    # The place stays taken until the process ends, also when the caller
    # gives up waiting: the work in the process goes on to the end.
    future.add_done_callback(_release_when_done(loop, places))
    # The process runs the one call it has, then stops.
    pool.shutdown(wait=False)
    return await asyncio.wrap_future(future)
