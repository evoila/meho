# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for unit shards 1-3 of a pull request on GitHub-hosted runners.

With the repository variable ``MEHO_CI_PR_HOSTED`` set to ``on``, the job
``python-unit-shard-hosted`` runs unit shards 1-3 of a pull request on
``ubuntu-latest``. The promises these tests check:

* only the free ``ubuntu-latest`` label, and a re-run uses our own pool;
* the job names no secret, no ``environment`` and no ``id-token``, and its
  steps are the steps of the second-site job, as aliases;
* never in the merge queue, the nightly run or for a fork;
* the shard split for every case, and the required check still needs all
  six shards;
* ``python-unit-shard``, the one job that checks the pinned vendor API
  files, keeps a shard on our own pool in every switch state;
* ``scripts/ci/pick_pr_hosted.sh`` picks the hosted runners only when every
  rule holds.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests import _outside_backend_tests as outside
from tests.test_ci_backend_python_skip import _detect
from tests.test_ci_second_runner_site import (
    _CI,
    _assert_ci_outside_jobs_names_no_secret,
    _assert_no_secret_environment_or_id_token,
    _bash_step,
    _ci_jobs,
    _expression,
    _job_text,
    _run,
    _step,
    _without_comments,
)

_JOB = "python-unit-shard-hosted"
_SHARD_JOB = "python-unit-shard"
_SECOND_SITE = "python-unit-shard-second-site"
# backend/tests/<this> -> parents[2] == repo root
_PICK = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "pick_pr_hosted.sh"
_REPO = "example-org/example-repo"
_HEAVY = "meho-runners-ci-heavy"
_LEFT_OUT = {
    "Check that this runner is on the second site",
    "Hide the proxy name in the test reports",
    "Upload per-test times (input for the shard split)",
}


def _context(
    event: str = "pull_request",
    head: str = _REPO,
    attempt: str = "1",
    **outputs: str,
) -> dict[str, Any]:
    values = {
        "docs_only": "false",
        "backend_python": "true",
        "single_connector": "",
        "test_map": "false",
        "second_site": "",
        "pr_hosted": "",
        **outputs,
    }
    return {
        "github.event_name": event,
        "github.event.pull_request.head.repo.full_name": head,
        "github.repository": _REPO,
        "github.run_attempt": attempt,
        **{f"needs.changes.outputs.{key}": value for key, value in values.items()},
    }


def _runs(job: str, context: dict[str, Any]) -> bool:
    return bool(_expression("${{ " + _ci_jobs()[job]["if"] + " }}", context))


def _runner(job: str, context: dict[str, Any]) -> str:
    return cast("str", _expression(_ci_jobs()[job]["runs-on"], context))


def _shard_matrix(job: str, context: dict[str, Any]) -> list[int]:
    value = _ci_jobs()[job]["strategy"]["matrix"]["shard"]
    if isinstance(value, list):
        return cast("list[int]", value)
    text = str(value)
    inner = re.fullmatch(r"\$\{\{ fromJSON\((?P<body>.*)\) \}\}", text)
    assert inner is not None, text
    return cast("list[int]", json.loads(_expression("${{ " + inner.group("body") + " }}", context)))


def _placement(context: dict[str, Any]) -> dict[int, str]:
    """Which runner each of the six shards gets in this case."""
    placed: dict[int, str] = {}
    for job in (_SHARD_JOB, _JOB, _SECOND_SITE):
        if _runs(job, context):
            for shard in _shard_matrix(job, context):
                assert shard not in placed, f"shard {shard} runs twice"
                placed[shard] = _runner(job, context)
    return placed


_POOL = dict.fromkeys(range(1, 7), _HEAVY)
_HOSTED_1_3 = {**_POOL, 1: "ubuntu-latest", 2: "ubuntu-latest", 3: "ubuntu-latest"}
_SITE2 = "meho-runners-ci-heavy-site2"


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (_context(), _POOL),
        (_context(pr_hosted="on"), _HOSTED_1_3),
        (_context(second_site="on"), {**_POOL, 4: _SITE2, 5: _SITE2, 6: _SITE2}),
        (
            _context(pr_hosted="on", second_site="on"),
            {**_HOSTED_1_3, 1: _HEAVY, 4: _SITE2, 5: _SITE2, 6: _SITE2},
        ),
        (_context(event="merge_group"), dict.fromkeys(range(1, 7), "meho-runners-ci-heavy-queue")),
        (_context(event="schedule"), _POOL),
        (_context(head="someone/fork"), {}),
        # "Re-run failed jobs" keeps the old `changes` output.
        (_context(attempt="2", pr_hosted="on"), _POOL),
    ],
    ids=[
        "switch-off",
        "switch-on",
        "site-on-switch-off",
        "switch-on-and-site-on",
        "merge-queue",
        "nightly",
        "fork",
        "re-run",
    ],
)
def test_each_shard_gets_its_runner(context: dict[str, Any], expected: dict[int, str]) -> None:
    assert _placement(context) == expected


