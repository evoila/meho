# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``meho_logs`` helper sees the log lines MEHO really writes.

A "not in the logs" check is only worth something when it reads MEHO's real
log output. If the log setup changes and the helper stops seeing it, these
tests fail.
"""

from __future__ import annotations

import logging
import uuid

import pytest
import structlog

from tests._production_log_lines import production_logging
from tests.conftest import MehoLogs


def _marker() -> str:
    return f"made-up-marker-{uuid.uuid4().hex}"


def test_helper_sees_a_structured_line_that_caplog_does_not(
    meho_logs: MehoLogs, caplog: pytest.LogCaptureFixture
) -> None:
    marker = _marker()
    with production_logging():
        structlog.get_logger("meho_backplane.guard").warning("guard_line", value=marker)

    assert marker not in caplog.text
    assert marker in meho_logs.text()


def test_helper_sees_a_standard_logging_line(meho_logs: MehoLogs) -> None:
    marker = _marker()
    with production_logging():
        logging.getLogger("meho_backplane.guard").warning("guard line %s", marker)

    assert marker in meho_logs.text()


def test_helper_keeps_earlier_output_after_a_read(meho_logs: MehoLogs) -> None:
    first, second = _marker(), _marker()
    with production_logging():
        log = structlog.get_logger("meho_backplane.guard")
        log.warning("guard_line", value=first)
        meho_logs.text()
        log.warning("guard_line", value=second)

    text = meho_logs.text()
    assert first in text
    assert second in text
