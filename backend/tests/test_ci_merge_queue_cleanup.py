# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for ``.github/workflows/merge-queue-cleanup.yml`` (#5292).

Every 5 minutes the workflow cancels the merge-queue runs of every workflow
whose group the queue no longer holds. A wrong cancel is expensive: it fails
a live group, and the queue then builds every group behind it again. So the
sweep cancels a run only when three reads agree (its commit is not in the
queue, its queue branch is gone, its commit is not on ``main``), and it
cancels nothing when any read fails.

These tests run the real step script, taken from the workflow file, with
``gh`` replaced by a shell function. The shell is started the way the runner
starts a ``run:`` step (``bash --noprofile --norc -eo pipefail``).

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
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "merge-queue-cleanup.yml"
_PREFIX = "gh-readonly-queue/main/"

# An answer the stubbed ``gh`` gives as an error instead of a body.
_API_FAILURE = "__FAIL__"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq not installed; the cleanup step runs jq",
)


def _workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text()))


def _run(run_id: int, sha: str, name: str = "CI", base: str = "main") -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "head_branch": f"gh-readonly-queue/{base}/pr-{run_id}-{sha}",
    }


def _runs(*runs: dict[str, Any]) -> str:
    return json.dumps({"total_count": len(runs), "workflow_runs": list(runs)})


def _queue(*shas: str | None, total: int | None = None) -> str:
    nodes = [{"headCommit": {"oid": sha} if sha else None} for sha in shas]
    entries = {"totalCount": len(shas) if total is None else total, "nodes": nodes}
    return json.dumps({"data": {"repository": {"mergeQueue": {"entries": entries}}}})


def _refs(*runs: dict[str, Any]) -> str:
    return json.dumps([{"ref": f"refs/heads/{run['head_branch']}"} for run in runs])


def _commits(*shas: str) -> str:
    return json.dumps([{"sha": sha} for sha in shas])


# The runs of one sweep, one per case the sweep must tell apart.
_LIVE = _run(1, "live")  # its group is in the queue
_DROPPED = _run(2, "dropped")  # group gone: not queued, no branch, not merged
_DROPPED_SCAN = _run(3, "dropped", name="Security Scan")  # same group, other workflow
_MERGED = _run(4, "merged")  # merged: not queued, no branch, on main
_BRANCH_LEFT = _run(5, "branch-left")  # not queued, but its branch still exists
_OTHER_BASE = _run(6, "other", base="release")  # a queue of another branch
_DROPPED_QUEUED = _run(7, "dropped-queued", name="chart")  # waiting for a runner


def _answers(**changed: str) -> dict[str, str]:
    """The answers of one sweep that has every case above; ``changed`` on top."""
    answers = {
        "queued": _runs(_DROPPED_QUEUED),
        "in_progress": _runs(_LIVE, _DROPPED, _DROPPED_SCAN, _MERGED, _BRANCH_LEFT, _OTHER_BASE),
        # A queued entry that waits for its turn has no commit yet.
        "queue": _queue("live", None),
        "refs": _refs(_LIVE, _BRANCH_LEFT),
        "commits": _commits("merged", "older-main-commit"),
    }
    answers.update(changed)
    return answers


class _Outcome:
    def __init__(self, result: subprocess.CompletedProcess[str], log_dir: Path) -> None:
        self.returncode = result.returncode
        self.output = result.stdout + result.stderr
        cancel_log = log_dir / "cancel.log"
        self.cancelled = (
            sorted(int(line) for line in cancel_log.read_text().split())
            if cancel_log.exists()
            else []
        )
        self.calls = (log_dir / "calls.log").read_text().splitlines()


