# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the second runner pool in CI (#6026).

CI can send unit test shards 4-6 to a second pool of self-hosted runners.
The most important promise is that nothing changes while the pool is off,
missing or down. These tests check that promise, and the failing cases of
each part:

* which runner label a unit shard gets (the real ``runs-on`` expression);
* ``scripts/ci/pick_extra_runner_pool.sh``, which decides if the pool is used;
* ``scripts/ci/extra_pool_watchdog.sh``, which stops a run that waits on a
  dead pool;
* ``scripts/ci/extra_runner_smoke.sh``, the check that runs on the pool;
* ``scripts/ci/use_public_test_images.sh``, the public image names.

The scripts run for real in bash, with ``gh``, ``sleep`` and ``timeout``
replaced by small fakes on ``PATH``. ``jq`` is real, so the tests skip where
it is missing.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests._pgvector_image import chart_postgres_image

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCRIPTS = _REPO_ROOT / "scripts" / "ci"

_PICK = _SCRIPTS / "pick_extra_runner_pool.sh"
_WATCHDOG = _SCRIPTS / "extra_pool_watchdog.sh"
_SMOKE = _SCRIPTS / "extra_runner_smoke.sh"
_IMAGES = _SCRIPTS / "use_public_test_images.sh"

_POOL = "meho-runners-ci-heavy-extra"
_HEAVY = "meho-runners-ci-heavy"
_QUEUE = "meho-runners-ci-heavy-queue"
_REPO = "example-org/example-repo"
_IMAGE_STEP = "Use public test images on the second runner pool"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the scripts need bash and jq",
)


def _workflow(name: str) -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load((_WORKFLOWS / name).read_text()))


def _ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", _workflow("ci.yml")["jobs"])


def _fake(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    script: Path, tmp_path: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": _REPO,
        **env,
    }
    return subprocess.run(
        ["bash", str(script), *args],
        env=full_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# --- Which runner a unit shard gets -------------------------------------------


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    assert isinstance(node, ast.Name), f"unexpected node {node!r}"
    return node.id


def _evaluate(node: ast.expr, context: dict[str, Any]) -> Any:
    """Work out the expression the way GitHub does.

    ``&&`` and ``||`` return the operand that decides, so an empty string
    falls through to the part after ``||``.
    """
    if isinstance(node, ast.BoolOp):
        value: Any = None
        for operand in node.values:
            value = _evaluate(operand, context)
            if isinstance(node.op, ast.And) and not value:
                return value
            if isinstance(node.op, ast.Or) and value:
                return value
        return value
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, context)
        right = _evaluate(node.comparators[0], context)
        operator = node.ops[0]
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.GtE):
            return left >= right
        raise AssertionError(f"unexpected operator {operator!r}")
    if isinstance(node, ast.Constant):
        return node.value
    return context[_dotted(node)]


def _runner_label(event: str, shard: int, extra: str, attempt: str = "1") -> str:
    runs_on = _ci_jobs()["python-unit-shard"]["runs-on"]
    assert runs_on.startswith("${{") and runs_on.endswith("}}")
    source = runs_on[3:-2].replace("&&", " and ").replace("||", " or ")
    context = {
        "github.event_name": event,
        "github.run_attempt": attempt,
        "matrix.shard": shard,
        "needs.changes.outputs.unit_extra_runner": extra,
    }
    return cast("str", _evaluate(ast.parse(source.strip(), mode="eval").body, context))


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule"])
@pytest.mark.parametrize("shard", [1, 2, 3, 4, 5, 6])
def test_no_pool_named_means_todays_runners(event: str, shard: int) -> None:
    assert _runner_label(event, shard, extra="") == _HEAVY


@pytest.mark.parametrize("extra", ["", _POOL])
@pytest.mark.parametrize("shard", [1, 2, 3, 4, 5, 6])
def test_merge_queue_keeps_its_runners(extra: str, shard: int) -> None:
    assert _runner_label("merge_group", shard, extra=extra) == _QUEUE


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule"])
def test_only_shards_4_to_6_use_the_second_pool(event: str) -> None:
    labels = {shard: _runner_label(event, shard, extra=_POOL) for shard in range(1, 7)}
    assert labels == {1: _HEAVY, 2: _HEAVY, 3: _HEAVY, 4: _POOL, 5: _POOL, 6: _POOL}


