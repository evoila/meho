# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured testcontainers wait strategies for the test fixtures.

``testcontainers`` 4.x deprecated the string/callable form of
``wait_for_logs(container, "msg", timeout=N)`` in favour of structured
wait strategies. This module provides the one shape every fixture
needs — "block until a log line appears, with a startup timeout" — so
the migration is a single import instead of repeating the strategy
construction (and the ``re.MULTILINE`` compile semantics) in each
fixture.

Usage::

    from tests._strategies import wait_for_log_message

    container.start()
    wait_for_log_message(container, "Server started!", timeout=60)

The call blocks until the message (a plain string compiled with
``re.MULTILINE`` or a pre-compiled ``re.Pattern``) is found in the
container's stdout/stderr, raising ``TimeoutError`` once the timeout
elapses or ``RuntimeError`` if the container exits first — the same
failure modes the deprecated ``wait_for_logs`` exposed, so existing
``try/except`` fall-backs in the fixtures keep working unchanged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from testcontainers.core.wait_strategies import LogMessageWaitStrategy

if TYPE_CHECKING:
    from testcontainers.core.waiting_utils import WaitStrategyTarget

__all__ = ["wait_for_log_message"]


def wait_for_log_message(
    container: WaitStrategyTarget,
    message: str | re.Pattern[str],
    *,
    timeout: float,
) -> None:
    """Block until ``message`` appears in ``container``'s logs.

    Structured replacement for the deprecated
    ``wait_for_logs(container, message, timeout=...)``. ``message`` is a
    plain string (compiled internally with ``re.MULTILINE``) or a
    pre-compiled ``re.Pattern``; ``timeout`` is the startup budget in
    seconds.

    Raises:
        TimeoutError: the message did not appear within ``timeout``.
        RuntimeError: the container exited before the message appeared.
    """
    strategy = LogMessageWaitStrategy(message).with_startup_timeout(timeout)
    strategy.wait_until_ready(container)
