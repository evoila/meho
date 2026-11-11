# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Time limits in tests that a busy CI runner must not fail (#6270).

A test that compares real elapsed time with a fixed limit carries the
``slow_runner_tolerant`` marker and checks the limit with
:func:`check_time_limit`. The required CI jobs set
``MEHO_SKIP_TIME_LIMITS=1``: the test still runs and its other checks
still fail the job, but the time limit does not. The non-required
"Python timing tests" CI job runs the marked tests without it, so a
slowdown shows there without blocking a merge.
"""

from __future__ import annotations

import os


def check_time_limit(value: float, limit: float, message: str) -> None:
    """Fail when ``value`` is not below ``limit``, unless time limits are off."""
    if os.environ.get("MEHO_SKIP_TIME_LIMITS") == "1":
        return
    assert value < limit, message