@pytest.mark.parametrize("shard", [4, 5, 6])
@pytest.mark.parametrize("attempt", ["2", "3"])
def test_a_rerun_always_uses_todays_runners(shard: int, attempt: str) -> None:
    assert _runner_label("pull_request", shard, extra=_POOL, attempt=attempt) == _HEAVY


def test_required_check_names_did_not_change() -> None:
    jobs = _ci_jobs()
    assert jobs["python-unit-lane"]["name"] == "Python (ruff + mypy + pytest)"
    assert jobs["python-integration"]["name"] == "Python (integration testcontainers)"
    assert jobs["python-unit-lane"]["needs"] == [
        "changes",
        "python-lint",
        "python-unit-shard",
        "python-outside-backend",
    ]
    assert "format('Python unit shard {0}/6', matrix.shard)" in jobs["python-unit-shard"]["name"]


def test_the_shard_job_still_skips_on_a_fork_pull_request() -> None:
    job_if = _ci_jobs()["python-unit-shard"]["if"]
    assert "head.repo.full_name == github.repository" in job_if


def test_second_pool_label_is_only_named_where_it_may_run() -> None:
    """In ci.yml the label comes only from the `changes` output."""
    for name, job in _ci_jobs().items():
        assert _POOL not in str(job.get("runs-on", "")), name
    assert _workflow("extra-runner-smoke.yml")["jobs"]["smoke"]["runs-on"] == _POOL
    assert "unit_extra_runner" in str(_ci_jobs()["python-unit-shard"]["runs-on"])


def test_pool_step_can_never_break_the_changes_job() -> None:
    changes = _ci_jobs()["changes"]
    (step,) = [s for s in changes["steps"] if s.get("id") == "pool"]
    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] <= 2
    assert "github.event_name != 'merge_group'" in step["if"]
    # The token only needs to read the run history, which the job could already.
    assert changes["permissions"] == {"contents": "read", "actions": "read"}
    assert changes["outputs"]["unit_extra_runner"] == "${{ steps.pool.outputs.runner }}"


def test_watchdog_runs_only_when_the_pool_was_picked() -> None:
    job = _ci_jobs()["extra-pool-watchdog"]
    assert "needs.changes.outputs.unit_extra_runner != ''" in job["if"]
    assert "github.run_attempt == '1'" in job["if"]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read", "actions": "write"}


# --- The decision: pick_extra_runner_pool.sh -----------------------------------


def _iso(age_seconds: int) -> str:
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _smoke_run(age_seconds: int, conclusion: str = "success", head: str = _REPO) -> dict[str, Any]:
    return {
        "conclusion": conclusion,
        "run_started_at": _iso(age_seconds),
        "head_repository": {"full_name": head},
        "repository": {"full_name": _REPO},
    }


def _pick(
    tmp_path: Path,
    *,
    runs: list[dict[str, Any]] | None = None,
    gh_body: str | None = None,
    switch: str | None = "on",
    event: str = "pull_request",
    head: str = _REPO,
    actor: str = "someone",
) -> tuple[str, subprocess.CompletedProcess[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"workflow_runs": runs or []}))
    calls = tmp_path / "calls.log"
    body = gh_body if gh_body is not None else f'echo "$*" >> "{calls}"\ncat "{answer}"'
    _fake(bin_dir, "gh", body)
    output = tmp_path / "output.txt"
    env = {
        "GITHUB_OUTPUT": str(output),
        "EVENT_NAME": event,
        "HEAD_REPO": head,
        "GITHUB_ACTOR": actor,
    }
    if switch is not None:
        env["MEHO_CI_EXTRA_POOL"] = switch
    result = _run(_PICK, tmp_path, env)
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("runner=")
    return lines[0].removeprefix("runner="), result


def test_pick_uses_the_pool_after_a_recent_smoke_pass(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(600)])
    assert runner == _POOL
    calls = (tmp_path / "calls.log").read_text()
    assert "extra-runner-smoke.yml/runs?branch=main&status=success" in calls


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule"])
def test_pick_allows_the_normal_events(tmp_path: Path, event: str) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(60)], event=event)
    assert runner == _POOL


