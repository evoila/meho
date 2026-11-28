# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The compatibility check copies its result for a label or title edit (#7105).

``.github/workflows/compat-gates.yml`` starts on a label change and on an
edit of the pull request, because the check reads the ``breaking-change``
label and the text. ``scripts/ci/compat_gates_reuse.sh`` decides whether such
a run needs the full check, or copies the result of the newest earlier run on
the same commit. These tests run the real script with fake ``gh`` and
``sleep`` programs, the way the runner starts a ``run:`` step
(``bash --noprofile --norc -eo pipefail``). ``jq`` is the only real tool the
script needs. Where it is missing the tests skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "compat-gates.yml"
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "compat_gates_reuse.sh"
_CHECK_NAME = "REST API, MCP tools and Helm values compatibility"
_SHA = "a" * 40
_RUN_ID = 500
_PR = 42

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the script runs jq"
)


def _workflow() -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(_WORKFLOW.read_text()))


def _runs(*ids: int, pr: int | None = _PR) -> str:
    pulls = [] if pr is None else [{"number": pr}]
    return json.dumps({"workflow_runs": [{"id": i, "pull_requests": pulls} for i in ids]})


def _jobs(status: str = "completed", conclusion: str | None = "success") -> str:
    return json.dumps(
        {
            "jobs": [
                {
                    "name": "Find the result of this commit",
                    "status": "completed",
                    "conclusion": "success",
                },
                {"name": _CHECK_NAME, "status": status, "conclusion": conclusion},
            ]
        }
    )


@dataclass
class _Outcome:
    returncode: int
    outputs: dict[str, str]
    output: str
    calls: list[str]
    sleeps: int


def _decide(
    tmp_path: Path,
    *,
    action: str = "labeled",
    label: str = "documentation",
    changes: str = "null",
    runs: str | None = None,
    jobs: list[str] | None = None,
    sha: str = _SHA,
) -> _Outcome:
    """Run the script. ``gh`` answers the runs list with ``runs`` and the n-th
    jobs read with ``jobs[n]`` (the last one repeats); ``"FAIL"`` is an error."""
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "runs").write_text(_runs(400) if runs is None else runs)
    for i, body in enumerate(jobs or [_jobs()]):
        (answers / f"jobs-{i}").write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "gh").write_text(
        f"""#!/bin/bash
echo "$2" >> {calls}
if [[ "$2" == *"/jobs?"* ]]; then
  n=$(grep -c '/jobs?' {calls}); n=$((n - 1))
  f={answers}/jobs-$n
  [ -f "$f" ] || f=$(ls {answers}/jobs-* | sort -V | tail -1)
else
  f={answers}/runs
fi
[ "$(cat "$f")" = FAIL ] && exit 1
cat "$f"
"""
    )
    (bin_dir / "sleep").write_text(f"#!/bin/bash\necho sleep >> {tmp_path}/sleeps\n")
    for tool in ("gh", "sleep"):
        (bin_dir / tool).chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": str(_RUN_ID),
        "EVENT_ACTION": action,
        "LABEL_NAME": label,
        "CHANGES_JSON": changes,
        "HEAD_SHA": sha,
        "PR_NUMBER": str(_PR),
        "CHECK_NAME": _CHECK_NAME,
        "WORKFLOW_FILE": "compat-gates.yml",
        "POLLS": "3",
        "POLL_SECONDS": "1",
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    sleeps = tmp_path / "sleeps"
    return _Outcome(
        returncode=result.returncode,
        outputs=dict(line.split("=", 1) for line in output.read_text().splitlines()),
        output=result.stdout + result.stderr,
        calls=calls.read_text().splitlines() if calls.exists() else [],
        sleeps=len(sleeps.read_text().splitlines()) if sleeps.exists() else 0,
    )


_FULL = {"reuse": "false", "conclusion": ""}


@pytest.mark.parametrize("conclusion", ["success", "failure"])
@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_an_unrelated_label_copies_the_last_result(
    tmp_path: Path, action: str, conclusion: str
) -> None:
    outcome = _decide(tmp_path, action=action, jobs=[_jobs(conclusion=conclusion)])
    assert outcome.returncode == 0, outcome.output
    assert outcome.outputs == {"reuse": "true", "conclusion": conclusion}
    assert outcome.calls == [
        f"repos/owner/repo/actions/workflows/compat-gates.yml/runs?head_sha={_SHA}"
        "&event=pull_request&per_page=100",
        "repos/owner/repo/actions/runs/400/jobs?per_page=100",
    ]


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_the_breaking_change_label_runs_the_full_check(tmp_path: Path, action: str) -> None:
    outcome = _decide(tmp_path, action=action, label="breaking-change")
    assert outcome.outputs == _FULL
    assert outcome.calls == []


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", ""])
def test_other_events_run_the_full_check(tmp_path: Path, action: str) -> None:
    outcome = _decide(tmp_path, action=action)
    assert outcome.outputs == _FULL
    assert outcome.calls == []


def test_a_title_edit_copies_the_last_result(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, action="edited", changes='{"title": {"from": "old"}}')
    assert outcome.outputs == {"reuse": "true", "conclusion": "success"}


