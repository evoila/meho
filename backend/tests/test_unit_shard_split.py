# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the CI shard split of the unit and integration tests (tests/_unit_shard_split.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import _unit_shard_split as splitter

TESTS_DIR = Path(__file__).parent
CI_WORKFLOW = TESTS_DIR.parent.parent / ".github" / "workflows" / "ci.yml"
INTEGRATION_DURATIONS = TESTS_DIR / "_integration_shard_durations.json"
CANARY_FILE = "tests/acceptance/test_g07_vsphere_canary.py"


def _ci_unit_files() -> list[str]:
    """The file list the CI step pipes into the splitter."""
    return sorted(
        str(path.relative_to(TESTS_DIR.parent))
        for path in TESTS_DIR.rglob("test_*.py")
        if "integration" not in path.relative_to(TESTS_DIR).parts[:1]
        and "migrations" not in path.relative_to(TESTS_DIR).parts[:1]
    )


def _ci_integration_files() -> list[str]:
    """The file list the python-integration-shard step pipes into the splitter."""
    files = {
        str(path.relative_to(TESTS_DIR.parent))
        for path in (TESTS_DIR / "integration").rglob("test_*.py")
    }
    return sorted(files | {CANARY_FILE})


UNIT_SHARDS = 5


def _integration_shard_job() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    job: dict[str, Any] = workflow["jobs"]["python-integration-shard"]
    return job


def test_every_ci_unit_file_lands_in_exactly_one_shard() -> None:
    files = _ci_unit_files()
    durations = splitter.load_durations()
    shards = splitter.split(files, durations, UNIT_SHARDS)

    assert len(shards) == UNIT_SHARDS
    assert sorted(name for shard in shards for name in shard) == files
    assert all(shard for shard in shards)


def test_ci_unit_shards_match_the_split_in_the_workflow() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    job = workflow["jobs"]["python-unit-shard"]
    shards = list(range(1, UNIT_SHARDS + 1))
    run = next(
        step["run"] for step in job["steps"] if "_unit_shard_split.py" in step.get("run", "")
    )

    assert f"'{shards}'" in job["strategy"]["matrix"]["shard"]
    assert f"TOTAL={UNIT_SHARDS}\n" in run
    assert f"format('Python unit shard {{0}}/{UNIT_SHARDS}', matrix.shard)" in job["name"]


def test_split_is_deterministic_and_ignores_input_order() -> None:
    files = _ci_unit_files()
    durations = splitter.load_durations()

    first = splitter.split(files, durations, UNIT_SHARDS)
    second = splitter.split(list(reversed(files)), durations, UNIT_SHARDS)

    assert first == second


def test_slow_files_go_to_different_shards() -> None:
    durations = {
        "tests/test_a.py": 100.0,
        "tests/test_b.py": 90.0,
        "tests/test_c.py": 5.0,
        "tests/test_d.py": 1.0,
    }
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"]

    shards = splitter.split(files, durations, 2)

    assert shards[0] == ["tests/test_a.py"]
    assert shards[1] == ["tests/test_b.py", "tests/test_c.py", "tests/test_d.py"]


def test_file_without_measurement_counts_as_the_median() -> None:
    durations = {"tests/test_a.py": 10.0, "tests/test_b.py": 20.0, "tests/test_c.py": 30.0}
    shards = [["tests/test_new.py"], ["tests/test_b.py"]]

    assert splitter.predicted_seconds(shards, durations) == [20.1, 20.1]


def test_files_measured_at_zero_spread_over_all_shards() -> None:
    files = [f"tests/test_skipped_{i}.py" for i in range(8)] + ["tests/test_a.py"]
    durations = dict.fromkeys(files, 0.0)
    durations["tests/test_a.py"] = 5.0

    shards = splitter.split(files, durations, 4)

    assert [len(shard) for shard in shards] == [1, 3, 3, 2]


def test_split_rejects_zero_shards() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        splitter.split(["tests/test_a.py"], {}, 0)