def _sweep(
    tmp_path: Path,
    answers: dict[str, str],
    *,
    dry_run: bool = False,
    failing_cancels: tuple[int, ...] = (),
) -> _Outcome:
    (step,) = _workflow()["jobs"]["sweep"]["steps"]
    (tmp_path / "sweep.sh").write_text(step["run"])
    for key, body in answers.items():
        (tmp_path / f"{key}.json").write_text(body)
    (tmp_path / "cancel-fails.txt").write_text("".join(f"{run_id}\n" for run_id in failing_cancels))
    (tmp_path / "calls.log").write_text("")

    harness = f"""
export GITHUB_REPOSITORY=example/repo GH_TOKEN=x
export BASE_BRANCH={json.dumps(step["env"]["BASE_BRANCH"])}
export DRY_RUN={"true" if dry_run else "false"}
dir={json.dumps(str(tmp_path))}
gh() {{
  if [ "$1" = api ] && [ "$2" = -X ]; then
    id="${{4%/cancel}}"; id="${{id##*/}}"
    echo "cancel $id" >> "$dir/calls.log"
    if grep -qx "$id" "$dir/cancel-fails.txt"; then echo "gh: HTTP 409" >&2; return 1; fi
    echo "$id" >> "$dir/cancel.log"; return 0
  fi
  echo "$2" >> "$dir/calls.log"
  case "$2" in
    *status=queued*) f=queued ;;
    *status=in_progress*) f=in_progress ;;
    graphql) f=queue ;;
    */git/matching-refs/*) f=refs ;;
    */commits*) f=commits ;;
    *) echo "unexpected gh call: $*" >&2; return 2 ;;
  esac
  if [ "$(cat "$dir/$f.json")" = "{_API_FAILURE}" ]; then echo "gh: HTTP 503" >&2; return 1; fi
  cat "$dir/$f.json"
}}
source "$dir/sweep.sh"
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


def test_workflow_runs_outside_the_queue_on_a_hosted_runner() -> None:
    """The sweep must not be a queue check, and must not take a runner from our pools."""
    workflow = _workflow()
    # PyYAML reads the bare key ``on`` as the boolean True.
    triggers = cast("dict[Any, Any]", workflow)[True]
    assert "merge_group" not in triggers
    assert "schedule" in triggers
    assert workflow["jobs"]["sweep"]["runs-on"] == "ubuntu-latest"
    assert workflow["permissions"]["actions"] == "write"
    assert workflow["jobs"]["sweep"]["steps"][0]["env"]["BASE_BRANCH"] == "main"


def test_cancels_only_the_runs_of_groups_that_are_gone(tmp_path: Path) -> None:
    """Live, merged, still-branched and other-branch runs stay; dropped runs of any workflow go."""
    outcome = _sweep(tmp_path, _answers())
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED["id"], _DROPPED_SCAN["id"], _DROPPED_QUEUED["id"]]
    assert "Cancelled run 3 (Security Scan)" in outcome.output


def test_queue_branches_and_main_are_read_after_the_queue(tmp_path: Path) -> None:
    """A group that merges during the sweep must show up in the last read (``main``)."""
    outcome = _sweep(tmp_path, _answers())
    reads = [call for call in outcome.calls if not call.startswith("cancel ")]
    assert len(reads) == 5, reads
    assert "/actions/runs?event=merge_group&status=queued&" in reads[0]
    assert "/actions/runs?event=merge_group&status=in_progress&" in reads[1]
    assert reads[2] == "graphql"
    assert reads[3].endswith(f"/git/matching-refs/heads/{_PREFIX}")
    assert "/commits?sha=main&" in reads[4]


def test_no_open_runs_ends_after_listing_them(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers(queued=_runs(), in_progress=_runs()))
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert len(outcome.calls) == 2, outcome.calls


def test_every_run_in_the_queue_ends_after_the_queue_read(tmp_path: Path) -> None:
    outcome = _sweep(
        tmp_path, _answers(queued=_runs(), in_progress=_runs(_LIVE), queue=_queue("live"))
    )
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert len(outcome.calls) == 3, outcome.calls


@pytest.mark.parametrize("failing", ["queued", "in_progress", "queue", "refs", "commits"])
def test_a_failed_read_cancels_nothing(tmp_path: Path, failing: str) -> None:
    outcome = _sweep(tmp_path, _answers(**{failing: _API_FAILURE}))
    assert outcome.returncode == 1, outcome.output
    assert outcome.cancelled == [], outcome.output
    assert "Nothing was cancelled." in outcome.output


@pytest.mark.parametrize(
    ("key", "body"),
    [
        # The token cannot see the queue, or the branch has no queue.
        ("queue", json.dumps({"data": {"repository": {"mergeQueue": None}}})),
        ("queue", json.dumps({"data": None, "errors": [{"message": "not accessible"}]})),
        # More entries than the one page the sweep reads.
        ("queue", _queue("live", total=101)),
        ("refs", "not json"),
        ("commits", json.dumps({"message": "Not Found"})),
    ],
)
def test_an_unreadable_answer_cancels_nothing(tmp_path: Path, key: str, body: str) -> None:
    outcome = _sweep(tmp_path, _answers(**{key: body}))
    assert outcome.returncode == 1, outcome.output
    assert outcome.cancelled == [], outcome.output
    assert "Nothing was cancelled." in outcome.output


def test_dry_run_only_prints(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers(), dry_run=True)
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert not any(call.startswith("cancel ") for call in outcome.calls)
    assert "dry run: would cancel run 2 (CI)" in outcome.output


def test_a_failed_cancel_does_not_stop_the_others(tmp_path: Path) -> None:
    """A run that finished between the list and the cancel answers 409."""
    outcome = _sweep(tmp_path, _answers(), failing_cancels=(_DROPPED["id"],))
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED_SCAN["id"], _DROPPED_QUEUED["id"]]
    assert "Could not cancel run 2 (CI)" in outcome.output
