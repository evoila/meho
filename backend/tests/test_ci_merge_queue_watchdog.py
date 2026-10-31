# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the merge-queue watchdog in ``.github/workflows/ci.yml``.

The watchdog (#4732) is a shell loop in the ``merge-queue-watchdog`` job. It
cancels its own merge-queue run when a job that carries or feeds a required
check did not pass, or when the queue has dropped the group and deleted the
queue branch. It must never cancel a run that passed and merged.

A merged run has also lost its queue branch, so the watchdog may only look at
the branch while a required job is still running, and it reads the jobs once
more after the branch is gone, because the group can merge between the two
lookups. These tests run the real
step script, taken from the workflow file, with ``gh``, ``curl``, ``sleep``
and ``timeout`` replaced by shell functions. The shell is started the way the
runner starts a ``run:`` step (``bash --noprofile --norc -eo pipefail``), so
a command that fails under ``-e`` fails here too.

``jq`` is the only real tool the script needs. Where it is missing the tests
skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_WATCHDOG_JOB = "merge-queue-watchdog"
_OPTIONAL_JOB = "cli-api-snapshot-freshness"

# A response the stubbed jobs API answers with an error instead of a body.
_API_FAILURE = "__FAIL__"
# After this many jobs-API polls the stubbed ``sleep`` ends the loop.
_MAX_POLLS = 4

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq not installed; the watchdog step runs jq",
)


def _workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))


def _watchdog_step() -> dict[str, Any]:
    (step,) = _workflow()["jobs"][_WATCHDOG_JOB]["steps"]
    return cast("dict[str, Any]", step)


def _job(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def _jobs(*changed: dict[str, Any]) -> str:
    """A jobs-API answer: every job finished green, with ``changed`` on top.

    The names are display names, as the jobs API returns them. Only the two
    names the step reads (its own and the optional job) have to match the
    workflow; the rest stand for the jobs that carry or feed required checks.
    """
    step_env = _watchdog_step()["env"]
    by_name = {
        job["name"]: job
        for job in (
            _job("Detect migration + docs-only changes"),
            _job("Python lint + typecheck (ruff + mypy)"),
            _job("Python unit shard 1/5"),
            _job("Python unit shard 2/5"),
            _job("Python (ruff + mypy + pytest)"),
            _job("Python (integration testcontainers)"),
            _job("Python (database migrations)"),
            _job("Go (golangci-lint + go test)"),
            _job("Helm (lint + template + kubeconform)"),
            _job(step_env["HARMLESS_JOB"]),
            _job(step_env["SELF_NAME"], status="in_progress", conclusion=None),
        )
    }
    for job in changed:
        by_name[job["name"]] = job
    return json.dumps({"total_count": len(by_name), "jobs": list(by_name.values())})


class _Outcome:
    def __init__(self, result: subprocess.CompletedProcess[str], log_dir: Path) -> None:
        self.returncode = result.returncode
        self.output = result.stdout + result.stderr
        self.cancelled = (log_dir / "cancel.log").exists()
        branch_log = log_dir / "branch.log"
        self.branch_lookups = len(branch_log.read_text().splitlines()) if branch_log.exists() else 0
        self.jobs_lookups = int((log_dir / "polls.txt").read_text())


def _run_watchdog(tmp_path: Path, polls: list[str], branch_code: str = "200") -> _Outcome:
    """Run the step script with stubbed tools.

    ``polls`` holds one jobs-API answer per poll; the last one repeats. The
    stubbed branch lookup always answers ``branch_code``.
    """
    step = _watchdog_step()
    (tmp_path / "watchdog.sh").write_text(step["run"])
    for index, body in enumerate(polls, start=1):
        (tmp_path / f"poll-{index}.json").write_text(body)
    (tmp_path / "poll-last.json").write_text(polls[-1])
    (tmp_path / "polls.txt").write_text("0")

    harness = f"""
export GITHUB_REPOSITORY=example/repo GITHUB_RUN_ID=123 GH_TOKEN=x
export GITHUB_API_URL=https://api.example
export QUEUE_BRANCH='gh-readonly-queue/main/pr-1-abc'
export SELF_NAME={json.dumps(step["env"]["SELF_NAME"])}
export HARMLESS_JOB={json.dumps(step["env"]["HARMLESS_JOB"])}
dir={json.dumps(str(tmp_path))}
timeout() {{ shift; "$@"; }}
sleep() {{ [ "$(cat "$dir/polls.txt")" -ge {_MAX_POLLS} ] && SECONDS=999999; return 0; }}
gh() {{
  if [ "$1" = run ] && [ "$2" = cancel ]; then echo "$3" >> "$dir/cancel.log"; return 0; fi
  n=$(( $(cat "$dir/polls.txt") + 1 )); echo "$n" > "$dir/polls.txt"
  f="$dir/poll-$n.json"; [ -f "$f" ] || f="$dir/poll-last.json"
  if [ "$(cat "$f")" = "{_API_FAILURE}" ]; then echo "gh: HTTP 503"; return 1; fi
  cat "$f"
}}
curl() {{ echo {branch_code} >> "$dir/branch.log"; echo {branch_code}; }}
source "$dir/watchdog.sh"
"""
    (tmp_path / "harness.sh").write_text(harness)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(tmp_path / "harness.sh")],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    return _Outcome(result, tmp_path)


