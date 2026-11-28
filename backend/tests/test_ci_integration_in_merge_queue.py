# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What CI runs on a pull request, in the merge queue and at night (#7183).

* On a pull request the four integration shards never start. The fan-in
  ``Python (integration testcontainers)`` still reports, passes and says in
  its summary that the integration tests run in the merge queue.
* In the merge queue and in the nightly run the shards run, and a failed
  shard fails the fan-in.
* In the merge queue the ``changes`` job reads the diff of the whole group,
  from its base commit to its head. A docs-only group skips the heavy Python
  jobs, a group with no backend change runs the short job, and a group whose
  diff cannot be read runs everything.

The tests run the real step scripts, taken from ``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.test_ci_backend_python_skip import _bash, _detect, _jobs, _step_script

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_SUMMARY_LINE = "The integration tests run in the merge queue."

_QUEUE_FULL_RUN = {
    "migrations": "true",
    "docs_only": "false",
    "backend_python": "true",
    "redaction_compare": "true",
    "single_connector": "",
}


def _condition(job: str) -> str:
    return " ".join(str(_jobs()[job]["if"]).split())


# --- Job gates ---------------------------------------------------------------


def test_integration_shards_never_start_on_a_pull_request() -> None:
    condition = _condition("python-integration-shard")
    assert condition.startswith("github.event_name != 'pull_request' &&")
    # A top-level `||` could open the job to a pull request again.
    assert "||" not in condition
    assert "needs.changes.outputs.docs_only != 'true'" in condition
    # A group with no backend change still runs the integration tests.
    assert "backend_python" not in condition


def test_integration_fan_in_reports_on_every_run() -> None:
    job = _jobs()["python-integration"]
    assert job["name"] == "Python (integration testcontainers)"
    assert job["if"] == "always()"
    assert job["needs"] == ["changes", "python-integration-shard"]


def test_outside_backend_job_runs_for_a_queue_group_without_backend_change() -> None:
    condition = _condition("python-outside-backend")
    assert (
        "(github.event_name == 'merge_group' && needs.changes.outputs.backend_python == 'false')"
        in condition
    )
    runs_on = _jobs()["python-outside-backend"]["runs-on"]
    assert runs_on == (
        "${{ github.event_name == 'merge_group' && 'meho-runners-ci' || 'ubuntu-latest' }}"
    )


@pytest.mark.parametrize("job", ["python-lint", "python-unit-shard"])
def test_heavy_unit_jobs_read_the_outputs_in_the_merge_queue_too(job: str) -> None:
    condition = _condition(job)
    assert condition.endswith(
        "needs.changes.outputs.docs_only != 'true' && "
        "needs.changes.outputs.backend_python != 'false'"
    )
    # No `github.event_name != 'pull_request' ||` before the outputs: that
    # made the merge queue run these jobs for every group.
    assert "github.event_name != 'pull_request' || (needs" not in condition


def test_queue_base_reaches_the_detect_step() -> None:
    for step in _jobs()["changes"]["steps"]:
        if step.get("id") == "detect":
            assert step["env"] == {"QUEUE_BASE_SHA": "${{ github.event.merge_group.base_sha }}"}
            return
    raise AssertionError("detect step not found")


# --- The `changes` detector in the merge queue -------------------------------


def test_docs_only_queue_group_skips_the_heavy_python_jobs(tmp_path: Path) -> None:
    outputs = _detect(
        tmp_path, "merge_group", ["docs/x.md"], later=["README.md"], queue_base="base"
    )
    # The migration gate, the compare job and the unit shards keep the
    # full-run values; only the two skip outputs come from the diff.
    assert outputs == {**_QUEUE_FULL_RUN, "docs_only": "true", "backend_python": "false"}


def test_queue_group_without_backend_change_runs_the_short_job(tmp_path: Path) -> None:
    outputs = _detect(
        tmp_path, "merge_group", ["cli/main.go"], later=["docs/x.md"], queue_base="base"
    )
    assert outputs == {**_QUEUE_FULL_RUN, "backend_python": "false"}


def test_queue_group_with_a_backend_change_runs_everything(tmp_path: Path) -> None:
    outputs = _detect(
        tmp_path,
        "merge_group",
        ["backend/src/meho_backplane/connectors/loki/__init__.py"],
        queue_base="base",
    )
    # A one-connector change still runs the six unit shards in the queue.
    assert outputs == _QUEUE_FULL_RUN


def test_queue_group_reads_every_pull_request_of_the_group(tmp_path: Path) -> None:
    # The first pull request of the group changes code, the second only
    # docs. The diff against HEAD^1 would be docs only; the group is not.
    outputs = _detect(
        tmp_path,
        "merge_group",
        ["backend/src/meho_backplane/app.py"],
        later=["docs/x.md"],
        queue_base="base",
    )
    assert outputs == _QUEUE_FULL_RUN


