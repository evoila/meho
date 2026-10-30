# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the merge-queue cleanup (#5292).

``.github/workflows/merge-queue-cleanup.yml`` runs
``scripts/ci/merge_queue_cleanup.sh`` every 5 minutes. The script cancels the
merge-queue runs of every workflow whose group the queue no longer holds. A
wrong cancel is expensive: it fails a live group, and the queue then builds
every group behind it again. So the script cancels a run only when its commit
is not in the queue, its queue branch is gone and its commit is not on
``main``, and it reads the queue and the exact branch once more right before
each cancel. Any failed read stops the sweep.

These tests source the real script with ``gh`` replaced by a shell function.
The stub answers each kind of read from a file; ``<key>-<n>.json`` answers the
n-th read of that kind, so a second look can differ from the first. The shell
is started the way the runner starts a ``run:`` step
(``bash --noprofile --norc -eo pipefail``).

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
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "merge_queue_cleanup.sh"
_PREFIX = "gh-readonly-queue/main/"

# An answer the stubbed ``gh`` gives as an error instead of a body.
_API_FAILURE = "__FAIL__"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq not installed; the cleanup script runs jq",
)


def _workflow() -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(_WORKFLOW.read_text()))


def _run(
    run_id: int, sha: str, name: str = "CI", pr: int | None = None, base: str = "main"
) -> dict[str, Any]:
    """A run of the group for pull request ``pr`` (default: the run id)."""
    pr = run_id if pr is None else pr
    return {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "head_branch": f"gh-readonly-queue/{base}/pr-{pr}-{sha}",
    }


def _pages(*pages: list[dict[str, Any]]) -> str:
    """What ``gh api --paginate --slurp`` prints: one runs answer per page."""
    total = sum(len(page) for page in pages)
    return json.dumps([{"total_count": total, "workflow_runs": page} for page in pages])


def _queue(*shas: str | None, total: int | None = None) -> str:
    nodes = [{"headCommit": {"oid": sha} if sha else None} for sha in shas]
    entries = {"totalCount": len(shas) if total is None else total, "nodes": nodes}
    return json.dumps({"data": {"repository": {"mergeQueue": {"entries": entries}}}})


def _refs(*runs: dict[str, Any]) -> str:
    return json.dumps([{"ref": f"refs/heads/{run['head_branch']}"} for run in runs])


def _compare(merge_base: str) -> str:
    return json.dumps({"status": "diverged", "merge_base_commit": {"sha": merge_base}})


# The runs of one sweep, one per case the sweep must tell apart.
_LIVE = _run(1, "live")  # its group is in the queue
_DROPPED = _run(2, "dropped")  # group gone: not queued, no branch, not merged
_DROPPED_SCAN = _run(3, "dropped", name="Security Scan", pr=2)  # same group, other workflow
_MERGED = _run(4, "merged")  # merged: not queued, no branch, on main
_BRANCH_LEFT = _run(5, "branch-left")  # not queued, but its branch still exists
_OTHER_BASE = _run(6, "other", base="release")  # a queue of another branch
_DROPPED_QUEUED = _run(7, "dropped-queued", name="chart")  # waiting for a runner


def _answers(**changed: str) -> dict[str, str]:
    """The answers of one sweep that has every case above; ``changed`` on top."""
    answers = {
        "queued": _pages([_DROPPED_QUEUED]),
        "in_progress": _pages([_LIVE, _DROPPED, _DROPPED_SCAN, _MERGED, _BRANCH_LEFT, _OTHER_BASE]),
        # A queued entry that waits for its turn has no commit yet.
        "queue": _queue("live", None),
        "refs": _refs(_LIVE, _BRANCH_LEFT),
        # The second look at one exact branch: gone.
        "branch": "[]",
        "compare": _compare("old-main-commit"),
        "compare-merged": _compare("merged"),
    }
    answers.update(changed)
    return answers


class _Outcome:
    def __init__(self, result: subprocess.CompletedProcess[str], log_dir: Path) -> None:
        self.returncode = result.returncode
        self.output = result.stdout + result.stderr
        self.calls = (log_dir / "calls.log").read_text().splitlines()
        self.cancelled = [int(call.split()[1]) for call in self.calls if call.startswith("cancel ")]


