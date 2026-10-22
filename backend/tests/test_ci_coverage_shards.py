# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CI coverage path keeps every shard's data (#4758).

Two things lose a whole shard's coverage while every check stays green:

* The shard count is written in several places of ``ci.yml`` (matrix,
  job name, ``TOTAL`` in the pytest step, one download step per shard in
  the combine job). A shard that the combine job does not download is
  simply missing from ``coverage.xml``.
* The xdist worker writes its coverage data file at interpreter exit, and
  the controller kills a worker 10 seconds after it reports "finished".
  The session-finish hook in ``tests/conftest.py`` saves the data before
  that report.

These tests pin both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests import conftest

_CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci_jobs() -> dict[str, Any]:
    with _CI_YML.open() as fh:
        return cast("dict[str, Any]", yaml.safe_load(fh)["jobs"])


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
