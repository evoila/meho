# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the skip limit check of the CI shards (tests/_skip_limit_check.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests import _skip_limit_check as checker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_FILE_A = "tests/test_skip_limit_check.py"
_FILE_B = "tests/_skip_limit_check.py"


def _junit(tmp_path: Path, cases: list[tuple[str, str]]) -> Path:
    """Write a JUnit file; each case is (file, outcome) with outcome pass/skip/xfail."""
    rows = []
    for index, (name, outcome) in enumerate(cases):
        inner = ""
        if outcome == "skip":
            inner = '<skipped type="pytest.skip" message="no tool">no tool</skipped>'
        elif outcome == "xfail":
            inner = '<skipped type="pytest.xfail" message="known">known</skipped>'
        rows.append(
            f'<testcase classname="c" name="t{index}" file="{name}" time="0.1">{inner}</testcase>'
        )
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(rows)
        + "</testsuite></testsuites>"
    )
    return path


def _limits(
    tmp_path: Path, unit: dict[str, Any], integration: dict[str, Any] | None = None
) -> Path:
    path = tmp_path / "limits.json"
    path.write_text(json.dumps({"lanes": {"unit": unit, "integration": integration or {}}}))
    return path


def _run(junit: Path, limits: Path, lane: str = "unit") -> int:
    return checker._main(["--lane", lane, "--limits", str(limits), str(junit)])


def test_count_above_the_number_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "skip"), (_FILE_A, "skip"), (_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 1})) == 1
    out = capfd.readouterr().out
    assert (
        f"::error title=Too many skipped tests::{_FILE_A} skipped 2 tests; the unit lane allows 1"
        in out
    )


def test_count_equal_to_the_number_passes(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "skip"), (_FILE_A, "skip")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 2})) == 0
    out = capfd.readouterr().out
    assert "::error" not in out
    assert "::notice" not in out


def test_count_below_the_number_passes_with_a_notice(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "skip"), (_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 3})) == 0
    out = capfd.readouterr().out
    assert f"lower the number for {_FILE_A} in this pull request" in out
    assert "::error" not in out


def test_listed_file_that_ran_with_no_skip_gives_a_notice(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass"), (_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 2})) == 0
    out = capfd.readouterr().out
    assert f"{_FILE_A} skipped 0 tests, the number is 2" in out
    assert f"lower the number for {_FILE_A} in this pull request" in out


def test_file_with_a_number_that_did_not_run_gives_no_notice(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Another shard runs that file; this shard has nothing to say about it."""
    junit = _junit(tmp_path, [(_FILE_B, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 3})) == 0
    assert "::notice" not in capfd.readouterr().out


def test_file_not_in_the_count_file_with_one_skip_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path, [(_FILE_B, "skip"), (_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: 1})) == 1
    assert f"{_FILE_B} skipped 1 tests; the unit lane allows 0" in capfd.readouterr().out


def test_xfail_counts_as_a_skip(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "xfail")])
    assert checker.count_skips(junit) == {_FILE_A: 1}
    assert _run(junit, _limits(tmp_path, {})) == 1


def test_the_lanes_have_their_own_numbers(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "skip")])
    limits = _limits(tmp_path, {}, {_FILE_A: 1})
    assert _run(junit, limits, lane="integration") == 0
    assert _run(junit, limits, lane="unit") == 1


def test_missing_junit_file_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path / "missing.xml", _limits(tmp_path, {})) == 1
    assert "cannot read the JUnit file" in capfd.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<testsuites><testsuite></testcase></testsuites>",
        "<testsuites><testsuite name='pytest'></testsuite></testsuites>",
    ],
    ids=["empty", "broken", "no-test-case"],
)
def test_empty_or_broken_junit_file_fails(tmp_path: Path, text: str) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(text)
    assert _run(junit, _limits(tmp_path, {})) == 1


def test_junit_case_without_a_file_name_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuites><testsuite><testcase classname='c' name='t'/></testsuite></testsuites>"
    )
    assert _run(junit, _limits(tmp_path, {})) == 1
    assert "junit_family=xunit1" in capfd.readouterr().out


def test_missing_count_file_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    assert _run(junit, tmp_path / "missing.json") == 1
    assert "cannot read the count file" in capfd.readouterr().out


def test_broken_count_file_fails(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    limits = tmp_path / "limits.json"
    limits.write_text("{not json")
    assert _run(junit, limits) == 1


def test_row_for_a_test_file_that_does_not_exist_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    limits = _limits(tmp_path, {_FILE_A: 1}, {"tests/integration/test_gone.py": 2})
    assert _run(junit, limits) == 1
    assert (
        "tests/integration/test_gone.py is not in the repo; remove this row"
        in capfd.readouterr().out
    )


@pytest.mark.parametrize("number", [-1, 1.5, "2", True])
def test_number_that_is_not_a_count_fails(tmp_path: Path, number: object) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {_FILE_A: number})) == 1


def test_unknown_lane_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    assert _run(junit, _limits(tmp_path, {}), lane="smoke") == 1
    assert "unknown lane 'smoke'" in capfd.readouterr().out


def test_unknown_lane_in_the_count_file_fails(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [(_FILE_A, "pass")])
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"lanes": {"unit": {}, "nightly": {}}}))
    assert _run(junit, limits) == 1


def test_committed_count_file_loads_for_both_lanes() -> None:
    """Every row of the real count file names a test file in the repo."""
    for lane in checker.LANES:
        limits = checker.load_limits(checker.LIMITS_FILE, lane)
        assert limits, f"the {lane} lane of tests/_skip_limits.json is empty"


def _step_runs(job_name: str) -> list[str]:
    doc = yaml.safe_load(_WORKFLOW.read_text())
    job = cast(dict[str, Any], doc["jobs"])[job_name]
    return [str(step.get("run", "")) for step in job["steps"]]


@pytest.mark.parametrize(
    ("job_name", "lane", "junit_name"),
    [
        ("python-unit-shard", "unit", "pytest-unit-shard.xml"),
        ("python-integration-shard", "integration", "pytest-integration-shard.xml"),
    ],
)
def test_the_shards_run_the_check_after_pytest(job_name: str, lane: str, junit_name: str) -> None:
    runs = _step_runs(job_name)
    pytest_index = next(i for i, run in enumerate(runs) if f"--junitxml={junit_name}" in run)
    check_index = next(i for i, run in enumerate(runs) if "tests/_skip_limit_check.py" in run)
    assert check_index > pytest_index
    assert f"--lane {lane} {junit_name}" in runs[check_index]
