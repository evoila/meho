# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A small thread pool of its own for the blocking network probes.

``net.ping``, ``net.trace``, ``net.path_mtu`` and ``net.tls_inspect`` use
blocking sockets that can wait up to a minute for a silent peer. MEHO runs
them here, not on the default thread pool of the event loop. The default
pool also runs the Vault login, so a burst of slow probes must never fill
it. When all probe threads are busy, the next probe waits in this pool's
queue and the rest of MEHO is not affected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

__all__ = ["PROBE_POOL_WORKERS", "run_blocking_probe"]

#: Probe threads at one time. Well below the size of the default pool.
PROBE_POOL_WORKERS: Final[int] = 4

_POOL: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=PROBE_POOL_WORKERS, thread_name_prefix="net-probe"
)


async def run_blocking_probe[T](func: Callable[..., T], /, *args: Any) -> T:
    """Run the blocking *func* on the probe pool and return its result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_POOL, func, *args)