@pytest.mark.parametrize(
    "changes",
    [
        '{"body": {"from": "old"}}',
        '{"title": {"from": "a"}, "body": {"from": "b"}}',
        '{"base": {"ref": {"from": "x"}, "sha": {"from": "y"}}}',
        "null",
        "not json",
        "",
    ],
)
def test_an_edit_of_the_text_or_base_runs_the_full_check(tmp_path: Path, changes: str) -> None:
    outcome = _decide(tmp_path, action="edited", changes=changes)
    assert outcome.returncode == 0, outcome.output
    assert outcome.outputs == _FULL
    assert outcome.calls == []


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "cancelled"),
        ("completed", "skipped"),
        ("completed", "timed_out"),
        ("completed", None),
    ],
)
def test_no_finished_result_runs_the_full_check(
    tmp_path: Path, status: str, conclusion: str | None
) -> None:
    outcome = _decide(tmp_path, jobs=[_jobs(status, conclusion)])
    assert outcome.outputs == _FULL
    assert "no finished result to copy" in outcome.output


def test_copies_only_the_newest_earlier_run(tmp_path: Path) -> None:
    # Run 600 started after this one; 450 is the newest before it.
    outcome = _decide(tmp_path, runs=_runs(300, 450, 600, _RUN_ID))
    assert outcome.outputs["reuse"] == "true"
    assert outcome.calls[-1] == "repos/owner/repo/actions/runs/450/jobs?per_page=100"


def test_a_newer_cancelled_run_hides_an_older_pass(tmp_path: Path) -> None:
    # The newest run on the commit was cancelled: what it would have said is
    # unknown, so an older pass must not be copied.
    outcome = _decide(tmp_path, runs=_runs(300, 450), jobs=[_jobs("completed", "cancelled")])
    assert outcome.outputs == _FULL
    assert outcome.calls[-1] == "repos/owner/repo/actions/runs/450/jobs?per_page=100"


def test_runs_of_another_pull_request_do_not_count(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, runs=_runs(400, pr=7))
    assert outcome.outputs == _FULL
    assert "no earlier run" in outcome.output


def test_a_fork_run_without_pull_request_counts(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, runs=_runs(400, pr=None))
    assert outcome.outputs["reuse"] == "true"


def test_no_earlier_run_runs_the_full_check(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, runs=_runs())
    assert outcome.outputs == _FULL
    assert "no earlier run" in outcome.output


def test_waits_for_an_earlier_run_that_still_checks(tmp_path: Path) -> None:
    outcome = _decide(
        tmp_path,
        jobs=[_jobs("in_progress", None), _jobs("queued", None), _jobs("completed", "failure")],
    )
    assert outcome.outputs == {"reuse": "true", "conclusion": "failure"}
    assert outcome.sleeps == 2


def test_an_earlier_run_that_does_not_finish_runs_the_full_check(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, jobs=[_jobs("in_progress", None)])
    assert outcome.outputs == _FULL
    assert outcome.sleeps == 2
    assert "did not finish in time" in outcome.output


@pytest.mark.parametrize(
    ("runs", "jobs"),
    [
        ("FAIL", None),
        ("not json", None),
        (None, ["FAIL"]),
        (None, ["not json"]),
        (None, [json.dumps({"jobs": []})]),
        (None, [_jobs("in_progress", None), "FAIL"]),
    ],
)
def test_a_failed_read_runs_the_full_check(
    tmp_path: Path, runs: str | None, jobs: list[str] | None
) -> None:
    outcome = _decide(tmp_path, runs=runs, jobs=jobs)
    assert outcome.returncode == 0, outcome.output
    assert outcome.outputs == _FULL


def test_a_bad_head_commit_runs_the_full_check(tmp_path: Path) -> None:
    outcome = _decide(tmp_path, sha="main&x=1")
    assert outcome.outputs == _FULL
    assert outcome.calls == []


# --- The workflow -----------------------------------------------------------------


def test_the_check_job_is_never_skipped_for_a_copy() -> None:
    jobs = _workflow()["jobs"]
    compat = jobs["compat"]
    assert compat["name"] == _CHECK_NAME
    assert compat["needs"] == "reuse"
    assert compat["if"] == "${{ !cancelled() }}"
    copy, *check = compat["steps"]
    assert copy["if"] == "env.REUSE == 'true'"
    assert all(step["if"] == "env.REUSE != 'true'" for step in check)
    reuse_step = jobs["reuse"]["steps"][-1]
    assert reuse_step["env"]["CHECK_NAME"] == _CHECK_NAME
    assert reuse_step["run"] == "bash scripts/ci/compat_gates_reuse.sh"


def _copy_step(conclusion: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "copy.sh"
    script.write_text(_workflow()["jobs"]["compat"]["steps"][0]["run"])
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env={"PATH": "/usr/bin:/bin", "CONCLUSION": conclusion},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_copied_failure_fails_the_job(tmp_path: Path) -> None:
    assert _copy_step("success", tmp_path).returncode == 0
    assert _copy_step("failure", tmp_path).returncode == 1
    assert _copy_step("", tmp_path).returncode == 1


def test_a_copy_runs_on_a_hosted_runner() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["reuse"]["runs-on"] == "ubuntu-latest"
    assert jobs["compat"]["runs-on"].startswith("${{ (needs.reuse.outputs.reuse == 'true' || ")
    assert "'ubuntu-latest' || 'meho-runners-ci' }}" in jobs["compat"]["runs-on"]


def test_a_copy_run_does_not_cancel_the_check_of_the_commit() -> None:
    group = _workflow()["concurrency"]["group"]
    assert "github.event.label.name != 'breaking-change'" in group
    assert "github.event.changes.body == null && github.event.changes.base == null" in group
    assert "format('-copy-{0}', github.run_id)" in group
