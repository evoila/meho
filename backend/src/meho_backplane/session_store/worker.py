# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run the CPU work of an upload in a separate worker process.

MEHO serves every tenant from one event loop. Unpacking, checking and
packing a large archive takes seconds of CPU time. A worker thread does
not help: the regular expressions of the secret check hold Python's
interpreter lock for a whole search. So the work runs in a small pool of
worker processes, and the event loop only waits for the answer.

At most :data:`WORKERS` uploads run at a time; more wait in the pool's
queue. The pool starts on the first upload. When a worker process dies
(for example, it ran out of memory), that upload fails and the next one
starts a new pool.
"""

from __future__ import annotations

import asyncio
import functools
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Final

__all__ = ["WORKERS", "run_in_worker", "shutdown_worker_pool"]

#: How many uploads run at the same time.
WORKERS: Final[int] = 2

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            # "spawn" starts each worker as a fresh interpreter. A fork of the
            # server process would copy its threads' locks in any state.
            _pool = ProcessPoolExecutor(
                max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn")
            )
        return _pool


def _drop_pool(pool: ProcessPoolExecutor) -> None:
    global _pool
    with _lock:
        if _pool is pool:
            _pool = None
    pool.shutdown(wait=False, cancel_futures=True)


async def run_in_worker[**P, R](fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``fn(*args, **kwargs)`` in a worker process and return its result.

    *fn* must be a module-level function, and its arguments and result
    must pickle. An exception that *fn* raises comes back as it is.
    """
    pool = _get_pool()
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, functools.partial(fn, *args, **kwargs))
    except BrokenProcessPool:
        _drop_pool(pool)
        raise


def shutdown_worker_pool() -> None:
    """Stop the worker processes. The next upload starts a new pool."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