@pytest.mark.parametrize("switch", [None, "", "off", "true", "ON"])
def test_pick_needs_the_switch_on(tmp_path: Path, switch: str | None) -> None:
    runner, result = _pick(tmp_path, runs=[_smoke_run(60)], switch=switch)
    assert runner == ""
    assert "not switched on" in result.stdout


def test_pick_never_uses_the_pool_for_a_fork(tmp_path: Path) -> None:
    runner, result = _pick(tmp_path, runs=[_smoke_run(60)], head="stranger/fork")
    assert runner == ""
    assert "fork" in result.stdout


def test_pick_keeps_the_merge_queue_off_the_pool(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(60)], event="merge_group")
    assert runner == ""


def test_pick_keeps_dependabot_off_the_pool(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(60)], actor="dependabot[bot]")
    assert runner == ""


def test_pick_counts_a_pass_just_inside_the_limit(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(45 * 60 - 120)])
    assert runner == _POOL


def test_pick_treats_an_old_pass_as_offline(tmp_path: Path) -> None:
    runner, result = _pick(tmp_path, runs=[_smoke_run(45 * 60 + 120)])
    assert runner == ""
    assert "old" in result.stdout


def test_pick_with_no_pass_at_all_is_offline(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[])
    assert runner == ""


def test_pick_uses_the_newest_pass_in_any_order(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(9000), _smoke_run(60), _smoke_run(5000)])
    assert runner == _POOL


def test_pick_ignores_a_run_that_did_not_pass(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(60, conclusion="failure")])
    assert runner == ""


def test_pick_ignores_a_run_from_another_repository(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(60, head="stranger/fork")])
    assert runner == ""


def test_pick_treats_a_future_time_as_unknown(tmp_path: Path) -> None:
    runner, _ = _pick(tmp_path, runs=[_smoke_run(-7200)])
    assert runner == ""


def test_pick_stays_safe_when_github_does_not_answer(tmp_path: Path) -> None:
    runner, result = _pick(tmp_path, gh_body="exit 1")
    assert runner == ""
    assert "could not read" in result.stdout


@pytest.mark.parametrize(
    "body",
    ["echo not-json", "echo '{}'", """echo '{"workflow_runs": [{"conclusion": "success"}]}'"""],
)
def test_pick_stays_safe_on_an_odd_answer(tmp_path: Path, body: str) -> None:
    runner, _ = _pick(tmp_path, gh_body=body)
    assert runner == ""


def test_pick_stays_safe_on_an_unreadable_time(tmp_path: Path) -> None:
    run = _smoke_run(60)
    run["run_started_at"] = "soon"
    runner, _ = _pick(tmp_path, runs=[run])
    assert runner == ""


# --- The watchdog: extra_pool_watchdog.sh --------------------------------------


def _shards(**statuses: str) -> str:
    """An answer of the jobs API. Keys are like ``s4``; other jobs are added."""
    jobs = [{"name": "Detect migration + docs-only changes", "status": "completed"}]
    for key, status in statuses.items():
        jobs.append({"name": f"Python unit shard {key[1:]}/6", "status": status})
    return json.dumps({"jobs": jobs})


def _watch(
    tmp_path: Path, answers: list[str], cancel_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the watchdog. ``gh api`` gives ``answers`` in turn; the last repeats."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    answer_dir = tmp_path / "answers"
    answer_dir.mkdir()
    for index, answer in enumerate(answers):
        (answer_dir / str(index)).write_text(answer)
    calls = tmp_path / "calls.log"
    count = tmp_path / "count"
    count.write_text("0")
    last = len(answers) - 1
    _fake(
        bin_dir,
        "gh",
        f"""echo "$*" >> "{calls}"
if [ "$1" = run ]; then exit {cancel_exit}; fi
n=$(cat "{count}"); echo $((n + 1)) > "{count}"
if [ "$n" -gt {last} ]; then n={last}; fi
if [ "$(cat "{answer_dir}/$n")" = FAIL ]; then exit 1; fi
cat "{answer_dir}/$n"
""",
    )
    _fake(bin_dir, "sleep", "exit 0")
    result = _run(
        _WATCHDOG, tmp_path, {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "x"}
    )
    return result, calls.read_text().splitlines()


def _cancels(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("run cancel")]


def test_watchdog_ends_when_every_shard_has_a_runner(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_shards(s4="in_progress", s5="in_progress", s6="completed")])
    assert result.returncode == 0
    assert _cancels(calls) == []
    assert len(calls) == 1