@pytest.mark.parametrize(
    "queue_base",
    [
        # No base commit in the event.
        "",
        # Not a commit id.
        "main",
        "0123abc",
        "0123456789abcdef0123456789abcdef0123456; rm -rf /",
        # A commit id that the repository does not hold and cannot fetch
        # (the test repository has no `origin`).
        "0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_queue_group_whose_diff_cannot_be_read_runs_everything(
    tmp_path: Path, queue_base: str
) -> None:
    outputs = _detect(tmp_path, "merge_group", ["docs/x.md"], queue_base=queue_base)
    assert outputs == _QUEUE_FULL_RUN


def test_queue_group_with_an_empty_diff_runs_everything(tmp_path: Path) -> None:
    # No change commit: the head is the base commit.
    outputs = _detect(tmp_path, "merge_group", None, queue_base="base")
    assert outputs == _QUEUE_FULL_RUN


# --- The `python-integration` fan-in -----------------------------------------


def _integration_fan_in(
    tmp_path: Path, *, event: str, docs_only: str, shards: str, detect: str = "success"
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
        "CHANGES_DOCS_ONLY": docs_only,
        "SHARDS_RESULT": shards,
    }
    result = _bash(script, tmp_path, env)
    return result.returncode == 0, summary.read_text()


@pytest.mark.parametrize("docs_only", ["false", "true"])
def test_integration_fan_in_passes_on_a_pull_request(tmp_path: Path, docs_only: str) -> None:
    passed, summary = _integration_fan_in(
        tmp_path, event="pull_request", docs_only=docs_only, shards="skipped"
    )
    assert passed
    assert summary.strip() == _SUMMARY_LINE


@pytest.mark.parametrize(
    ("event", "docs_only", "shards", "passes"),
    [
        # A pull request: the shards never start. A red result still fails.
        ("pull_request", "false", "failure", False),
        ("pull_request", "false", "cancelled", False),
        # Merge queue and nightly: every shard must pass.
        ("merge_group", "false", "success", True),
        ("merge_group", "false", "failure", False),
        ("merge_group", "false", "cancelled", False),
        ("merge_group", "false", "skipped", False),
        ("schedule", "false", "success", True),
        ("schedule", "false", "failure", False),
        ("schedule", "false", "skipped", False),
        ("merge_group", "", "skipped", False),
        # A docs-only merge queue group: the shards skip.
        ("merge_group", "true", "skipped", True),
        ("merge_group", "true", "failure", False),
    ],
)
def test_integration_fan_in_result(
    tmp_path: Path, event: str, docs_only: str, shards: str, passes: bool
) -> None:
    passed, summary = _integration_fan_in(tmp_path, event=event, docs_only=docs_only, shards=shards)
    assert passed is passes
    if event != "pull_request":
        assert _SUMMARY_LINE not in summary


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "schedule"])
def test_integration_fan_in_fails_when_the_detector_did_not_pass(
    tmp_path: Path, event: str
) -> None:
    passed, _ = _integration_fan_in(
        tmp_path, event=event, docs_only="", shards="skipped", detect="failure"
    )
    assert not passed


# --- The `python-unit-lane` fan-in in the merge queue ------------------------


def _unit_fan_in(tmp_path: Path, *, event: str, docs_only: str, outside: str) -> bool:
    script = _step_script(
        "python-unit-lane", name="Assert lint + typecheck + every pytest shard passed"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "IS_FORK_PR": "false",
        "EVENT_NAME": event,
        "CHANGES_RESULT": "success",
        "CHANGES_DOCS_ONLY": docs_only,
        "CHANGES_BACKEND_PYTHON": "false",
        "CHANGES_SINGLE_CONNECTOR": "",
        "LINT_RESULT": "skipped",
        "SHARDS_RESULT": "skipped",
        "OUTSIDE_RESULT": outside,
        "CHANGES_SECOND_SITE": "",
        "SECOND_SITE_RESULT": "skipped",
    }
    return _bash(script, tmp_path, env).returncode == 0


@pytest.mark.parametrize(
    ("event", "docs_only", "outside", "passes"),
    [
        # A docs-only queue group needs the short job to pass.
        ("merge_group", "true", "success", True),
        ("merge_group", "true", "skipped", False),
        ("merge_group", "true", "failure", False),
        # A queue group with no backend change: the same.
        ("merge_group", "false", "success", True),
        ("merge_group", "false", "skipped", False),
        # A docs-only pull request keeps its fast path.
        ("pull_request", "true", "skipped", True),
    ],
)
def test_unit_fan_in_in_the_merge_queue(
    tmp_path: Path, event: str, docs_only: str, outside: str, passes: bool
) -> None:
    assert _unit_fan_in(tmp_path, event=event, docs_only=docs_only, outside=outside) is passes
