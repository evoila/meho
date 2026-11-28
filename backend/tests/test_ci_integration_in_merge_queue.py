# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What CI runs on a pull request, in the merge queue and at night.

* On a pull request the four integration shards never start. The fan-in
  ``Python (integration testcontainers)`` still reports, passes and says in
  its summary that the integration tests run in the merge queue.
* In the merge queue and in the nightly run the shards run, and a failed,
  cancelled or skipped shard fails the fan-in.
* In the merge queue every group runs the full set: a docs-only group and a
  group with no backend change too.

The tests run the real step scripts, taken from ``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.test_ci_backend_python_skip import _bash, _detect, _jobs, _step_script

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_SUMMARY_LINE = "The integration tests run in the merge queue."

# The heavy jobs that read the outputs of `changes` on a pull request. In
# the merge queue each of them must start for every group.
_HEAVY_JOBS = (
    "python-lint",
    "python-unit-shard",
    "connector-package-template",
    "python-integration-shard",
)


def _condition(job: str) -> str:
    return " ".join(str(_jobs()[job]["if"]).split())


# --- Job gates ---------------------------------------------------------------


def test_integration_shards_never_start_on_a_pull_request() -> None:
    # No `||` and no other term: no pull request event, also not from a
    # fork, can start a leg, and every other event always starts it.
    assert _condition("python-integration-shard") == "github.event_name != 'pull_request'"


def test_integration_fan_in_reports_on_every_run() -> None:
    job = _jobs()["python-integration"]
    assert job["name"] == "Python (integration testcontainers)"
    assert job["if"] == "always()"
    assert job["needs"] == ["changes", "python-integration-shard"]


@pytest.mark.parametrize("job", _HEAVY_JOBS)
def test_heavy_jobs_start_for_every_merge_queue_group(job: str) -> None:
    # On every event that is not a pull request the condition is true before
    # any output of `changes` is read.
    condition = _condition(job)
    assert condition == "github.event_name != 'pull_request'" or condition.startswith(
        "github.event_name != 'pull_request' || "
    )


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(["docs/x.md", "README.md"], id="docs-only"),
        pytest.param(["cli/main.go", "docs/x.md"], id="no-backend-change"),
    ],
)
def test_merge_queue_group_runs_the_full_set(tmp_path: Path, changed: list[str]) -> None:
    # The `changes` job reads no diff in the merge queue: the full-run values.
    outputs = _detect(tmp_path, "merge_group", changed)
    assert outputs["docs_only"] == "false"
    assert outputs["backend_python"] == "true"
    assert outputs["migrations"] == "true"


# --- The `python-integration` fan-in -----------------------------------------


def _integration_fan_in(
    tmp_path: Path, *, event: str, shards: str, detect: str = "success"
) -> tuple[bool, str]:
    script = _step_script("python-integration", name="Assert every integration shard passed")
    assert "${{" not in script
    summary = tmp_path / "summary.md"
    summary.write_text("")
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_STEP_SUMMARY": str(summary),
        "EVENT_NAME": event,
        "CHANGES_RESULT": detect,
        "SHARDS_RESULT": shards,
    }
    result = _bash(script, tmp_path, env)
    return result.returncode == 0, summary.read_text()


def test_integration_fan_in_passes_on_a_pull_request(tmp_path: Path) -> None:
    passed, summary = _integration_fan_in(tmp_path, event="pull_request", shards="skipped")
    assert passed
    assert summary.strip() == _SUMMARY_LINE


@pytest.mark.parametrize(
    ("event", "shards", "passes"),
    [
        # A pull request: the shards never start. A red result still fails.
        ("pull_request", "failure", False),
        ("pull_request", "cancelled", False),
        # Merge queue and nightly: every shard must pass.
        ("merge_group", "success", True),
        ("merge_group", "failure", False),
        ("merge_group", "cancelled", False),
        ("merge_group", "skipped", False),
        ("schedule", "success", True),
        ("schedule", "failure", False),
        ("schedule", "cancelled", False),
        ("schedule", "skipped", False),
    ],
)
def test_integration_fan_in_result(tmp_path: Path, event: str, shards: str, passes: bool) -> None:
    passed, summary = _integration_fan_in(tmp_path, event=event, shards=shards)
    assert passed is passes
    if event != "pull_request":
        assert _SUMMARY_LINE not in summary


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "schedule"])
def test_integration_fan_in_fails_when_the_detector_did_not_pass(
    tmp_path: Path, event: str
) -> None:
    passed, _ = _integration_fan_in(tmp_path, event=event, shards="skipped", detect="failure")
    assert not passed
