# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The block check for each request a vendor library sends from a worker thread.

A vendor library such as ``hvac`` sends its requests from a worker thread,
where the block check cannot run. :func:`to_thread_checked` runs the call in
a worker thread and lets that thread run the check on the event loop:

* once when the worker starts the call, so a call that waited for a free
  worker is checked when it can run, and
* right before each request that a client of :func:`checked_requests_adapter`
  sends, so a call that sends more than one request (a read, then a write)
  stops before the next request when the caller was blocked in between.

Only a request counts as sent (:func:`mark_request_sent`). A login that an
admin blocked gets the block refusal, and the request is not sent. Outside
an operation, and in a clean-up
(:func:`~meho_backplane.auth.principal_block_run.cleanup_without_block_check`),
the check does nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, Final

import requests
import requests.adapters

from meho_backplane.auth.principal_block import PrincipalBlockedStepError, raise_if_send_refused
from meho_backplane.auth.principal_block_run import (
    in_cleanup,
    mark_request_sent,
    mark_step_refused,
    run_login,
)

__all__ = ["check_before_send", "checked_requests_adapter", "to_thread_checked"]

#: Longest time, in seconds, a worker waits for the check on the event loop.
#: A check that does not end in this time refuses the request, and the run
#: notes the refusal (``principal_block_check_unavailable``).
_CHECK_WAIT_SECONDS: Final[float] = 60.0

#: The event loop of the call that started the running worker thread.
_loop_var: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "send_check_loop", default=None
)


async def check_before_send() -> None:
    """Check the block, then note that the running call sends. Use it right before a send."""
    await raise_if_send_refused()
    mark_request_sent()


def _check_in_worker() -> None:
    """Run the block check on the event loop and wait for it, from a worker thread."""
    loop = _loop_var.get()
    if loop is None or run_login() is None or in_cleanup():
        return
    future = asyncio.run_coroutine_threadsafe(raise_if_send_refused(), loop)
    try:
        future.result(timeout=_CHECK_WAIT_SECONDS)
    except TimeoutError:
        future.cancel()
        # Noted for the run like an unreadable block list, so a caller that
        # catches the error does not go on, and no later request is sent.
        refusal = PrincipalBlockedStepError(unavailable=True)
        mark_step_refused(refusal)
        raise refusal from None


class _CheckedAdapter(requests.adapters.HTTPAdapter):
    """A ``requests`` adapter that checks the block right before each request."""

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> Any:
        _check_in_worker()
        mark_request_sent()
        return super().send(request, *args, **kwargs)


def checked_requests_adapter() -> requests.adapters.HTTPAdapter:
    """An adapter to mount on a ``requests`` session whose requests MEHO checks."""
    return _CheckedAdapter()


async def to_thread_checked[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run ``func(*args, **kwargs)`` in a worker thread that checks the block.

    The check runs when the worker starts the call, and before each request
    of a client with :func:`checked_requests_adapter`.
    """
    loop = asyncio.get_running_loop()

    def _run() -> T:
        _loop_var.set(loop)
        _check_in_worker()
        return func(*args, **kwargs)

    return await asyncio.to_thread(_run)