def test_watchdog_stops_a_run_that_waits_too_long(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_shards(s4="queued", s5="in_progress", s6="in_progress")])
    assert result.returncode == 0
    assert _cancels(calls) == [f"run cancel 42 --repo {_REPO}"]
    assert "1 unit shard(s) waited 30 minutes" in result.stdout
    assert "Re-run" in result.stdout


def test_watchdog_waits_for_a_shard_that_starts_late(tmp_path: Path) -> None:
    waiting = _shards(s4="queued", s5="queued", s6="queued")
    started = _shards(s4="in_progress", s5="in_progress", s6="in_progress")
    result, calls = _watch(tmp_path, [waiting, waiting, started])
    assert result.returncode == 0
    assert _cancels(calls) == []


@pytest.mark.parametrize("status", ["pending", "requested", "waiting"])
def test_watchdog_treats_other_statuses_as_waiting(tmp_path: Path, status: str) -> None:
    result, calls = _watch(tmp_path, [_shards(s4=status, s5="in_progress", s6="completed")])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1


def test_watchdog_never_cancels_when_it_cannot_read_the_jobs(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, ["FAIL"])
    assert result.returncode == 0
    assert _cancels(calls) == []
    assert "no shard is known to be stuck" in result.stdout


def test_watchdog_never_cancels_on_an_unreadable_answer(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, ["not json"])
    assert result.returncode == 0
    assert _cancels(calls) == []


def test_watchdog_never_cancels_while_a_shard_is_not_listed(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_shards(s4="queued", s5="queued")])
    assert result.returncode == 0
    assert _cancels(calls) == []


def test_watchdog_uses_the_last_good_answer_after_a_read_failure(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_shards(s4="queued", s5="queued", s6="queued"), "FAIL"])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1
    assert "3 unit shard(s) waited" in result.stdout


def test_watchdog_reports_when_it_cannot_stop_the_run(tmp_path: Path) -> None:
    result, calls = _watch(
        tmp_path, [_shards(s4="queued", s5="queued", s6="queued")], cancel_exit=1
    )
    assert result.returncode == 1
    assert len(_cancels(calls)) == 3
    assert "Could not stop this run" in result.stdout


def test_watchdog_reads_only_this_attempt(tmp_path: Path) -> None:
    _, calls = _watch(tmp_path, [_shards(s4="completed", s5="completed", s6="completed")])
    assert calls[0] == f"api repos/{_REPO}/actions/runs/42/attempts/1/jobs?per_page=100"


# --- The smoke check: extra_runner_smoke.sh -------------------------------------

_REACH = "https://reach.example"
_ADDRESS = "secret.example"
_ADDRESS_URL = f"https://{_ADDRESS}/"