def test_committed_durations_cover_most_ci_unit_files() -> None:
    files = _ci_unit_files()
    durations = splitter.load_durations()

    unmeasured = [name for name in files if name not in durations]

    assert len(unmeasured) < len(files) // 4, (
        "most unit test files have no measurement; refresh "
        "tests/_unit_shard_durations.json (see tests/_unit_shard_split.py)"
    )


def test_measure_sums_seconds_per_file(tmp_path: Path) -> None:
    junit = tmp_path / "unit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" file="tests/test_a.py" name="t1" time="1.5"/>'
        '<testcase classname="tests.test_a" file="tests/test_a.py" name="t2" time="2.0"/>'
        '<testcase classname="tests.test_b" file="tests/test_b.py" name="t1" time="0.25"/>'
        "</testsuite></testsuites>"
    )

    second = tmp_path / "unit-2.xml"
    second.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_b" file="tests/test_b.py" name="t2" time="0.75"/>'
        "</testsuite></testsuites>"
    )

    assert splitter.measure([junit, second]) == {"tests/test_a.py": 3.5, "tests/test_b.py": 1.0}


def test_measure_requires_xunit1_file_attribute(tmp_path: Path) -> None:
    junit = tmp_path / "unit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" name="t1" time="1.5"/>'
        "</testsuite></testsuites>"
    )

    with pytest.raises(ValueError, match="xunit1"):
        splitter.measure([junit])


def test_cli_prints_one_shard_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import io

    durations = {"tests/test_a.py": 100.0, "tests/test_b.py": 90.0}
    monkeypatch.setattr(splitter, "load_durations", lambda _path: durations)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("tests/test_a.py\ntests/test_b.py\ntests/test_c.py\n")
    )

    assert splitter._main(["--shard", "2", "--total", "2"]) == 0

    out, err = capfd.readouterr()
    assert out.splitlines() == ["tests/test_b.py", "tests/test_c.py"]
    assert "shard 2/2: 2 test files" in err
    assert "1 files without a measurement" in err


def test_every_ci_integration_file_lands_in_exactly_one_shard() -> None:
    files = _ci_integration_files()
    durations = splitter.load_durations(INTEGRATION_DURATIONS)
    total = len(_integration_shard_job()["strategy"]["matrix"]["shard"])

    shards = splitter.split(files, durations, total)

    assert total >= 3
    assert sorted(name for shard in shards for name in shard) == files
    assert all(shard for shard in shards)


def test_ci_integration_shards_match_the_split_in_the_workflow() -> None:
    job = _integration_shard_job()
    shards = job["strategy"]["matrix"]["shard"]
    run = next(
        step["run"] for step in job["steps"] if "_unit_shard_split.py" in step.get("run", "")
    )

    assert shards == list(range(1, len(shards) + 1))
    assert f"TOTAL={len(shards)}\n" in run
    assert "find tests/integration -name 'test_*.py'" in run
    assert f"echo {CANARY_FILE}" in run
    assert "--durations tests/_integration_shard_durations.json" in run


def test_committed_durations_cover_most_ci_integration_files() -> None:
    files = _ci_integration_files()
    durations = splitter.load_durations(INTEGRATION_DURATIONS)

    unmeasured = [name for name in files if name not in durations]

    assert len(unmeasured) < len(files) // 4, (
        "most integration test files have no measurement; refresh "
        "tests/_integration_shard_durations.json (see tests/_unit_shard_split.py)"
    )


def test_cli_reads_the_given_durations_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import io

    durations_file = tmp_path / "durations.json"
    durations_file.write_text('{"seconds": {"tests/test_a.py": 100.0, "tests/test_b.py": 1.0}}')
    monkeypatch.setattr("sys.stdin", io.StringIO("tests/test_a.py\ntests/test_b.py\n"))

    assert splitter._main(["--durations", str(durations_file), "--shard", "1", "--total", "2"]) == 0

    out, _ = capfd.readouterr()
    assert out.splitlines() == ["tests/test_a.py"]