def _sweep(
    tmp_path: Path,
    answers: dict[str, str],
    *,
    dry_run: bool = False,
    failing_cancels: tuple[int, ...] = (),
) -> _Outcome:
    for key, body in answers.items():
        (tmp_path / f"{key}.json").write_text(body)
    (tmp_path / "cancel-fails.txt").write_text("".join(f"{run_id}\n" for run_id in failing_cancels))
    (tmp_path / "calls.log").write_text("")

    harness = f"""
export GITHUB_REPOSITORY=example/repo GH_TOKEN=x BASE_BRANCH=main
export DRY_RUN={"true" if dry_run else "false"}
dir={json.dumps(str(tmp_path))}
answer() {{
  local n f
  n=$(( $(cat "$dir/count-$1" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$dir/count-$1"
  f="$dir/$1-$n.json"
  [ -f "$f" ] || f="$dir/$1.json"
  if [ "$(cat "$f")" = "{_API_FAILURE}" ]; then echo "gh: HTTP 503" >&2; return 1; fi
  cat "$f"
}}
gh() {{
  if [ "$1" = api ] && [ "$2" = -X ]; then
    id="${{4%/cancel}}"; id="${{id##*/}}"
    echo "cancel $id" >> "$dir/calls.log"
    if grep -qx "$id" "$dir/cancel-fails.txt"; then echo "gh: HTTP 409" >&2; return 1; fi
    return 0
  fi
  echo "$2" >> "$dir/calls.log"
  case "$2" in
    *status=queued*) answer queued ;;
    *status=in_progress*) answer in_progress ;;
    graphql) answer queue ;;
    */git/matching-refs/heads/{_PREFIX}) answer refs ;;
    */git/matching-refs/heads/*) answer branch ;;
    */compare/*)
      sha="${{2#*/compare/}}"; sha="${{sha%%...*}}"
      if [ -f "$dir/compare-$sha.json" ]; then answer "compare-$sha"; else answer compare; fi ;;
    *) echo "unexpected gh call: $*" >&2; return 2 ;;
  esac
}}
source {json.dumps(str(_SCRIPT))}
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


def test_workflow_jobs_and_permissions() -> None:
    """Only the sweep can cancel; the dry run is read-only; no run happens in the queue."""
    workflow = _workflow()
    # PyYAML reads the bare key ``on`` as the boolean True.
    triggers = workflow[True]
    assert "merge_group" not in triggers
    assert {"schedule", "workflow_dispatch", "pull_request"} <= set(triggers)
    assert "scripts/ci/merge_queue_cleanup.sh" in triggers["pull_request"]["paths"]
    assert workflow["permissions"] == {"contents": "read"}

    sweep, dry_run = workflow["jobs"]["sweep"], workflow["jobs"]["dry-run"]
    assert sweep["if"] == "github.event_name != 'pull_request'"
    assert dry_run["if"] == "github.event_name == 'pull_request'"
    assert sweep["permissions"] == {"contents": "read", "actions": "write"}
    assert dry_run["permissions"] == {"contents": "read", "actions": "read"}
    # Scheduled and manual sweeps share one group with no expression in it.
    assert sweep["concurrency"] == {"group": "merge-queue-cleanup", "cancel-in-progress": False}
    for job, dry in ((sweep, "false"), (dry_run, "true")):
        assert job["runs-on"] == "ubuntu-latest"
        step = job["steps"][-1]
        assert step["run"] == "bash scripts/ci/merge_queue_cleanup.sh"
        assert step["env"]["BASE_BRANCH"] == "main"
        assert step["env"]["DRY_RUN"] == dry


def test_cancels_only_the_runs_of_groups_that_are_gone(tmp_path: Path) -> None:
    """Live, merged, still-branched and other-branch runs stay; dropped runs of any workflow go."""
    outcome = _sweep(tmp_path, _answers())
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED["id"], _DROPPED_SCAN["id"], _DROPPED_QUEUED["id"]]
    assert "Cancelled run 3 (Security Scan)" in outcome.output
    assert "commit merged is on main" in outcome.output


def test_every_cancel_follows_a_fresh_look_at_the_queue_and_its_branch(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers())
    by_id = {run["id"]: run for run in (_DROPPED, _DROPPED_SCAN, _DROPPED_QUEUED)}
    for index, call in enumerate(outcome.calls):
        if call.startswith("cancel "):
            branch = by_id[int(call.split()[1])]["head_branch"]
            assert outcome.calls[index - 2] == "graphql", outcome.calls
            assert outcome.calls[index - 1].endswith(f"/git/matching-refs/heads/{branch}")


def test_same_commit_back_in_the_queue_is_not_cancelled(tmp_path: Path) -> None:
    """The queue drops a group, then a new group with the same commit appears."""
    answers = _answers(
        # The first read misses the commit; every later read shows the new group.
        **{"queue-1": _queue("live"), "queue": _queue("live", "dropped")},
    )
    outcome = _sweep(tmp_path, answers)
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED_QUEUED["id"]]
    assert "commit dropped is in the queue again; keeping run 2 (CI)" in outcome.output
    assert "keeping run 3 (Security Scan)" in outcome.output


def test_branch_made_again_is_not_cancelled(tmp_path: Path) -> None:
    """The queue drops a group, then builds a new group on the same branch."""
    again = _refs(_DROPPED)
    outcome = _sweep(tmp_path, _answers(**{"branch-1": again, "branch-2": again}))
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED_QUEUED["id"]]
    assert f"branch {_DROPPED['head_branch']} exists again" in outcome.output


def test_reads_every_page_and_cancels_each_run_once(tmp_path: Path) -> None:
    late = _run(8, "dropped-late")
    answers = _answers(
        # Run 2 is listed twice: it started between the two lists.
        queued=_pages([_DROPPED]),
        in_progress=_pages([_LIVE, _DROPPED], [late]),
    )
    outcome = _sweep(tmp_path, answers)
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED["id"], late["id"]]


def test_no_open_runs_ends_after_listing_them(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers(queued=_pages([]), in_progress=_pages([])))
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert len(outcome.calls) == 2, outcome.calls


def test_every_run_in_the_queue_ends_after_the_queue_read(tmp_path: Path) -> None:
    outcome = _sweep(
        tmp_path, _answers(queued=_pages([]), in_progress=_pages([_LIVE]), queue=_queue("live"))
    )
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert len(outcome.calls) == 3, outcome.calls


@pytest.mark.parametrize(
    "failing",
    # The two run lists, the first queue read, the branch list, the merge-base
    # check, and the second look at the queue and at the branch.
    ["queued", "in_progress", "queue-1", "refs", "compare", "queue-2", "branch-1"],
)
def test_a_failed_read_before_the_first_cancel_cancels_nothing(
    tmp_path: Path, failing: str
) -> None:
    outcome = _sweep(tmp_path, _answers(**{failing: _API_FAILURE}))
    assert outcome.returncode == 1, outcome.output
    assert outcome.cancelled == [], outcome.output
    assert "The sweep stopped" in outcome.output


def test_a_failed_read_after_a_cancel_stops_the_rest(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers(**{"queue-3": _API_FAILURE}))
    assert outcome.returncode == 1, outcome.output
    assert outcome.cancelled == [_DROPPED["id"]]


@pytest.mark.parametrize(
    ("key", "body"),
    [
        # The token cannot see the queue, or the branch has no queue.
        ("queue", json.dumps({"data": {"repository": {"mergeQueue": None}}})),
        ("queue", json.dumps({"data": None, "errors": [{"message": "not accessible"}]})),
        # More entries than the one page the sweep reads.
        ("queue", _queue("live", total=101)),
        ("refs", "not json"),
        ("refs", json.dumps({"message": "Not Found"})),
        ("branch", json.dumps({"message": "Not Found"})),
        ("compare", json.dumps({"message": "Not Found"})),
    ],
)
def test_an_unreadable_answer_cancels_nothing(tmp_path: Path, key: str, body: str) -> None:
    outcome = _sweep(tmp_path, _answers(**{key: body}))
    assert outcome.returncode == 1, outcome.output
    assert outcome.cancelled == [], outcome.output
    assert "The sweep stopped" in outcome.output


def test_dry_run_does_every_read_but_cancels_nothing(tmp_path: Path) -> None:
    outcome = _sweep(tmp_path, _answers(), dry_run=True)
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == []
    assert outcome.calls.count("graphql") == 4, outcome.calls
    assert "dry run: would cancel run 2 (CI)" in outcome.output


def test_a_failed_cancel_does_not_stop_the_others(tmp_path: Path) -> None:
    """A run that finished between the list and the cancel answers 409."""
    outcome = _sweep(tmp_path, _answers(), failing_cancels=(_DROPPED["id"],))
    assert outcome.returncode == 0, outcome.output
    assert outcome.cancelled == [_DROPPED["id"], _DROPPED_SCAN["id"], _DROPPED_QUEUED["id"]]
    assert "Could not cancel run 2 (CI)" in outcome.output