def _smoke(
    tmp_path: Path,
    deny: str | None,
    *reach: str,
    site: str = "extra",
    exits: dict[str, int] | None = None,
    connected: str = "0.000000",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the smoke check with a fake curl.

    ``exits`` gives the exit code for a URL. By default a reach check works
    and a deny check times out.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "curl.log"
    rules = "\n".join(f'  "{url}") code={code} ;;' for url, code in (exits or {}).items())
    _fake(
        bin_dir,
        "curl",
        f"""echo "$*" >> "{calls}"
for url; do :; done
code=0
if [[ "$*" == *--write-out* ]]; then code=28; printf '%s' '{connected}'; fi
case "$url" in
{rules}
esac
exit "$code"
""",
    )
    env = {"MEHO_CI_SITE": site}
    if deny is not None:
        env["MEHO_CI_DENY_PROBE_HOSTS"] = deny
    result = _run(_SMOKE, tmp_path, env, *reach)
    return result, calls.read_text().splitlines() if calls.exists() else []


def _assert_private(result: subprocess.CompletedProcess[str], *secrets: str) -> None:
    """The ``::add-mask::`` lines are the only ones that may hold an address.

    The runner reads those as a command and hides the value in the log.
    """
    text = "\n".join(
        line
        for line in (result.stdout + result.stderr).splitlines()
        if not line.startswith("::add-mask::")
    )
    for secret in secrets:
        assert secret not in text, f"{secret!r} was printed"


def test_smoke_passes_when_every_address_times_out(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, f"{_ADDRESS},second.example:8443", _REACH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "address 1 of 2: exit code 28" in result.stdout
    assert "address 2 of 2: exit code 28" in result.stdout
    assert "passed" in result.stdout
    _assert_private(result, _ADDRESS, "second.example")


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(0, id="success"),
        pytest.param(7, id="refused"),
        pytest.param(60, id="certificate-error"),
        pytest.param(35, id="tls-error"),
        pytest.param(6, id="name-not-found"),
        pytest.param(52, id="empty-reply"),
    ],
)
def test_smoke_fails_on_anything_but_a_timeout(tmp_path: Path, code: int) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, exits={_ADDRESS_URL: code})
    assert result.returncode == 1
    assert f"Address 1 of 1: exit code {code}" in result.stdout
    assert "passed" not in result.stdout
    _assert_private(result, _ADDRESS)


def test_smoke_fails_on_a_timeout_after_the_connection_was_made(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, connected="0.031000")
    assert result.returncode == 1
    assert "Address 1 of 1: exit code 28" in result.stdout
    _assert_private(result, _ADDRESS)


def test_smoke_checks_every_address(tmp_path: Path) -> None:
    deny = f"{_ADDRESS}, second.example:8443 ,third.example"
    result, calls = _smoke(tmp_path, deny, _REACH, exits={"https://second.example:8443/": 7})
    assert result.returncode == 1
    assert "address 1 of 3: exit code 28" in result.stdout
    assert "Address 2 of 3: exit code 7" in result.stdout
    assert "address 3 of 3: exit code 28" in result.stdout
    assert len(calls) == 4, "one reach check and three deny checks"
    _assert_private(result, _ADDRESS, "second.example", "third.example")


def test_smoke_masks_each_address_before_it_is_used(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, f"{_ADDRESS},second.example:8443", _REACH)
    lines = result.stdout.splitlines()
    first_probe = next(i for i, line in enumerate(lines) if line.startswith("reached"))
    assert f"::add-mask::{_ADDRESS}" in lines[:first_probe]
    assert "::add-mask::second.example:8443" in lines[:first_probe]
    assert "::add-mask::second.example" in lines[:first_probe]


def test_smoke_deny_probe_has_the_right_flags(tmp_path: Path) -> None:
    _, calls = _smoke(tmp_path, _ADDRESS, _REACH)
    deny_call = calls[-1]
    assert deny_call.endswith(_ADDRESS_URL)
    assert "--connect-timeout 5" in deny_call
    assert "--noproxy *" in deny_call
    for call in calls:
        assert "--insecure" not in call
        assert " -k " not in call


@pytest.mark.parametrize("deny", [None, "", "   ", ",", " , ,"])
def test_smoke_fails_when_the_list_is_empty(tmp_path: Path, deny: str | None) -> None:
    result, calls = _smoke(tmp_path, deny, _REACH)
    assert result.returncode == 1
    assert "is empty" in result.stdout
    assert calls == []


def test_smoke_fails_on_the_wrong_site(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, site="other")
    assert result.returncode == 1
    assert "MEHO_CI_SITE" in result.stdout


def test_smoke_fails_without_a_url_to_reach(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS)
    assert result.returncode == 1
    assert calls == []


def test_smoke_fails_when_a_needed_url_is_out_of_reach(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, exits={_REACH: 28})
    assert result.returncode == 1
    assert f"Cannot reach {_REACH} (curl exit code 28)" in result.stdout
    assert len(calls) == 1, "no deny check runs when a reach check fails"
    _assert_private(result, _ADDRESS)


