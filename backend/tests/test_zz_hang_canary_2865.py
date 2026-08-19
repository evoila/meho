import time

import pytest


@pytest.mark.timeout(15)
def test_deliberate_coverage_hang_2865():
    # #2865 CI VALIDATION ONLY — reverted before merge-ready. Proves a hung
    # coverage test dies as a pytest-timeout FAILURE (=> step failure, run
    # conclusion `success` via the job's continue-on-error), never a job
    # cancellation. The per-test mark trips at 15 s under the job's
    # --timeout=300 so the validation run stays fast.
    time.sleep(120)
