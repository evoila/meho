# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CI coverage path keeps every shard's data (#4758) and runs once a night (#4910).

Two things lose a whole shard's coverage while every check stays green:

* The shard count is written in several places of ``ci.yml`` (matrix,
  job name, ``TOTAL`` in the pytest step, one download step per shard in
  the combine job, and the test map step that needs every shard). A
  shard that the combine job does not download is simply missing from
  ``coverage.xml``.
* The xdist worker writes its coverage data file at interpreter exit, and
  the controller kills a worker 10 seconds after it reports "finished".
  The session-finish hook in ``tests/conftest.py`` saves the data before
  that report.

These tests pin both.

The coverage shards run once a night on the ``schedule`` trigger, not after
every push to ``main`` (#4910): on push they filled the whole heavy runner
pool and starved the merge queue's unit shards. ``quality-gate.yml`` must
analyze only that nightly run, because no other run uploads the Python
coverage file. The last tests pin that trigger shape on both workflows.

Each shard runs three test workers (#7103): serial shards hit their step
limit every night, so no test map was made. The alarm workflow
``unit-test-map-check.yml`` is red after a nightly run without a map; its
tests run the real step script with a fake ``gh``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from tests import conftest

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_CI_YML = _WORKFLOWS / "ci.yml"
_QUALITY_GATE_YML = _WORKFLOWS / "quality-gate.yml"
_MAP_CHECK_YML = _WORKFLOWS / "unit-test-map-check.yml"
_COVERAGERC = Path(__file__).resolve().parents[1] / ".coveragerc.ci"

_COVERAGE_JOBS = ["python-coverage", "python-coverage-combine"]
_VALIDATE_LABEL = "contains(github.event.pull_request.labels.*.name, 'ci-coverage-validate')"
_FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"


def _load(path: Path) -> dict[Any, Any]:
    with path.open() as fh:
        return cast("dict[Any, Any]", yaml.safe_load(fh))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare ``on:`` key as the boolean True.
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


@pytest.fixture(scope="module")
def ci_workflow() -> dict[Any, Any]:
    return _load(_CI_YML)


@pytest.fixture(scope="module")
def ci_jobs(ci_workflow: dict[Any, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", ci_workflow["jobs"])


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}, found {len(matches)}"
    return cast("dict[str, Any]", matches[0])


def test_coverage_shard_count_is_the_same_everywhere(ci_jobs: dict[str, Any]) -> None:
    shard_job = ci_jobs["python-coverage"]
    shards = shard_job["strategy"]["matrix"]["shard"]
    total = len(shards)
    assert shards == list(range(1, total + 1))
    assert shard_job["name"].endswith(f"/{total})")

    pytest_step = _step(shard_job, "Pytest (unit) with offline coverage data")
    assert f"\nTOTAL={total}\n" in pytest_step["run"]
    assert "NR % t == s - 1" in pytest_step["run"]

    print_step = _step(shard_job, "Print peak memory")
    assert f"coverage shard ${{{{ matrix.shard }}}}/{total})" in print_step["run"]

    upload_step = _step(shard_job, "Upload shard coverage data (parallel files)")
    assert upload_step["with"]["name"] == "python-coverage-data-shard-${{ matrix.shard }}"

    combine_job = ci_jobs["python-coverage-combine"]
    downloaded = [
        s["with"]["name"]
        for s in combine_job["steps"]
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert downloaded == [f"python-coverage-data-shard-{i}" for i in range(1, total + 1)]

    # The test map (#5642) is made only when every shard delivered its data.
    map_step = _step(combine_job, "Make the unit test map")
    for i in range(1, total + 1):
        assert f"steps.shard-{i}.outcome == 'success'" in map_step["if"]
    assert f"steps.shard-{total + 1}." not in map_step["if"]
    assert f'"${{#junit[@]}}" -ne {total} ]' in map_step["run"]


@pytest.mark.parametrize("job_id", ["python-coverage", "python-coverage-combine"])
def test_coverage_job_cap_is_above_its_step_caps(ci_jobs: dict[str, Any], job_id: str) -> None:
    """A job-level timeout cancels the run; step timeouts only fail the step.

    So every bounded step must be able to time out before the job does.
    """
    job = ci_jobs[job_id]
    step_caps = sum(int(s.get("timeout-minutes", 0)) for s in job["steps"])
    assert step_caps > 0
    assert int(job["timeout-minutes"]) > step_caps


class _FakeCoverage:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")

    def save(self) -> None:
        self.calls.append("save")


def _use_fake_coverage(monkeypatch: pytest.MonkeyPatch, fake: _FakeCoverage | None) -> None:
    import coverage

    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: fake))


def test_worker_saves_its_coverage_data_before_reporting_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("COVERAGE_PROCESS_START", ".coveragerc.ci")
    fake = _FakeCoverage()
    _use_fake_coverage(monkeypatch, fake)

    conftest._save_worker_coverage_data()

    assert fake.calls == ["stop", "save"]


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"COVERAGE_PROCESS_START": ".coveragerc.ci"}, "controller process, not a worker"),
        ({"PYTEST_XDIST_WORKER": "gw0"}, "no process-startup coverage requested"),
    ],
)
def test_worker_save_is_inert_outside_the_ci_coverage_setup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], reason: str
) -> None:
    for var in ("PYTEST_XDIST_WORKER", "COVERAGE_PROCESS_START"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    fake = _FakeCoverage()
    _use_fake_coverage(monkeypatch, fake)

    conftest._save_worker_coverage_data()

    assert fake.calls == [], reason


def test_worker_save_tolerates_no_started_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("COVERAGE_PROCESS_START", ".coveragerc.ci")
    _use_fake_coverage(monkeypatch, None)

    conftest._save_worker_coverage_data()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"PYTEST_XDIST_WORKER": "gw2", "COVERAGE_PROCESS_START": ".coveragerc.ci"},
            [("xdist_worker", "gw2")],
        ),
        ({"COVERAGE_PROCESS_START": ".coveragerc.ci"}, []),
        ({"PYTEST_XDIST_WORKER": "gw2"}, []),
    ],
    ids=["coverage-worker", "controller", "unit-shard-worker"],
)
def test_coverage_worker_names_itself_in_the_junit_file(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: list[tuple[str, str]]
) -> None:
    """The test map builder checks the coverage data of each worker by this name."""
    for var in ("PYTEST_XDIST_WORKER", "COVERAGE_PROCESS_START"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    item = SimpleNamespace(user_properties=[])

    conftest.pytest_itemcollected(cast("pytest.Item", item))

    assert item.user_properties == expected


def test_ci_runs_once_a_night_on_a_schedule(ci_workflow: dict[Any, Any]) -> None:
    schedule = _triggers(ci_workflow)["schedule"]
    assert len(schedule) == 1, "one nightly cron line"
    minute, hour, day, month, weekday = schedule[0]["cron"].split()
    assert (day, month, weekday) == ("*", "*", "*"), "daily, not weekly or monthly"
    assert hour.isdigit(), "one fixed hour"
    assert minute.isdigit() and minute != "0", "GitHub delays runs at the full hour"


@pytest.mark.parametrize("job_id", _COVERAGE_JOBS)
def test_coverage_jobs_run_on_the_schedule_and_the_label_only(
    ci_jobs: dict[str, Any], job_id: str
) -> None:
    condition = ci_jobs[job_id]["if"]
    assert "github.event_name == 'schedule'" in condition
    assert "github.event_name == 'pull_request'" in condition
    assert _VALIDATE_LABEL in condition
    assert _FORK_GUARD in condition, "a fork PR must not reach the internal pool"
    assert "'push'" not in condition, "coverage no longer runs after every merge"
    assert "merge_group" not in condition, "the merge queue never runs coverage"


def test_quality_gate_analyzes_only_green_nightly_runs() -> None:
    job = _load(_QUALITY_GATE_YML)["jobs"]["sonarcloud"]
    condition = job["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.event == 'schedule'" in condition
    assert "'push'" not in condition, "a push run has no python-coverage artifact"


def test_coverage_shards_run_three_workers_like_the_unit_shards(ci_jobs: dict[str, Any]) -> None:
    run = _step(ci_jobs["python-coverage"], "Pytest (unit) with offline coverage data")["run"]
    assert "uv run pytest -n 3 --dist loadscope -p no:cov" in run
    assert "--max-worker-restart=0" in run, "a crashed worker must not be started again"
    unit_run = _step(ci_jobs["python-unit-shard"], "Pytest (unit shard ${{ matrix.shard }}/6)")[
        "run"
    ]
    assert "pytest -n 3 " in unit_run


def test_coverage_leaves_out_files_in_pytest_temp_folders() -> None:
    """A test's temp package is gone at report time; it must not stop coverage.xml."""
    text = _COVERAGERC.read_text(encoding="utf-8")
    assert "omit =\n    */pytest-of-*/*\n" in text


# --- the alarm: unit-test-map-check.yml ---------------------------------------


def test_map_check_runs_after_every_nightly_ci_run() -> None:
    workflow = _load(_MAP_CHECK_YML)
    assert _triggers(workflow) == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}}
    assert workflow["name"] != "CI"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["check"]
    assert job["if"] == "github.event.workflow_run.event == 'schedule'"
    assert job["permissions"] == {"actions": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    assert "continue-on-error" not in job, "the alarm must be able to turn red"
    assert all("continue-on-error" not in step for step in job["steps"])
    assert all("uses" not in step for step in job["steps"]), "no checkout, no other action"


def _run_map_check(
    tmp_path: Path, artifacts: list[dict[str, Any]] | None, jobs: list[dict[str, Any]]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real step of the alarm with a fake ``gh`` (None: the API call fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    if artifacts is not None:
        (tmp_path / "artifacts.json").write_text(
            json.dumps({"artifacts": artifacts}), encoding="utf-8"
        )
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "${2#repos/example/repo/actions/runs/77/}" in\n'
        f'  artifacts?name=unit-test-map) f="{tmp_path}/artifacts.json" ;;\n'
        f'  jobs?filter=latest*) f="{tmp_path}/jobs.json" ;;\n'
        '  *) echo "unexpected: $2" >&2; exit 2 ;;\n'
        "esac\n"
        '[ -f "$f" ] || exit 1\n'
        '[ "$3" = "--jq" ] && exec jq -r "$4" "$f"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    step = _load(_MAP_CHECK_YML)["jobs"]["check"]["steps"][0]
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    summary = tmp_path / "summary"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_STEP_SUMMARY": str(summary),
        "RUN_ID": "77",
        "RUN_URL": "https://github.com/example/repo/actions/runs/77",
    }
    run = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return run, summary.read_text(encoding="utf-8") if summary.exists() else ""


_COVERAGE_JOBS_OF_A_RUN = [
    {"name": "Python coverage (SonarCloud, shard 1/8)", "conclusion": "success"},
    {"name": "Python coverage (SonarCloud, shard 2/8)", "conclusion": "failure"},
    {"name": "Python coverage combine (SonarCloud artifact)", "conclusion": "failure"},
    {"name": "Python unit shard 1/6", "conclusion": "failure"},
]

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


@needs_jq
def test_map_check_is_green_when_the_nightly_run_made_a_map(tmp_path: Path) -> None:
    run, summary = _run_map_check(
        tmp_path, [{"name": "unit-test-map", "expired": False}], _COVERAGE_JOBS_OF_A_RUN
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert "::error" not in run.stdout
    assert summary.startswith("The nightly run made the unit test map: https://")


@needs_jq
@pytest.mark.parametrize(
    "artifacts", [[], [{"name": "unit-test-map", "expired": True}]], ids=["none", "expired"]
)
def test_map_check_is_red_and_names_the_failed_coverage_jobs_without_a_map(
    tmp_path: Path, artifacts: list[dict[str, Any]]
) -> None:
    run, summary = _run_map_check(tmp_path, artifacts, _COVERAGE_JOBS_OF_A_RUN)

    assert run.returncode == 1
    assert (
        "::error title=No unit test map::The nightly run made no unit test map."
        " Nightly run: https://github.com/example/repo/actions/runs/77"
    ) in run.stdout
    assert "six unit shards" not in run.stdout + summary, (
        "an older map may still be in use: the alarm says only what it found in this run"
    )
    assert "## No unit test map" in summary
    assert "- Python coverage (SonarCloud, shard 2/8): failure" in summary
    assert "- Python coverage combine (SonarCloud artifact): failure" in summary
    assert "shard 1/8" not in summary, "a passed job is not listed"
    assert "Python unit shard" not in summary, "only the coverage jobs make the map"


@needs_jq
def test_map_check_with_every_coverage_job_passed_points_to_the_map_step(tmp_path: Path) -> None:
    passed = [dict(job, conclusion="success") for job in _COVERAGE_JOBS_OF_A_RUN]

    run, summary = _run_map_check(tmp_path, [], passed)

    assert run.returncode == 1
    assert 'read the step "Make the unit test map" of the coverage combine job' in summary


@needs_jq
def test_map_check_is_red_when_it_cannot_read_the_artifacts(tmp_path: Path) -> None:
    run, summary = _run_map_check(tmp_path, None, _COVERAGE_JOBS_OF_A_RUN)

    assert run.returncode == 1
    assert (
        "::error title=No unit test map::Could not read the artifacts of the nightly run."
        " Nightly run: https://github.com/example/repo/actions/runs/77"
    ) in run.stdout
    assert "six unit shards" not in run.stdout + summary
    assert "- Python coverage (SonarCloud, shard 2/8): failure" in summary