@pytest.mark.parametrize(
    "entry",
    ["bad/value", "bad value!", "host:notaport", "$(touch /tmp/x)"],
)
def test_smoke_refuses_an_entry_of_the_wrong_shape(tmp_path: Path, entry: str) -> None:
    result, calls = _smoke(tmp_path, entry, _REACH)
    assert result.returncode == 1
    assert "Entry 1 of MEHO_CI_DENY_PROBE_HOSTS" in result.stdout
    assert calls == []
    _assert_private(result, entry)


def test_smoke_puts_an_ipv6_address_in_brackets(tmp_path: Path) -> None:
    deny = "2001:db8::1"
    result, calls = _smoke(tmp_path, deny, _REACH)
    assert result.returncode == 0, result.stdout
    assert calls[-1].endswith(f"https://[{deny}]/")
    _assert_private(result, deny)


def test_smoke_workflow_is_safe_by_construction() -> None:
    workflow = _workflow("extra-runner-smoke.yml")
    # PyYAML reads the bare `on:` key as the boolean True.
    assert set(workflow[True]) == {"workflow_dispatch", "schedule"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["smoke"]
    assert "vars.MEHO_CI_EXTRA_POOL == 'on'" in job["if"]
    assert "env" not in job, "a private value must stay on the pod, not in the log"
    for step in job["steps"]:
        assert "env" not in step, "the runner prints a step's env block in the public log"
    assert job["steps"][-1]["run"].split()[2] == "https://github.com"


# --- The public image names: use_public_test_images.sh ------------------------


def _images(tmp_path: Path, env: dict[str, str]) -> tuple[dict[str, str], str, int]:
    github_env = tmp_path / "github_env"
    github_env.touch()
    result = _run(_IMAGES, tmp_path, {"GITHUB_ENV": str(github_env), **env})
    written = dict(
        line.split("=", 1) for line in github_env.read_text().splitlines() if line.strip()
    )
    return written, result.stdout, result.returncode


# Every job with a MEHO_TEST_*_IMAGE line. Only the jobs that can run on the
# second pool have the step; the name rule is checked on all of these lines.
_IMAGE_JOBS = [
    name
    for name, job in yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())["jobs"].items()
    if any(
        str(key).startswith("MEHO_TEST_") and str(key).endswith("_IMAGE")
        for key in job.get("env", {})
    )
]


def test_image_jobs_are_the_ones_we_expect() -> None:
    assert set(_IMAGE_JOBS) == {
        "python-unit-shard",
        "python-coverage",
        "python-integration-shard",
        "python-migration-tests",
    }


def test_image_step_comes_right_after_checkout() -> None:
    steps = _ci_jobs()["python-unit-shard"]["steps"]
    assert steps[0]["name"] == "Checkout"
    assert steps[1]["name"] == _IMAGE_STEP
    assert steps[1]["run"] == "bash scripts/ci/use_public_test_images.sh"


def test_image_step_is_only_in_the_job_that_can_use_the_pool() -> None:
    with_step = [
        name
        for name, job in _ci_jobs().items()
        if _IMAGE_STEP in [step.get("name") for step in job.get("steps", [])]
    ]
    assert with_step == ["python-unit-shard"]


def test_login_step_still_skips_on_an_empty_username() -> None:
    steps = _ci_jobs()["python-unit-shard"]["steps"]
    names = [step.get("name") for step in steps]
    login = names.index("Docker Hub login (for testcontainers pulls)")
    assert steps[login]["if"] == "env.DOCKERHUB_USERNAME != ''"
    assert names.index(_IMAGE_STEP) < login


@pytest.mark.parametrize("job", _IMAGE_JOBS)
def test_images_do_not_change_on_todays_pools(tmp_path: Path, job: str) -> None:
    env = {k: str(v) for k, v in _ci_jobs()[job]["env"].items() if k.startswith("MEHO_TEST_")}
    written, stdout, code = _images(tmp_path, env)
    assert code == 0
    assert written == {}, "nothing may change on today's pools, also not the login"
    assert "Not the second runner pool" in stdout


@pytest.mark.parametrize("site", ["", "other", "EXTRA"])
def test_images_do_not_change_for_another_site(tmp_path: Path, site: str) -> None:
    env = {"MEHO_CI_SITE": site, "MEHO_TEST_X_IMAGE": "mirror.example/project/org/app:1"}
    written, _, code = _images(tmp_path, env)
    assert code == 0
    assert written == {}