_VERIFY = "Verify spec-shelf provisioned the pinned specs"
_SWITCH_STATES = [
    {"pr_hosted": "", "second_site": ""},
    {"pr_hosted": "", "second_site": "on"},
    {"pr_hosted": "on", "second_site": ""},
    {"pr_hosted": "on", "second_site": "on"},
]


@pytest.mark.parametrize(
    "state", _SWITCH_STATES, ids=lambda s: f"{s['pr_hosted']}-{s['second_site']}"
)
def test_the_pool_job_runs_the_spec_shelf_check_in_every_switch_state(
    state: dict[str, str],
) -> None:
    context = _context(**state)
    placement = _placement(context)
    assert sorted(placement) == [1, 2, 3, 4, 5, 6]
    assert _runs(_SHARD_JOB, context)
    pool_shards = _shard_matrix(_SHARD_JOB, context)
    assert pool_shards
    assert all(placement[shard] == _HEAVY for shard in pool_shards)
    # The verify step is only in python-unit-shard.
    jobs = _ci_jobs()
    holders = [
        name
        for name, job in jobs.items()
        if any(s.get("name") == _VERIFY for s in job.get("steps", []))
    ]
    assert _SHARD_JOB in holders
    assert not {_JOB, _SECOND_SITE} & set(holders)


@pytest.mark.parametrize("event", ["merge_group", "schedule", "push", "workflow_dispatch"])
def test_job_never_runs_outside_a_pull_request(event: str) -> None:
    assert not _runs(_JOB, _context(event=event, pr_hosted="on"))


def test_job_never_runs_for_a_fork() -> None:
    assert not _runs(_JOB, _context(head="someone/fork", pr_hosted="on"))


def test_job_runs_only_when_the_pick_says_on() -> None:
    assert _runs(_JOB, _context(pr_hosted="on"))
    for value in ("", "ON", "off", "true"):
        assert not _runs(_JOB, _context(pr_hosted=value))


@pytest.mark.parametrize(
    ("attempt", "label"), [("1", "ubuntu-latest"), ("2", _HEAVY), ("3", _HEAVY)]
)
def test_only_the_free_label_and_a_re_run_goes_home(attempt: str, label: str) -> None:
    assert _runner(_JOB, {"github.run_attempt": attempt}) == label
    labels = set(re.findall(r"'([^']+)'", _ci_jobs()[_JOB]["runs-on"])) - {"1"}
    assert labels == {"ubuntu-latest", _HEAVY}


def test_job_shape() -> None:
    jobs = _ci_jobs()
    job = jobs[_JOB]
    assert job["name"] == "Python unit shard ${{ matrix.shard }}/6"
    assert set(job["strategy"]["matrix"]) == {"shard"}
    assert _shard_matrix(_JOB, _context(pr_hosted="on")) == [1, 2, 3]
    assert _shard_matrix(_JOB, _context(pr_hosted="on", second_site="on")) == [2, 3]
    assert job["needs"] == "changes"
    assert job["permissions"] == {"contents": "read"}
    assert job["cache-mode"] == "read"
    assert "continue-on-error" not in job
    assert job["timeout-minutes"] == jobs[_SHARD_JOB]["timeout-minutes"]


def test_job_names_no_secret_environment_or_id_token() -> None:
    _assert_no_secret_environment_or_id_token(_without_comments(_job_text(_JOB)))
    job = _ci_jobs()[_JOB]
    # The job as GitHub reads it: with every alias filled in.
    _assert_no_secret_environment_or_id_token(yaml.safe_dump(job))
    assert "environment" not in job
    assert set(job["env"]) == {"MEHO_CONSUMER_DOCS_ROOT"}
    _assert_ci_outside_jobs_names_no_secret(_CI.read_text())


