# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The time-limit helpers for tests, and how CI sets them (#6270, #7220)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests._time_limits import check_time_limit, wallclock_limit

_TESTS_DIR = Path(__file__).resolve().parent
_WORKFLOW = _TESTS_DIR.parents[1] / ".github" / "workflows" / "ci.yml"


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


def test_the_factor_is_one_when_the_variable_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEHO_TIME_LIMIT_FACTOR", raising=False)
    assert wallclock_limit(2.0) == 2.0
    monkeypatch.setenv("MEHO_TIME_LIMIT_FACTOR", "")
    assert wallclock_limit(2.0) == 2.0


def test_a_factor_below_one_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_TIME_LIMIT_FACTOR", "0.5")
    with pytest.raises(ValueError, match="MEHO_TIME_LIMIT_FACTOR"):
        wallclock_limit(2.0)


@pytest.mark.parametrize(("factor", "passes"), [("3", True), ("1", False)])
def test_a_run_of_two_and_a_half_times_the_limit_passes_only_with_factor_three(
    monkeypatch: pytest.MonkeyPatch, factor: str, passes: bool
) -> None:
    monkeypatch.setenv("MEHO_TIME_LIMIT_FACTOR", factor)
    limit = 2.0
    measured = 2.5 * limit  # a stand-in for a run on a busy runner
    assert (measured < wallclock_limit(limit)) is passes


# The forms of "a measured time compared with a number" the guard finds, on
# one line that starts with ``assert``: on the left ``elapsed...``, ``took``,
# ``...duration...``, a name ending in ``_ms``, ``monotonic()``,
# ``perf_counter()`` or ``loop.time()``; then ``<`` or ``<=``; then a number,
# an UPPER_CASE name or ``module.UPPER_CASE``. A line that calls
# ``wallclock_limit(`` is fine. A wait with a timeout is not searched.
_MEASURED = (
    r"(?:\belapsed\w*|\btook\b|\w*duration\w*|\b[a-z]\w*_ms(?=\s*<)"
    r"|\bmonotonic\(\)|\bperf_counter\(\)|\bloop\.time\(\))"
)
_LIMIT = r"(?:\d[\d_.]*|[A-Z_][A-Z0-9_]*\b|\w+\.[A-Z_][A-Z0-9_]*\b)"
_RAW_TIME_COMPARE = re.compile(rf"^\s*assert\b[^#]*{_MEASURED}[^#,]*?<=?\s*{_LIMIT}")

# Files that still compare a measured time with a number without the helper,
# and how many such lines each has. The numbers may only go down: a new raw
# comparison fails, and a file that drops one must lower its number here.
_RAW_TIME_COMPARES_LEFT: dict[str, int] = {
    "integration/test_topology_g91_acceptance.py": 4,
    "integration/test_topology_query.py": 3,
    "test_api_v1_connectors_ingest.py": 1,
    "test_api_v1_gateway.py": 1,
    "test_api_v1_operation_runs.py": 1,
    "test_apply_with_secret_values_waits.py": 1,
    "test_approval_script_pages.py": 1,
    "test_blocking_work_limits.py": 1,
    "test_ci_pull_test_images.py": 1,
    "test_connectors_pfsense_config_redaction_rules.py": 2,
    "test_container_ready.py": 2,
    "test_credential_capable_calls_wait.py": 2,
    "test_feed_stream_ends_with_login.py": 5,
    "test_first_start.py": 1,
    "test_mcp_call_operation_async.py": 1,
    "test_mcp_tool_broadcast_watch.py": 1,
    "test_memory_snippet.py": 1,
    "test_operation_run_service.py": 1,
    "test_operations_ingest_jobs.py": 3,
    "test_operations_tls_presented.py": 1,
    "test_redaction_patterns.py": 3,
    "test_secret_text_large_inputs.py": 5,
    "test_topology_history_integration.py": 1,
    "test_ui_readiness_pill.py": 3,
}


def _is_raw_time_compare(line: str) -> bool:
    return bool(_RAW_TIME_COMPARE.search(line)) and "wallclock_limit(" not in line


@pytest.mark.parametrize(
    "line",
    [
        "    assert elapsed < _TIME_LIMIT_SECONDS",
        '    assert elapsed < 5.0, f"render should not block, took {elapsed:.2f}s"',
        "    assert time.monotonic() - started < 2.0",
        "    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS",
        "    assert time.monotonic() - started < read_to_end.STOP_WAIT_SECONDS",
        "    assert deep_ms < _PERF_ABS_CEILING_MS, (",
        "    assert loop.time() - started < 5",
        "    assert 0.9 <= took <= 5.0",
    ],
)
def test_the_guard_finds_a_raw_time_compare(line: str) -> None:
    assert _is_raw_time_compare(line)


@pytest.mark.parametrize(
    "line",
    [
        "    assert elapsed < wallclock_limit(_TIME_LIMIT_SECONDS)",
        "    assert time.monotonic() - started < wallclock_limit(read_to_end.STOP_WAIT_SECONDS)",
        '    check_time_limit(elapsed, 2.0, "too slow")',
        '    assert 0.0 <= body["round_trip_ms"] < 1_000',
        '    assert claims["exp"] - time.time() <= 24 * 60 * 60',
        "    assert ratio < _PERF_RATIO_CEILING, (",
    ],
)
def test_the_guard_leaves_other_lines_alone(line: str) -> None:
    assert not _is_raw_time_compare(line)


def test_no_new_raw_time_compare_in_a_test_file() -> None:
    # A time limit compared without wallclock_limit fails a required check
    # on a busy runner. Use wallclock_limit, or check_time_limit with the
    # slow_runner_tolerant marker.
    found: Counter[str] = Counter()
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for line in path.read_text().splitlines():
            if _is_raw_time_compare(line):
                found[str(path.relative_to(_TESTS_DIR))] += 1
    assert dict(found) == _RAW_TIME_COMPARES_LEFT


def _pytest_env(job_name: str) -> dict[str, Any]:
    doc = yaml.safe_load(_WORKFLOW.read_text())
    steps = cast(list[dict[str, Any]], doc["jobs"][job_name]["steps"])
    pytest_steps = [s for s in steps if str(s.get("name", "")).startswith("Pytest (")]
    assert len(pytest_steps) == 1, f"{job_name} must have exactly one Pytest step"
    return cast(dict[str, Any], pytest_steps[0].get("env", {}))


_FACTOR_FOR_EVENT = "${{ github.event_name == 'schedule' && '1' || '3' }}"


@pytest.mark.parametrize(
    "job_name",
    [
        "python-unit-shard",
        "python-unit-shard-second-site",
        "python-unit-shard-hosted-trial",
        "python-integration-shard",
    ],
)
def test_required_test_jobs_scale_the_limits_except_at_night(job_name: str) -> None:
    assert _pytest_env(job_name).get("MEHO_TIME_LIMIT_FACTOR") == _FACTOR_FOR_EVENT


def test_the_workflow_runs_only_on_pull_request_merge_queue_and_night() -> None:
    # The factor expression above gives 3 to every event that is not the
    # nightly schedule; a new trigger must decide its factor first.
    doc = yaml.safe_load(_WORKFLOW.read_text())
    triggers = doc.get(True) or doc["on"]
    assert sorted(triggers) == ["merge_group", "pull_request", "schedule"]