@pytest.mark.parametrize("job", _IMAGE_JOBS)
def test_real_image_lines_become_public_names(tmp_path: Path, job: str) -> None:
    images = {
        k: str(v)
        for k, v in _ci_jobs()[job]["env"].items()
        if k.startswith("MEHO_TEST_") and k.endswith("_IMAGE")
    }
    written, _, code = _images(tmp_path, {"MEHO_CI_SITE": "extra", **images})
    assert code == 0
    assert set(written) == set(images) | {"DOCKERHUB_USERNAME"}
    for name, value in images.items():
        registry = value.split("/", 1)[0]
        assert "." in registry, f"{name} is expected to name a registry today"
        assert written[name].startswith("docker.io/"), name
        assert registry not in written[name], name
        assert value.endswith("/" + written[name].removeprefix("docker.io/")), name


def test_keycloak_gets_a_public_name(tmp_path: Path) -> None:
    images = {
        k: str(v)
        for k, v in _ci_jobs()["python-integration-shard"]["env"].items()
        if k.startswith("MEHO_TEST_") and k.endswith("_IMAGE")
    }
    assert "MEHO_TEST_KEYCLOAK_IMAGE" in images
    written, _, code = _images(tmp_path, {"MEHO_CI_SITE": "extra", **images})
    assert code == 0
    assert written["MEHO_TEST_KEYCLOAK_IMAGE"].startswith("docker.io/keycloak/keycloak:")


def test_pgvector_keeps_the_pin_the_chart_test_expects(tmp_path: Path) -> None:
    images = {
        k: str(v)
        for k, v in _ci_jobs()["python-unit-shard"]["env"].items()
        if k == "MEHO_TEST_PGVECTOR_IMAGE"
    }
    written, _, code = _images(tmp_path, {"MEHO_CI_SITE": "extra", **images})
    assert code == 0
    assert written["MEHO_TEST_PGVECTOR_IMAGE"].endswith(f"/{chart_postgres_image()}")


def test_the_docker_hub_login_is_skipped_on_the_second_pool(tmp_path: Path) -> None:
    written, stdout, code = _images(tmp_path, {"MEHO_CI_SITE": "extra"})
    assert code == 0
    assert written == {"DOCKERHUB_USERNAME": ""}
    assert "login step is skipped" in stdout


def test_a_digest_pin_survives(tmp_path: Path) -> None:
    env = {
        "MEHO_CI_SITE": "extra",
        "MEHO_TEST_X_IMAGE": "mirror.example/project/org/app@sha256:abc123",
    }
    written, _, code = _images(tmp_path, env)
    assert code == 0
    assert written["MEHO_TEST_X_IMAGE"] == "docker.io/org/app@sha256:abc123"


@pytest.mark.parametrize("value", ["org/app:1", "docker.io/org/app:1"])
def test_a_public_name_is_left_alone(tmp_path: Path, value: str) -> None:
    env = {"MEHO_CI_SITE": "extra", "MEHO_TEST_X_IMAGE": value}
    written, stdout, code = _images(tmp_path, env)
    assert code == 0
    assert "MEHO_TEST_X_IMAGE" not in written
    assert "already a public name" in stdout


def test_a_private_name_without_a_project_fails(tmp_path: Path) -> None:
    env = {"MEHO_CI_SITE": "extra", "MEHO_TEST_X_IMAGE": "mirror.example/app:1"}
    written, stdout, code = _images(tmp_path, env)
    assert code == 1
    assert "MEHO_TEST_X_IMAGE" not in written
    assert "no project part" in stdout


def test_other_variables_are_not_touched(tmp_path: Path) -> None:
    env = {
        "MEHO_CI_SITE": "extra",
        "MEHO_TEST_IMAGE_NOTE": "mirror.example/project/org/app:1",
        "OTHER_IMAGE": "mirror.example/project/org/app:1",
    }
    written, _, code = _images(tmp_path, env)
    assert code == 0
    assert written == {"DOCKERHUB_USERNAME": ""}