def test_a_secret_in_a_shared_step_is_found() -> None:
    text = _CI.read_text()
    marker = "      - &unit-helm\n        name: Install Helm\n"
    assert text.count(marker) == 1
    changed = text.replace(marker, marker + "        env:\n          T: ${{ secrets.X }}\n", 1)
    with pytest.raises(AssertionError):
        _assert_no_secret_environment_or_id_token(yaml.safe_dump(_ci_jobs(changed)[_JOB]))


def test_steps_are_aliases_of_the_second_site_steps() -> None:
    jobs = _ci_jobs()
    second_site = [s for s in jobs[_SECOND_SITE]["steps"] if s["name"] not in _LEFT_OUT]
    assert jobs[_JOB]["steps"] == second_site
    # Aliases, not copies: every step line of the job is `- *name`.
    lines = _without_comments(_job_text(_JOB)).splitlines()
    step_lines = [line for line in lines if line.startswith("      - ")]
    assert step_lines
    assert all(re.fullmatch(r"      - \*[a-z0-9-]+", line) for line in step_lines), step_lines
    uses = [str(s.get("uses", "")) for s in jobs[_JOB]["steps"]]
    assert not any(u.startswith(("actions/upload-artifact@", "actions/cache/save@")) for u in uses)


def test_the_trial_job_is_gone() -> None:
    assert "python-unit-shard-hosted-trial" not in _CI.read_text()


# --- The required check ------------------------------------------------------


def _fan_in(
    tmp_path: Path, *, pr_hosted: str, second_site: str, shards: str, site2: str, hosted: str
) -> subprocess.CompletedProcess[str]:
    script = _step("python-unit-lane", "Assert lint + typecheck + every pytest shard passed")["run"]
    values = {
        "IS_FORK_PR": "false",
        "CHANGES_RESULT": "success",
        "CHANGES_DOCS_ONLY": "false",
        "CHANGES_BACKEND_PYTHON": "true",
        "CHANGES_SINGLE_CONNECTOR": "",
        "LINT_RESULT": "success",
        "SHARDS_RESULT": shards,
        "CHANGES_SECOND_SITE": second_site,
        "SECOND_SITE_RESULT": site2,
        "CHANGES_PR_HOSTED": pr_hosted,
        "HOSTED_RESULT": hosted,
        "OUTSIDE_RESULT": "skipped",
    }
    return _bash_step(script, tmp_path, values)