def test_step_names_match_the_jobs_they_point_at() -> None:
    """The step reads two display names; both must track the job ``name:`` fields."""
    jobs = _workflow()["jobs"]
    env = _watchdog_step()["env"]
    assert env["SELF_NAME"] == jobs[_WATCHDOG_JOB]["name"]
    assert env["HARMLESS_JOB"] == jobs[_OPTIONAL_JOB]["name"]


def test_merged_run_with_optional_job_still_running_is_not_cancelled(tmp_path: Path) -> None:
    """Every required job passed, the optional job still runs, the branch is gone.

    The queue merges the group as soon as the required checks pass. The
    branch lookup would answer 404 here, so the watchdog must not look at the
    branch at all. It ends quietly instead.
    """
    polls = [_jobs(_job("CLI API snapshot freshness", status="in_progress", conclusion=None))]
    outcome = _run_watchdog(tmp_path, polls, branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output
    assert "nothing left to watch" in outcome.output


def test_merged_run_with_optional_job_queued_is_not_cancelled(tmp_path: Path) -> None:
    polls = [_jobs(_job("CLI API snapshot freshness", status="queued", conclusion=None))]
    outcome = _run_watchdog(tmp_path, polls, branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output


def test_all_jobs_finished_ends_quietly(tmp_path: Path) -> None:
    outcome = _run_watchdog(tmp_path, [_jobs()], branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output


def test_dropped_group_with_required_job_running_is_cancelled(tmp_path: Path) -> None:
    """The branch is gone and a second look at the jobs still shows required work open."""
    polls = [_jobs(_job("Python unit shard 2/5", status="in_progress", conclusion=None))]
    outcome = _run_watchdog(tmp_path, polls, branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled, outcome.output
    assert outcome.jobs_lookups == 2, outcome.output
    assert "queue branch" in outcome.output


def test_group_merged_between_jobs_lookup_and_branch_lookup_is_not_cancelled(
    tmp_path: Path,
) -> None:
    """The last required job passes, and the group merges, between the two lookups.

    The jobs lookup still saw a shard running. The branch lookup then answers
    404 because the merge deleted the queue branch. The watchdog must read the
    jobs again, see that every required job has finished, and not cancel.
    """
    polls = [
        _jobs(_job("Python unit shard 2/5", status="in_progress", conclusion=None)),
        _jobs(),
    ]
    outcome = _run_watchdog(tmp_path, polls, branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 1, outcome.output
    assert "since the last look" in outcome.output
    assert "nothing left to watch" in outcome.output


def test_jobs_unreadable_after_branch_is_gone_keeps_the_run(tmp_path: Path) -> None:
    """A 404 with no fresh view of the jobs is not enough to cancel."""
    polls = [
        _jobs(_job("Python unit shard 2/5", status="in_progress", conclusion=None)),
        _API_FAILURE,
    ]
    outcome = _run_watchdog(tmp_path, polls, branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 1, outcome.output
    assert "could not be read again" in outcome.output


def test_live_group_with_required_job_running_keeps_the_run(tmp_path: Path) -> None:
    polls = [_jobs(_job("Python unit shard 2/5", status="in_progress", conclusion=None))]
    outcome = _run_watchdog(tmp_path, polls, branch_code="200")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == _MAX_POLLS


def test_failed_required_job_is_cancelled_before_the_branch_check(tmp_path: Path) -> None:
    polls = [
        _jobs(
            _job("Go (golangci-lint + go test)", conclusion="failure"),
            _job("Python unit shard 2/5", status="in_progress", conclusion=None),
        )
    ]
    outcome = _run_watchdog(tmp_path, polls, branch_code="200")
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output
    assert "Go (golangci-lint + go test)" in outcome.output


def test_failed_optional_job_alone_does_not_cancel(tmp_path: Path) -> None:
    polls = [_jobs(_job("CLI API snapshot freshness", conclusion="failure"))]
    outcome = _run_watchdog(tmp_path, polls, branch_code="200")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output


def test_jobs_api_failure_after_merge_does_not_cancel(tmp_path: Path) -> None:
    """When the jobs cannot be listed, the branch must not be checked."""
    outcome = _run_watchdog(tmp_path, [_API_FAILURE], branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output
    assert "jobs API did not answer" in outcome.output


def test_unreadable_jobs_answer_keeps_the_watchdog_alive(tmp_path: Path) -> None:
    """``bash -e`` must not end the step on a jq parse error."""
    outcome = _run_watchdog(tmp_path, ["<html>not json</html>"], branch_code="404")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert outcome.branch_lookups == 0, outcome.output
    assert "could not be read" in outcome.output


def test_fan_in_waiting_on_needs_is_seen_on_the_second_poll(tmp_path: Path) -> None:
    """A job that still waits on ``needs`` is not listed yet; one quiet poll is not enough."""
    polls = [
        _jobs(),
        _jobs(_job("Python (ruff + mypy + pytest)", status="queued", conclusion=None)),
    ]
    outcome = _run_watchdog(tmp_path, polls, branch_code="200")
    assert outcome.returncode == 0, outcome.output
    assert not outcome.cancelled, outcome.output
    assert "one more poll" in outcome.output
    assert "1 required job(s) still queued or running" in outcome.output
