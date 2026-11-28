# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Time limits in tests that a busy CI runner must not fail (#6270, #7220).

There are two tools here.

A test that compares real elapsed time with a fixed limit carries the
``slow_runner_tolerant`` marker and checks the limit with
:func:`check_time_limit`. The required CI jobs set
``MEHO_SKIP_TIME_LIMITS=1``: the test still runs and its other checks
still fail the job, but the time limit does not. The non-required
"Python timing tests" CI job runs the marked tests without it, so a
slowdown shows there without blocking a merge.

:func:`wallclock_limit` is for a test that checks its time limit in every
job. It gives the limit times the factor in ``MEHO_TIME_LIMIT_FACTOR``
(1 when the variable is not set). The pull request run and the merge queue
run set the factor to 3, so a busy runner does not turn a required check
red. The nightly run sets it to 1 and so checks the real limits. A test
that waits for a condition with a timeout takes the timeout from it too.
``test_time_limits.py`` fails on a new raw comparison of a measured time
with a number.
"""

from __future__ import annotations

import os


def check_time_limit(value: float, limit: float, message: str) -> None:
    """Fail when ``value`` is not below ``limit``, unless time limits are off."""
    if os.environ.get("MEHO_SKIP_TIME_LIMITS") == "1":
        return
    assert value < limit, message


def wallclock_limit(seconds: float) -> float:
    """Return ``seconds`` times the factor in ``MEHO_TIME_LIMIT_FACTOR`` (default 1)."""
    raw = os.environ.get("MEHO_TIME_LIMIT_FACTOR", "").strip()
    factor = float(raw) if raw else 1.0
    if factor < 1:
        raise ValueError(f"MEHO_TIME_LIMIT_FACTOR must be 1 or more, got {raw!r}")
    return seconds * factor