@pytest.mark.parametrize(
    ("pr_hosted", "second_site", "shards", "site2", "hosted", "passes"),
    [
        # Switch on, site off: shards 4-6 and shards 1-3 must both pass.
        ("on", "", "success", "skipped", "success", True),
        ("on", "", "success", "skipped", "failure", False),
        ("on", "", "success", "skipped", "cancelled", False),
        ("on", "", "success", "skipped", "skipped", False),
        ("on", "", "failure", "skipped", "success", False),
        ("on", "", "skipped", "skipped", "success", False),
        ("on", "", "success", "success", "success", False),
        # Switch on, site on: python-unit-shard runs shard 1 and must pass.
        ("on", "on", "success", "success", "success", True),
        ("on", "on", "success", "failure", "success", False),
        ("on", "on", "success", "success", "failure", False),
        ("on", "on", "skipped", "success", "success", False),
        ("on", "on", "success", "skipped", "success", False),
        # Switch off: the hosted job must be skipped.
        ("", "", "success", "skipped", "skipped", True),
        ("", "", "success", "skipped", "success", False),
        ("", "", "success", "skipped", "failure", False),
        ("", "on", "success", "success", "skipped", True),
        ("", "on", "success", "success", "success", False),
    ],
)
def test_the_required_check_needs_all_six_shards(
    tmp_path: Path,
    pr_hosted: str,
    second_site: str,
    shards: str,
    site2: str,
    hosted: str,
    passes: bool,
) -> None:
    result = _fan_in(
        tmp_path,
        pr_hosted=pr_hosted,
        second_site=second_site,
        shards=shards,
        site2=site2,
        hosted=hosted,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


@pytest.mark.parametrize(
    "state", _SWITCH_STATES, ids=lambda s: f"{s['pr_hosted']}-{s['second_site']}"
)
@pytest.mark.parametrize("pool", ["skipped", "", "failure", "cancelled", "success"])
def test_the_required_check_needs_the_spec_shelf_job(
    tmp_path: Path, state: dict[str, str], pool: str
) -> None:
    result = _fan_in(
        tmp_path,
        pr_hosted=state["pr_hosted"],
        second_site=state["second_site"],
        shards=pool,
        site2="success" if state["second_site"] == "on" else "skipped",
        hosted="success" if state["pr_hosted"] == "on" else "skipped",
    )
    assert (result.returncode == 0) is (pool == "success"), result.stdout + result.stderr
    if pool != "success":
        assert "no job checked the pinned vendor API files" in result.stdout


def test_the_required_check_keeps_its_name_and_needs_the_hosted_job() -> None:
    lane = _ci_jobs()["python-unit-lane"]
    assert lane["name"] == "Python (ruff + mypy + pytest)"
    assert _JOB in lane["needs"]
    assert lane["if"] == "always()"


# --- The decision: pick_pr_hosted.sh ------------------------------------------


def _pick(tmp_path: Path, **overrides: str | None) -> tuple[str, str]:
    env: dict[str, str | None] = {
        "MEHO_CI_PR_HOSTED": "on",
        "EVENT_NAME": "pull_request",
        "HEAD_REPO": _REPO,
        "RUN_ATTEMPT": "1",
        "DOCS_ONLY": "false",
        "BACKEND_PYTHON": "true",
        "SINGLE_CONNECTOR": "",
        "TEST_MAP": "false",
        **overrides,
    }
    output = tmp_path / "output"
    output.write_text("")
    result = _run(
        _PICK,
        tmp_path,
        {"GITHUB_OUTPUT": str(output), **{k: v for k, v in env.items() if v is not None}},
    )
    assert result.returncode == 0, result.stderr
    return output.read_text(), result.stdout


def test_pick_on_when_every_rule_holds(tmp_path: Path) -> None:
    output, notice = _pick(tmp_path)
    assert output == "pr_hosted=on\n"
    assert "::notice title=Unit shards 1-3::" in notice


@pytest.mark.parametrize(
    "overrides",
    [
        {"MEHO_CI_PR_HOSTED": None},
        {"MEHO_CI_PR_HOSTED": ""},
        {"MEHO_CI_PR_HOSTED": "ON"},
        {"MEHO_CI_PR_HOSTED": "true"},
        {"EVENT_NAME": "merge_group"},
        {"EVENT_NAME": "schedule"},
        {"EVENT_NAME": None},
        {"HEAD_REPO": "someone/fork"},
        {"HEAD_REPO": None},
        {"RUN_ATTEMPT": "2"},
        {"RUN_ATTEMPT": None},
        {"DOCS_ONLY": "true"},
        {"DOCS_ONLY": None},
        {"BACKEND_PYTHON": "false"},
        {"SINGLE_CONNECTOR": "vault"},
        {"TEST_MAP": "true"},
    ],
)
def test_pick_keeps_today_s_pool(tmp_path: Path, overrides: dict[str, str | None]) -> None:
    output, notice = _pick(tmp_path, **overrides)
    assert output == "pr_hosted=\n"
    assert "today's pool" in notice


def test_pick_step_can_never_break_the_changes_job() -> None:
    changes = _ci_jobs()["changes"]
    step = _step("changes", "Pick the runners for unit shards 1-3")
    # The last step, so no step depends on it.
    assert step == changes["steps"][-1]
    assert step["id"] == "pr-hosted"
    assert step["env"]["TEST_MAP"] == changes["outputs"]["test_map"]
    assert step["env"]["RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] == 2
    assert step["run"] == "bash scripts/ci/pick_pr_hosted.sh"
    assert step["env"]["MEHO_CI_PR_HOSTED"] == "${{ vars.MEHO_CI_PR_HOSTED }}"
    assert changes["outputs"]["pr_hosted"] == "${{ steps.pr-hosted.outputs.pr_hosted }}"


# --- A change of only the pick script runs these tests ------------------------


def test_a_change_of_only_the_pick_script_runs_these_tests(tmp_path: Path) -> None:
    outputs = _detect(tmp_path, "pull_request", ["scripts/ci/pick_pr_hosted.sh"])
    # No backend change: only python-outside-backend runs, with this list.
    assert outputs["backend_python"] == "false"
    this_file = "tests/test_ci_pr_hosted_unit_shards.py"
    assert this_file in outside.OUTSIDE_BACKEND_TESTS
    assert this_file in outside.scan()
