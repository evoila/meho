"""Run a blocking library call in a worker thread, after the block check.

A vendor library such as ``hvac`` sends its requests from a worker thread,
where the block check cannot run. :func:`to_thread_checked` runs the check
on the event loop, right before the thread starts. A login that an admin
blocked while the call ran gets the block refusal, and nothing is sent.
Outside an operation the check does nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from meho_backplane.auth.principal_block import raise_if_send_refused
from meho_backplane.auth.principal_block_run import mark_request_sent


async def check_before_send() -> None:
    """Check the block, then note that the running call sends. Use it right before a send."""
    await raise_if_send_refused()
    mark_request_sent()


async def to_thread_checked[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Check the block, note the send, then run ``func(*args, **kwargs)`` in a thread."""
    await check_before_send()
    return await asyncio.to_thread(func, *args, **kwargs)
