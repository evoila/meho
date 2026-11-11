# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The time-limit helper for tests, and the marker the CI timing job picks (#6270)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._time_limits import check_time_limit

_TESTS_DIR = Path(__file__).resolve().parent


def test_a_time_over_the_limit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEHO_SKIP_TIME_LIMITS", raising=False)
    with pytest.raises(AssertionError, match="too slow"):
        check_time_limit(2.0, 1.0, "too slow")


def test_a_time_under_the_limit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEHO_SKIP_TIME_LIMITS", raising=False)
    check_time_limit(0.5, 1.0, "too slow")


def test_the_switch_turns_the_limit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SKIP_TIME_LIMITS", "1")
    check_time_limit(2.0, 1.0, "too slow")


def test_every_test_file_with_a_time_limit_is_marked() -> None:
    # The non-required CI timing job runs only files that carry the marker;
    # a time limit in an unmarked file would never be checked.
    users = [
        path
        for path in _TESTS_DIR.rglob("test_*.py")
        if path.name != Path(__file__).name and "check_time_limit(" in path.read_text()
    ]
    assert users, "no test uses check_time_limit"
    unmarked = [
        str(path.relative_to(_TESTS_DIR))
        for path in users
        if "mark.slow_runner_tolerant" not in path.read_text()
    ]
    assert unmarked == []
