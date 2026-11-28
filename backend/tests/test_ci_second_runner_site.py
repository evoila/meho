# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the second runner site in CI (#7085).

Unit test shards 4-6 of a pull request can run on a second runner site, in
the job ``python-unit-shard-second-site``. The most important promises:

* nothing changes while the site is off, missing or down;
* a job on the site names no secret, no ``environment`` and no ``id-token``;
* only two places name the site's label;
* the test steps of the two shard jobs are the same.

These tests check those promises, and the failing cases of each part:

* ``scripts/ci/pick_second_site.sh``, which decides if the site is used;
* the shard matrix split, the ``runs-on`` of the new job and the fan-in;
* ``scripts/ci/second_site_watchdog.sh``, which stops a run that waits on a
  dead site;
* ``scripts/ci/second_site_smoke.sh``, the check that runs on the site.

The scripts run for real in bash, with ``gh``, ``curl``, ``docker`` and
``sleep`` replaced by small fakes on ``PATH``. ``jq`` is real, so the tests
skip where it is missing.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCRIPTS = _REPO_ROOT / "scripts" / "ci"
_CI = _WORKFLOWS / "ci.yml"
_SMOKE_WORKFLOW = "second-site-smoke.yml"
_SMOKE_JOB = "Smoke check of the second runner site"

_PICK = _SCRIPTS / "pick_second_site.sh"
_WATCHDOG = _SCRIPTS / "second_site_watchdog.sh"
_SMOKE = _SCRIPTS / "second_site_smoke.sh"

_SITE_LABEL = "meho-runners-ci-heavy-site2"
_HEAVY = "meho-runners-ci-heavy"
_NEW_JOB = "python-unit-shard-second-site"
_REPO = "example-org/example-repo"

# The steps of python-unit-shard that read a secret or save a cache, and the
# steps that differ in the new job. Every other step must be the same in both
# jobs.
_SECRET_STEPS = {
    "Read the CI repository secrets",
    "List the required spec-shelf files",
    # YAML reads " #2949)" in this name as a comment, so the name ends at the comma.
    "Checkout vendor spec-shelf (secret-gated,",
    "Verify spec-shelf provisioned the pinned specs",
    "Docker Hub login (for testcontainers pulls)",
    "Embedding-model drift guard (evoila/meho#574)",
    "Hide the proxy name in the test reports",
}
# These save a cache: the new job restores caches and never saves one.
_CACHE_SAVING_STEPS = {
    "Install uv",
    "Cache fastembed model (drift-guard + slow-lane)",
    "Restore the spaCy model from the cache",
    "Save the PHP package files to the cache",
    "Save the GBK locale package files to the cache",
}
_NORMAL_ONLY_STEPS = _SECRET_STEPS | _CACHE_SAVING_STEPS
_SECOND_SITE_ONLY_STEPS = {
    "Check that this runner is on the second site",
    "Use the public test images",
    "Embedding-model drift guard (evoila/meho#574)",
    "Install uv",
    "Restore the fastembed model from the cache",
    "Restore the spaCy model from the cache",
    "Hide the proxy name in the test reports",
}
_HIDE_STEP = "Hide the proxy name in the test reports"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the scripts need bash and jq",
)


def _workflow(name: str) -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load((_WORKFLOWS / name).read_text()))


def _ci_jobs(text: str | None = None) -> dict[str, Any]:
    workflow = yaml.safe_load(text) if text is not None else _workflow("ci.yml")
    return cast("dict[str, Any]", workflow["jobs"])


def _job_text(name: str) -> str:
    """The raw text of one job of ci.yml, as written in the file."""
    text = _CI.read_text()
    start = text.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    end = start + 1 + following.start() if following else len(text)
    return text[start:end]


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


def _bash_step(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a `run:` script the way the runner starts it."""
    path = cwd / "step.sh"
    path.write_text(script)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


# --- Expressions: the matrix split and runs-on ---------------------------------


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
        if isinstance(operator, ast.NotEq):
            return left != right
        raise AssertionError(f"unexpected operator {operator!r}")
    if isinstance(node, ast.Constant):
        return node.value
    return context[_dotted(node)]


def _expression(text: str, context: dict[str, Any]) -> Any:
    text = text.strip()
    assert text.startswith("${{") and text.endswith("}}"), text
    source = text[3:-2].strip()
    source = source.replace("&&", " and ").replace("||", " or ")
    return _evaluate(ast.parse(source, mode="eval").body, context)


def _outputs(
    second_site: str = "", single_connector: str = "", test_map: str = "false"
) -> dict[str, str]:
    return {
        "needs.changes.outputs.second_site": second_site,
        "needs.changes.outputs.single_connector": single_connector,
        "needs.changes.outputs.test_map": test_map,
    }


def _shard_matrix(**outputs: str) -> list[int]:
    text = str(_ci_jobs()["python-unit-shard"]["strategy"]["matrix"]["shard"])
    inner = re.fullmatch(r"\$\{\{ fromJSON\((?P<body>.*)\) \}\}", text)
    assert inner is not None, text
    value = _expression("${{ " + inner.group("body") + " }}", _outputs(**outputs))
    return cast("list[int]", json.loads(value))


def test_matrix_split_when_the_site_is_on() -> None:
    assert _shard_matrix(second_site="on") == [1, 2, 3]
    assert _ci_jobs()[_NEW_JOB]["strategy"]["matrix"]["shard"] == [4, 5, 6]


def test_matrix_keeps_six_shards_when_the_site_is_off() -> None:
    assert _shard_matrix(second_site="") == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    "outputs",
    [{"single_connector": "vault"}, {"test_map": "true"}],
    ids=["one-connector", "test-map"],
)
def test_matrix_one_leg_runs_stay_as_today(outputs: dict[str, str]) -> None:
    assert _shard_matrix(**outputs) == [1]


def test_the_normal_shard_job_keeps_its_runner_line() -> None:
    assert _ci_jobs()["python-unit-shard"]["runs-on"] == (
        "${{ github.event_name == 'merge_group' && 'meho-runners-ci-heavy-queue' "
        "|| 'meho-runners-ci-heavy' }}"
    )


@pytest.mark.parametrize(("attempt", "label"), [("1", _SITE_LABEL), ("2", _HEAVY), ("3", _HEAVY)])
def test_new_job_uses_the_site_only_on_the_first_attempt(attempt: str, label: str) -> None:
    runs_on = _ci_jobs()[_NEW_JOB]["runs-on"]
    assert _expression(runs_on, {"github.run_attempt": attempt}) == label


def test_new_job_runs_only_for_a_pull_request_from_this_repository() -> None:
    job = _ci_jobs()[_NEW_JOB]
    condition = " ".join(job["if"].split())
    assert condition == (
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "needs.changes.outputs.second_site == 'on'"
    )
    assert job["needs"] == "changes"
    assert job["name"] == "Python unit shard ${{ matrix.shard }}/6"


def test_required_check_names_did_not_change() -> None:
    jobs = _ci_jobs()
    assert jobs["python-unit-lane"]["name"] == "Python (ruff + mypy + pytest)"
    assert jobs["python-integration"]["name"] == "Python (integration testcontainers)"
    assert "format('Python unit shard {0}/6', matrix.shard)" in jobs["python-unit-shard"]["name"]


# --- No secret on the second site ------------------------------------------------


# The `secrets` context: `secrets.X`, `secrets['X']`, `toJSON(secrets)`. Not
# part of a longer word such as `repo_secrets.sh` or `REQUIRE_SECRETS`.
_SECRETS_CONTEXT = re.compile(r"(?<![\w./-])secrets\b", re.IGNORECASE)


def _without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _assert_no_secret_environment_or_id_token(text: str) -> None:
    assert _SECRETS_CONTEXT.search(text) is None, _SECRETS_CONTEXT.search(text)
    assert re.search(r"^\s*environment\s*:", text, re.M) is None
    assert "id-token" not in text


def test_new_job_names_no_secret_environment_or_id_token() -> None:
    _assert_no_secret_environment_or_id_token(_without_comments(_job_text(_NEW_JOB)))
    # The job as GitHub reads it: with every alias filled in.
    job = _ci_jobs()[_NEW_JOB]
    _assert_no_secret_environment_or_id_token(yaml.safe_dump(job))
    assert "environment" not in job
    assert job["permissions"] == {"contents": "read"}
    # GitHub gives the workflow-level parts of ci.yml to this job too.
    _assert_ci_outside_jobs_names_no_secret(_CI.read_text())


def _assert_ci_outside_jobs_names_no_secret(text: str) -> None:
    """GitHub gives the workflow-level parts (such as ``env:``) to every job."""
    workflow = yaml.safe_load(text)
    outside = {key: value for key, value in workflow.items() if key != "jobs"}
    assert _SECRETS_CONTEXT.search(yaml.safe_dump(outside)) is None, outside
    # The raw lines of every top-level part that is not `jobs:`, as written.
    raw: list[str] = []
    in_jobs = False
    for line in _without_comments(text).splitlines():
        if line and not line[0].isspace():
            in_jobs = line.startswith("jobs:")
        if not in_jobs:
            raw.append(line)
    found = _SECRETS_CONTEXT.search("\n".join(raw))
    assert found is None, found


def test_ci_outside_the_jobs_names_no_secret() -> None:
    _assert_ci_outside_jobs_names_no_secret(_CI.read_text())


@pytest.mark.parametrize(
    "entry",
    [
        "env:\n  HF_TOKEN: ${{ secrets.HF_TOKEN }}\n",
        "env:\n  ALL: ${{ toJSON(secrets) }}\n",
        "env:\n  T: ${{ secrets['HF_TOKEN'] }}\n",
        "defaults:\n  run:\n    shell: bash\nenv:\n  T: ${{ secrets.X }}\n",
        "env:\n  T: ${{ secrets.SHARED_TOKEN }}\n",
        "env:\n  T: ${{ secrets.SH }}\n",
    ],
)
def test_ci_outside_the_jobs_check_finds_a_secret(entry: str) -> None:
    text = _CI.read_text().replace("\njobs:\n", f"\n{entry}jobs:\n", 1)
    with pytest.raises(AssertionError):
        _assert_ci_outside_jobs_names_no_secret(text)
    # Also when the part comes after `jobs:`.
    with pytest.raises(AssertionError):
        _assert_ci_outside_jobs_names_no_secret(_CI.read_text() + entry)


def test_smoke_workflow_names_no_secret_environment_or_id_token() -> None:
    raw = (_WORKFLOWS / _SMOKE_WORKFLOW).read_text()
    _assert_no_secret_environment_or_id_token(_without_comments(raw))
    workflow = _workflow(_SMOKE_WORKFLOW)
    _assert_no_secret_environment_or_id_token(yaml.safe_dump(workflow))
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]["smoke"]) == {
        "name",
        "needs",
        "if",
        "runs-on",
        "cache-mode",
        "timeout-minutes",
        "steps",
    }


def test_smoke_job_has_no_cache_access() -> None:
    """``cache-mode: none``: GitHub gives the job no cache access at all."""
    assert _workflow(_SMOKE_WORKFLOW)["jobs"]["smoke"].get("cache-mode") == "none"


def test_a_secret_in_the_second_site_job_or_the_workflow_env_is_found() -> None:
    text = _CI.read_text()
    secret = "${{ secrets.SHARED_TOKEN }}"
    marker = "      - *unit-sync\n"
    assert text.count(marker) == 1
    in_step = text.replace(marker, marker + f"      - name: Extra\n        run: echo {secret}\n", 1)
    job = yaml.safe_load(in_step)["jobs"][_NEW_JOB]
    with pytest.raises(AssertionError):
        _assert_no_secret_environment_or_id_token(yaml.safe_dump(job))
    in_env = text.replace("\njobs:\n", f"\nenv:\n  T: {secret}\njobs:\n", 1)
    assert "\nenv:\n" not in text
    with pytest.raises(AssertionError):
        _assert_ci_outside_jobs_names_no_secret(in_env)


@pytest.mark.parametrize(
    "text",
    [
        "x: ${{ secrets.HF_TOKEN }}",
        "x: ${{ secrets['HF_TOKEN'] }}",
        "x: ${{ toJSON(secrets) }}",
        "if: secrets.A != ''",
        "x: ${{ secrets.SHARED_TOKEN }}",
        "x: ${{ secrets.SH }}",
        "    environment: release",
        "      id-token: write",
    ],
)
def test_the_text_check_finds_each_form(text: str) -> None:
    with pytest.raises(AssertionError):
        _assert_no_secret_environment_or_id_token(text)


def test_the_text_check_allows_names_that_only_look_alike() -> None:
    _assert_no_secret_environment_or_id_token(
        'REQUIRE_SECRETS: "false"\nrun: bash scripts/ci/repo_secrets.sh\n'
        "- name: Sync Python environment (locked)\n"
    )


def test_only_two_places_name_the_site_label() -> None:
    found: dict[str, int] = {}
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        count = path.read_text().count(_SITE_LABEL)
        if count:
            found[path.name] = count
    assert set(found) == {"ci.yml", _SMOKE_WORKFLOW}, found
    assert _job_text(_NEW_JOB).count(_SITE_LABEL) == found["ci.yml"], (
        "in ci.yml only the job python-unit-shard-second-site may name the label"
    )
    assert _workflow(_SMOKE_WORKFLOW)["jobs"]["smoke"]["runs-on"] == _SITE_LABEL


def test_smoke_workflow_has_no_pull_request_trigger() -> None:
    workflow = _workflow(_SMOKE_WORKFLOW)
    # PyYAML reads the bare `on:` key as the boolean True.
    assert set(workflow[True]) == {"workflow_dispatch", "schedule"}
    assert "pull_request" not in workflow[True]
    assert "pull_request_target" not in workflow[True]


def test_smoke_workflow_is_safe_by_construction() -> None:
    workflow = _workflow(_SMOKE_WORKFLOW)
    assert workflow[True]["schedule"] == [{"cron": "7,37 * * * *"}]
    assert workflow["concurrency"]["cancel-in-progress"] is True
    job = workflow["jobs"]["smoke"]
    assert job["needs"] == "switch"
    assert job["if"] == "needs.switch.outputs.run == 'true'"
    switch = workflow["jobs"]["switch"]
    assert switch["runs-on"] == "ubuntu-latest"
    assert switch["outputs"] == {"run": "${{ steps.switch.outputs.run }}"}
    assert "env" not in job, "a private value must stay on the pod, not in the log"
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False}
    for step in job["steps"]:
        assert "env" not in step, "the runner prints a step's env block in the public log"
    args = job["steps"][-1]["run"].split()
    assert args[:4] == ["bash", "scripts/ci/second_site_smoke.sh", "--image", args[3]]
    assert args[4] == "https://github.com"


def _switch(tmp_path: Path, event: str, value: str | None) -> bool:
    """Whether the smoke job runs: the `if` of the switch job, then its step.

    GitHub compares strings in an `if` without regard to case.
    """
    switch = _workflow(_SMOKE_WORKFLOW)["jobs"]["switch"]
    assert switch["if"] == ("github.event_name != 'schedule' || vars.MEHO_CI_SECOND_SITE == 'on'")
    if event == "schedule" and (value or "").lower() != "on":
        return False
    (step,) = switch["steps"]
    output = tmp_path / "output.txt"
    output.touch()
    env = {"EVENT_NAME": event, "MEHO_CI_SECOND_SITE": value or "", "GITHUB_OUTPUT": str(output)}
    result = _bash_step(step["run"], tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = output.read_text().splitlines()
    return line == "run=true"


@pytest.mark.parametrize(
    ("event", "value", "runs"),
    [
        ("schedule", "on", True),
        ("schedule", "ON", False),
        ("schedule", "On", False),
        ("schedule", "off", False),
        ("schedule", None, False),
        ("workflow_dispatch", None, True),
        ("workflow_dispatch", "ON", True),
    ],
)
def test_smoke_schedule_needs_the_exact_value_on(
    tmp_path: Path, event: str, value: str | None, runs: bool
) -> None:
    assert _switch(tmp_path, event, value) is runs


def test_upper_case_on_leaves_both_paths_off(tmp_path: Path) -> None:
    (tmp_path / "switch").mkdir()
    (tmp_path / "pick").mkdir()
    assert _switch(tmp_path / "switch", "schedule", "ON") is False
    value, _ = _pick(tmp_path / "pick", runs=[_smoke_run(60)], MEHO_CI_SECOND_SITE="ON")
    assert value == ""


def test_smoke_pulls_one_of_the_unit_test_images() -> None:
    args = _workflow(_SMOKE_WORKFLOW)["jobs"]["smoke"]["steps"][-1]["run"].split()
    image = args[args.index("--image") + 1]
    script = _step(_NEW_JOB, "Use the public test images")["run"]
    assert f"PROXY/{image.removeprefix('docker.io/')}" in script


def test_only_the_second_site_watchdog_may_cancel_a_run() -> None:
    writers = sorted(
        name
        for name, job in _ci_jobs().items()
        if (job.get("permissions") or {}).get("actions") == "write"
    )
    assert writers == ["second-site-watchdog"]


# --- The same test steps in both shard jobs --------------------------------------


def _step(job: str, name: str) -> dict[str, Any]:
    for step in _ci_jobs()[job]["steps"]:
        if step.get("name") == name:
            return cast("dict[str, Any]", step)
    raise AssertionError(f"no step {name!r} in {job}")


def test_shared_test_steps_are_the_same_in_both_shard_jobs() -> None:
    jobs = _ci_jobs()
    normal = [s for s in jobs["python-unit-shard"]["steps"] if s["name"] not in _NORMAL_ONLY_STEPS]
    new = [s for s in jobs[_NEW_JOB]["steps"] if s["name"] not in _SECOND_SITE_ONLY_STEPS]
    assert [s["name"] for s in normal] == [s["name"] for s in new]
    assert normal == new


def test_shared_test_steps_are_aliases_not_copies() -> None:
    """The new job takes each shared step from python-unit-shard by alias."""
    raw = _job_text(_NEW_JOB)
    aliases = re.findall(r"^      - \*(unit-[a-z0-9-]+)$", raw, re.M)
    normal_raw = _job_text("python-unit-shard")
    anchors = re.findall(r"^      - &(unit-[a-z0-9-]+)$", normal_raw, re.M)
    assert aliases == anchors
    names = [s["name"] for s in _ci_jobs()[_NEW_JOB]["steps"]]
    assert len(aliases) == len(names) - len(_SECOND_SITE_ONLY_STEPS)


def test_the_steps_that_differ_are_in_the_same_order() -> None:
    names = [s["name"] for s in _ci_jobs()[_NEW_JOB]["steps"]]
    assert names[:3] == [
        "Check that this runner is on the second site",
        "Checkout",
        "Use the public test images",
    ]
    normal = [s["name"] for s in _ci_jobs()["python-unit-shard"]["steps"]]
    guard = "Embedding-model drift guard (evoila/meho#574)"
    assert names[names.index(guard) - 1] == "Restore the fastembed model from the cache"
    assert normal[normal.index(guard) - 1] == "Cache fastembed model (drift-guard + slow-lane)"
    assert names[names.index(guard) + 1] == normal[normal.index(guard) + 1]
    for name in ("Install uv", "Restore the spaCy model from the cache"):
        assert names[names.index(name) + 1] == normal[normal.index(name) + 1]
    for steps in (names, normal):
        assert steps[steps.index(_HIDE_STEP) - 2] == "Pytest (unit shard ${{ matrix.shard }}/6)"
        assert steps[steps.index(_HIDE_STEP) - 1] == "Check the skipped tests of this shard"
        assert steps[steps.index(_HIDE_STEP) + 1] == (
            "Upload per-test times (input for the shard split)"
        )


def test_hide_step_is_the_same_as_in_the_normal_job_without_its_env() -> None:
    normal = dict(_step("python-unit-shard", _HIDE_STEP))
    assert set(normal.pop("env")) == {"CI_IMAGE_PROXY"}
    assert _step(_NEW_JOB, _HIDE_STEP) == normal


def test_each_report_upload_of_the_second_site_job_needs_the_hide_step() -> None:
    steps = _ci_jobs()[_NEW_JOB]["steps"]
    names = [s["name"] for s in steps]
    hide = names.index(_HIDE_STEP)
    uploads = [
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert uploads
    for index in uploads:
        assert index > hide, names[index]
        assert "steps.hide-proxy-name.outcome == 'success'" in steps[index]["if"]


def test_second_site_job_has_the_time_limit_of_the_normal_job() -> None:
    jobs = _ci_jobs()
    assert jobs[_NEW_JOB]["timeout-minutes"] == jobs["python-unit-shard"]["timeout-minutes"]


# --- The second-site job restores caches and never saves one ---------------------

# A setup action with a cache option: (the option that turns the cache on, its
# default, the option that turns saving off, or None when it has none).
_SETUP_CACHES: dict[str, tuple[str, str, str | None]] = {
    "astral-sh/setup-uv@": ("enable-cache", "auto", "save-cache"),
    "actions/setup-python@": ("cache", "", None),
    "actions/setup-node@": ("cache", "", None),
    "actions/setup-go@": ("cache", "true", None),
    "actions/setup-java@": ("cache", "", None),
}


def _cache_saving_steps(steps: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for step in steps:
        uses = str(step.get("uses", ""))
        options = step.get("with") or {}
        name = str(step.get("name", uses))
        if uses.startswith(("actions/cache/save@", "actions/cache@")):
            found.append(name)
            continue
        for action, (switch, default, save) in _SETUP_CACHES.items():
            if not uses.startswith(action):
                continue
            cache_on = str(options.get(switch, default)).lower() not in ("false", "")
            save_off = save is not None and str(options.get(save, "")).lower() == "false"
            if cache_on and not save_off:
                found.append(name)
    return found


def _second_site_saving_steps(text: str) -> list[str]:
    # yaml fills in every alias, so a save step in a shared step counts too.
    return _cache_saving_steps(_ci_jobs(text)[_NEW_JOB]["steps"])


def test_second_site_job_has_read_only_cache_access() -> None:
    """``cache-mode: read``: GitHub lets the job's token restore caches, never save one."""
    assert _ci_jobs()[_NEW_JOB].get("cache-mode") == "read"


def test_second_site_job_restores_caches_and_never_saves_one() -> None:
    assert _second_site_saving_steps(_CI.read_text()) == []
    uses = [str(s.get("uses", "")) for s in _ci_jobs()[_NEW_JOB]["steps"]]
    assert sum(u.startswith("actions/cache/restore@") for u in uses) == 4


def test_normal_shard_job_still_saves_its_caches() -> None:
    assert _cache_saving_steps(_ci_jobs()["python-unit-shard"]["steps"]) == [
        "Install uv",
        "Cache fastembed model (drift-guard + slow-lane)",
        "Restore the spaCy model from the cache",
        "Save the PHP package files to the cache",
        "Save the GBK locale package files to the cache",
    ]


_SAVE_STEP = (
    "      - name: Save something to the cache\n"
    "        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0\n"
    "        with:\n"
    "          path: ~/.cache/x\n"
    "          key: x\n"
)


def _with_step_after(text: str, marker: str, step: str) -> str:
    assert text.count(marker) == 1, marker
    return text.replace(marker, marker + step, 1)


def test_save_check_finds_a_save_step_in_the_job() -> None:
    text = _with_step_after(_CI.read_text(), "      - *unit-sync\n", _SAVE_STEP)
    assert _second_site_saving_steps(text) == ["Save something to the cache"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("uses: actions/cache/restore@", "uses: actions/cache/save@"),
        ("uses: actions/cache/restore@", "uses: actions/cache@"),
    ],
)
def test_save_check_finds_a_save_step_in_a_shared_anchored_step(old: str, new: str) -> None:
    text = _CI.read_text()
    anchor = "      - &unit-php-restore\n"
    start = text.index(anchor)
    end = text.index("\n      - ", start + len(anchor))
    step = text[start:end]
    assert old in step
    text = text[:start] + step.replace(old, new) + text[end:]
    assert _second_site_saving_steps(text) == ["Restore the PHP package files from the cache"]


def test_save_check_finds_an_anchored_save_step_used_by_alias() -> None:
    text = _CI.read_text()
    shared = _SAVE_STEP.replace("      - name:", "      - &unit-extra-save\n        name:")
    text = text.replace("      - &unit-spacy\n", shared + "\n      - &unit-spacy\n", 1)
    text = text.replace(
        "      - *unit-spacy\n", "      - *unit-extra-save\n      - *unit-spacy\n", 1
    )
    assert _second_site_saving_steps(text) == ["Save something to the cache"]


@pytest.mark.parametrize(
    "options",
    [
        {"enable-cache": True},
        {"enable-cache": True, "save-cache": "auto"},
        {},
    ],
)
def test_save_check_finds_a_setup_action_that_saves_its_cache(options: dict[str, Any]) -> None:
    step = {"name": "Install uv", "uses": "astral-sh/setup-uv@abc", "with": options}
    assert _cache_saving_steps([step]) == ["Install uv"]


def test_save_check_allows_a_setup_action_with_its_cache_off_or_its_save_off() -> None:
    steps = [
        {"name": "a", "uses": "astral-sh/setup-uv@abc", "with": {"enable-cache": False}},
        {
            "name": "b",
            "uses": "astral-sh/setup-uv@abc",
            "with": {"enable-cache": True, "save-cache": False},
        },
        {"name": "c", "uses": "actions/setup-python@abc", "with": {"python-version": "3.13"}},
        {"name": "d", "uses": "actions/cache/restore@abc", "with": {"path": "x", "key": "x"}},
    ]
    assert _cache_saving_steps(steps) == []


def test_drift_guard_is_the_same_command_without_the_token() -> None:
    guard = "Embedding-model drift guard (evoila/meho#574)"
    normal = _step("python-unit-shard", guard)
    new = _step(_NEW_JOB, guard)
    assert new["run"] == normal["run"]
    assert new["env"] == {k: v for k, v in normal["env"].items() if k != "HF_TOKEN"}


def test_image_step_uses_the_same_image_lines_with_no_secret() -> None:
    normal = _step("python-unit-shard", "Read the CI repository secrets")["run"]
    new = _step(_NEW_JOB, "Use the public test images")
    assert new["run"] == normal.replace(" CI_IMAGE_PROXY SPEC_SHELF_REPOSITORY", "")
    assert new["env"] == {"REQUIRE_SECRETS": "false"}


def test_image_step_writes_only_public_names(tmp_path: Path) -> None:
    github_env = tmp_path / "github_env"
    github_env.touch()
    script = _step(_NEW_JOB, "Use the public test images")["run"]
    env = {"GITHUB_WORKSPACE": str(_REPO_ROOT), "GITHUB_ENV": str(github_env)}
    result = _bash_step(script, tmp_path, {**env, "REQUIRE_SECRETS": "false"})
    assert result.returncode == 0, result.stdout + result.stderr
    written = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert set(written) == {
        "MEHO_TEST_PGVECTOR_IMAGE",
        "MEHO_TEST_VALKEY_IMAGE",
        "RYUK_CONTAINER_IMAGE",
        "MEHO_TEST_VAULT_IMAGE",
        "MEHO_TEST_OPENBAO_IMAGE",
    }
    for value in written.values():
        assert value.split("/", 1)[0] == "docker.io", value


def test_new_job_has_no_login_and_no_spec_shelf() -> None:
    job = _ci_jobs()[_NEW_JOB]
    names = {s["name"] for s in job["steps"]}
    assert not names & (
        _SECRET_STEPS - {"Embedding-model drift guard (evoila/meho#574)", _HIDE_STEP}
    )
    assert set(job["env"]) == {"MEHO_CONSUMER_DOCS_ROOT"}
    assert "HF_TOKEN" not in yaml.safe_dump(job)


@pytest.mark.parametrize(
    ("site", "attempt", "passes"),
    [("site2", "1", True), ("", "1", False), ("other", "1", False), ("SITE2", "1", False)],
)
def test_site_check_fails_off_the_second_site(
    tmp_path: Path, site: str, attempt: str, passes: bool
) -> None:
    step = _ci_jobs()[_NEW_JOB]["steps"][0]
    assert step["if"] == "github.run_attempt == '1'"
    result = _bash_step(step["run"], tmp_path, {"MEHO_CI_SITE": site})
    assert (result.returncode == 0) is passes, result.stdout


# --- The fan-in in both modes -----------------------------------------------------


def _fan_in(
    tmp_path: Path, *, second_site: str, shards: str, site2: str
) -> subprocess.CompletedProcess[str]:
    script = _step("python-unit-lane", "Assert lint + typecheck + every pytest shard passed")["run"]
    assert "${{" not in script
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
        "OUTSIDE_RESULT": "skipped",
    }
    return _bash_step(script, tmp_path, values)


@pytest.mark.parametrize(
    ("second_site", "shards", "site2", "passes"),
    [
        # Site on: both shard jobs must succeed.
        ("on", "success", "success", True),
        ("on", "success", "failure", False),
        ("on", "success", "cancelled", False),
        ("on", "success", "skipped", False),
        ("on", "failure", "success", False),
        ("on", "skipped", "success", False),
        # Site off: the new job must be skipped, the rest as today.
        ("", "success", "skipped", True),
        ("", "failure", "skipped", False),
        ("", "success", "success", False),
        ("", "success", "failure", False),
    ],
)
def test_fan_in_result(
    tmp_path: Path, second_site: str, shards: str, site2: str, passes: bool
) -> None:
    result = _fan_in(tmp_path, second_site=second_site, shards=shards, site2=site2)
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_fan_in_needs_both_shard_jobs() -> None:
    lane = _ci_jobs()["python-unit-lane"]
    assert lane["needs"] == [
        "changes",
        "python-lint",
        "python-unit-shard",
        _NEW_JOB,
        "python-outside-backend",
    ]
    assert lane["if"] == "always()"


# --- The decision: pick_second_site.sh -------------------------------------------


# --- Artifacts of the second site never reach the usual runners --------------

_ARTIFACT_RULE = "# No job on the usual runners runs anything from an artifact of this job."
_TEMPLATE = re.compile(r"\$\{\{.*?\}\}")


def _artifact_steps(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [s for s in job.get("steps", []) if f"actions/{action}-artifact@" in s.get("uses", "")]


def _uploaders(jobs: dict[str, Any], artifact: str) -> set[str]:
    found = set()
    for name, job in jobs.items():
        for step in _artifact_steps(job, "upload"):
            parts = _TEMPLATE.split(step["with"]["name"])
            if re.fullmatch(".+".join(map(re.escape, parts)), artifact):
                found.add(name)
    return found


def _coverage_download_problems(jobs: dict[str, Any]) -> list[str]:
    problems = []
    downloads = _artifact_steps(jobs["python-coverage-combine"], "download")
    if not downloads:
        problems.append("the coverage job downloads nothing")
    for step in downloads:
        artifact = step.get("with", {}).get("name")
        if not artifact or _TEMPLATE.search(artifact) or "pattern" in step["with"]:
            problems.append(f"not one fixed artifact name: {step.get('with')}")
            continue
        uploaders = _uploaders(jobs, artifact)
        if not uploaders:
            problems.append(f"{artifact}: no job uploads it")
        for name in uploaders:
            if name == _NEW_JOB or _SITE_LABEL in str(jobs[name].get("runs-on")):
                problems.append(f"{artifact}: uploaded by {name}")
    return problems


def test_second_site_job_states_the_artifact_rule() -> None:
    assert _ARTIFACT_RULE in _job_text(_NEW_JOB)


def test_coverage_job_downloads_only_artifacts_of_the_usual_runners() -> None:
    assert _coverage_download_problems(_ci_jobs()) == []


def test_coverage_download_check_finds_an_artifact_of_the_second_site() -> None:
    jobs = _ci_jobs()
    step = _artifact_steps(jobs["python-coverage-combine"], "download")[0]
    step["with"]["name"] = "python-unit-shard-junit-4"
    assert _coverage_download_problems(jobs) == [
        f"python-unit-shard-junit-4: uploaded by {_NEW_JOB}"
    ]
    step["with"] = {"pattern": "python-*"}
    assert _coverage_download_problems(jobs) == [
        "not one fixed artifact name: {'pattern': 'python-*'}"
    ]


def _iso(age_seconds: int) -> str:
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _smoke_run(
    age_seconds: int,
    conclusion: str = "success",
    head: str | None = _REPO,
    branch: str = "main",
    repo: str | None = _REPO,
    run_id: int = 4242,
    attempt: int = 1,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "id": run_id,
        "run_attempt": attempt,
        "conclusion": conclusion,
        "head_branch": branch,
        "run_started_at": _iso(age_seconds),
    }
    if head is not None:
        run["head_repository"] = {"full_name": head}
    if repo is not None:
        run["repository"] = {"full_name": repo}
    return run


def _pick(
    tmp_path: Path,
    *,
    runs: list[dict[str, Any]] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    jobs_body: str | None = None,
    gh_body: str | None = None,
    **overrides: str | None,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"workflow_runs": runs if runs is not None else []}))
    jobs_answer = tmp_path / "jobs.json"
    jobs_answer.write_text(json.dumps({"jobs": jobs if jobs is not None else [_smoke_job()]}))
    calls = tmp_path / "calls.log"
    jobs_reply = jobs_body if jobs_body is not None else f'cat "{jobs_answer}"'
    body = (
        gh_body
        if gh_body is not None
        else (
            f'echo "$*" >> "{calls}"\n'
            f'case "$*" in *"/jobs"*) {jobs_reply} ;; *) cat "{answer}" ;; esac'
        )
    )
    _fake(bin_dir, "gh", body)
    output = tmp_path / "output.txt"
    env: dict[str, str | None] = {
        "GITHUB_OUTPUT": str(output),
        "MEHO_CI_SECOND_SITE": "on",
        "EVENT_NAME": "pull_request",
        "HEAD_REPO": _REPO,
        "ACTOR": "someone",
        "COVERAGE_LABEL": "false",
        "RUN_ATTEMPT": "1",
        "DOCS_ONLY": "false",
        "BACKEND_PYTHON": "true",
        "SINGLE_CONNECTOR": "",
        "TEST_MAP": "false",
        **overrides,
    }
    result = _run(_PICK, tmp_path, {k: v for k, v in env.items() if v is not None})
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("second_site=")
    notices = [line for line in result.stdout.splitlines() if line.startswith("::notice")]
    assert len(notices) == 1, result.stdout
    return lines[0].removeprefix("second_site="), result


def _smoke_job(conclusion: str = "success", name: str = _SMOKE_JOB) -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion}


def _gh_calls(tmp_path: Path) -> list[str]:
    calls = tmp_path / "calls.log"
    return calls.read_text().splitlines() if calls.exists() else []


def test_pick_uses_the_site_after_a_recent_smoke_pass(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(600)])
    assert value == "on"
    assert "use the second runner site" in result.stdout
    runs_call, jobs_call = _gh_calls(tmp_path)
    assert f"repos/{_REPO}/actions/workflows/{_SMOKE_WORKFLOW}/runs?branch=main" in runs_call
    assert "status=success" in runs_call
    assert f"repos/{_REPO}/actions/runs/4242/attempts/1/jobs" in jobs_call


def test_pick_reads_the_jobs_of_the_newest_pass_and_its_attempt(tmp_path: Path) -> None:
    runs = [_smoke_run(9000, run_id=1), _smoke_run(60, run_id=2, attempt=3), _smoke_run(5000)]
    value, _ = _pick(tmp_path, runs=runs)
    assert value == "on"
    assert f"repos/{_REPO}/actions/runs/2/attempts/3/jobs" in _gh_calls(tmp_path)[1]


def test_smoke_job_name_matches_the_workflow() -> None:
    assert _workflow("second-site-smoke.yml")["jobs"]["smoke"]["name"] == _SMOKE_JOB
    assert f'smoke_job="{_SMOKE_JOB}"' in _PICK.read_text()


@pytest.mark.parametrize(
    "jobs",
    [
        pytest.param([_smoke_job("skipped")], id="smoke-skipped"),
        pytest.param(
            [_smoke_job(name="Check the switch of the second runner site")], id="only-switch"
        ),
        pytest.param([_smoke_job("failure")], id="smoke-failed"),
        pytest.param([_smoke_job("cancelled")], id="smoke-cancelled"),
        pytest.param([], id="no-jobs"),
    ],
)
def test_pick_needs_the_smoke_job_itself_to_pass(
    tmp_path: Path, jobs: list[dict[str, Any]]
) -> None:
    # The workflow run passed, but no runner of the site ran the smoke job.
    switch = _smoke_job(name="Check the switch of the second runner site")
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], jobs=[switch, *jobs])
    assert value == ""
    assert "the smoke job did not pass" in result.stdout


def test_skipped_smoke_job_keeps_all_six_shards_on_the_usual_pool(tmp_path: Path) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(60)], jobs=[_smoke_job("skipped")])
    assert value == ""
    assert _shard_matrix(second_site=value) == [1, 2, 3, 4, 5, 6]


def test_pick_jobs_api_error(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], jobs_body="echo 'HTTP 500' >&2; exit 1")
    assert value == ""
    assert "could not read the jobs" in result.stdout


@pytest.mark.parametrize("body", ["echo not-json", "echo '{}'", """echo '{"jobs": "x"}'"""])
def test_pick_odd_jobs_answer(tmp_path: Path, body: str) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(60)], jobs_body=body)
    assert value == ""


@pytest.mark.parametrize("field", ["id", "run_attempt"])
def test_pick_run_without_id_or_attempt(tmp_path: Path, field: str) -> None:
    run = _smoke_run(60)
    del run[field]
    value, _ = _pick(tmp_path, runs=[run])
    assert value == ""


@pytest.mark.parametrize("switch", [None, "", "off", "true", "ON", "on "])
def test_pick_variable_off(tmp_path: Path, switch: str | None) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], MEHO_CI_SECOND_SITE=switch)
    assert value == ""
    assert "today's pool: the second runner site is not switched on" in result.stdout
    assert _gh_calls(tmp_path) == []


@pytest.mark.parametrize(
    "event", ["merge_group", "schedule", "push", "workflow_dispatch", "pull_request_target", ""]
)
def test_pick_only_a_pull_request(tmp_path: Path, event: str) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], EVENT_NAME=event)
    assert value == ""
    assert "today's pool: only a pull request run" in result.stdout
    assert _gh_calls(tmp_path) == []


@pytest.mark.parametrize("head", ["stranger/fork", "", None])
def test_pick_fork(tmp_path: Path, head: str | None) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], HEAD_REPO=head)
    assert value == ""
    assert "fork" in result.stdout


def test_pick_dependabot(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], ACTOR="dependabot[bot]")
    assert value == ""
    assert "Dependabot" in result.stdout


_SITE_CASES = [
    pytest.param([600], id="recent-pass"),
    pytest.param([40 * 60], id="pass-near-the-limit"),
    pytest.param([9000, 60], id="newest-of-many"),
]


@pytest.mark.parametrize("ages", _SITE_CASES)
def test_pick_coverage_label_keeps_the_usual_runners(tmp_path: Path, ages: list[int]) -> None:
    # Made here, not at collection time: a slow run must not age a result past the limit.
    runs = [_smoke_run(age, run_id=i, attempt=i) for i, age in enumerate(ages, start=1)]
    (tmp_path / "without").mkdir()
    (tmp_path / "with").mkdir()
    assert _pick(tmp_path / "without", runs=runs)[0] == "on"
    value, result = _pick(tmp_path / "with", runs=runs, COVERAGE_LABEL="true")
    assert value == ""
    assert "ci-coverage-validate" in result.stdout
    assert _gh_calls(tmp_path / "with") == []


@pytest.mark.parametrize("label", ["", "False", None])
def test_pick_unknown_coverage_label_keeps_the_usual_runners(
    tmp_path: Path, label: str | None
) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], COVERAGE_LABEL=label)
    assert value == ""
    assert "ci-coverage-validate" in result.stdout


@pytest.mark.parametrize("attempt", ["2", "3", "", None])
def test_pick_attempt_2(tmp_path: Path, attempt: str | None) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], RUN_ATTEMPT=attempt)
    assert value == ""
    assert "re-run" in result.stdout


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"DOCS_ONLY": "true"}, id="docs-only"),
        pytest.param({"DOCS_ONLY": ""}, id="docs-only-unknown"),
        pytest.param({"BACKEND_PYTHON": "false"}, id="no-backend-change"),
        pytest.param({"BACKEND_PYTHON": ""}, id="backend-unknown"),
        pytest.param({"SINGLE_CONNECTOR": "vault"}, id="one-connector"),
        pytest.param({"TEST_MAP": "true"}, id="test-map"),
    ],
)
def test_pick_only_when_six_shards_run(tmp_path: Path, overrides: dict[str, Any]) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(60)], **overrides)
    assert value == ""
    assert "does not run all six unit shards" in result.stdout


def test_pick_counts_a_pass_just_inside_the_limit(tmp_path: Path) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(45 * 60 - 120)])
    assert value == "on"


def test_pick_old_smoke_run(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, runs=[_smoke_run(45 * 60 + 120)])
    assert value == ""
    assert "old" in result.stdout


def test_pick_no_smoke_run(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, runs=[])
    assert value == ""
    assert "no smoke run passed" in result.stdout


def test_pick_uses_the_newest_pass_in_any_order(tmp_path: Path) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(9000), _smoke_run(60), _smoke_run(5000)])
    assert value == "on"


@pytest.mark.parametrize(
    "run",
    [
        pytest.param({"conclusion": "failure"}, id="failed"),
        pytest.param({"head": "stranger/fork"}, id="other-repository"),
        pytest.param({"branch": "feature"}, id="other-branch"),
        pytest.param({"head": None, "repo": None}, id="both-names-missing"),
        pytest.param({"head": None}, id="head-name-missing"),
        pytest.param({"repo": None}, id="repository-name-missing"),
        pytest.param({"head": "other/repo", "repo": "other/repo"}, id="both-another-repository"),
    ],
)
def test_pick_ignores_a_run_that_does_not_count(tmp_path: Path, run: dict[str, Any]) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(60, **run)])
    assert value == ""


def test_pick_treats_a_future_time_as_unknown(tmp_path: Path) -> None:
    value, _ = _pick(tmp_path, runs=[_smoke_run(-7200)])
    assert value == ""


def test_pick_api_error(tmp_path: Path) -> None:
    value, result = _pick(tmp_path, gh_body="echo 'HTTP 500' >&2; exit 1")
    assert value == ""
    assert "could not read the smoke runs" in result.stdout


@pytest.mark.parametrize(
    "body",
    [
        "echo not-json",
        "echo '{}'",
        "echo '[]'",
        """echo '{"workflow_runs": [{"conclusion": "success"}]}'""",
        """echo '{"workflow_runs": "x"}'""",
    ],
)
def test_pick_odd_answer(tmp_path: Path, body: str) -> None:
    value, _ = _pick(tmp_path, gh_body=body)
    assert value == ""


def test_pick_unreadable_time(tmp_path: Path) -> None:
    run = _smoke_run(60)
    run["run_started_at"] = "soon"
    value, _ = _pick(tmp_path, runs=[run])
    assert value == ""


def test_pick_step_can_never_break_the_changes_job() -> None:
    changes = _ci_jobs()["changes"]
    (step,) = [s for s in changes["steps"] if s.get("id") == "second-site"]
    assert step is changes["steps"][-1]
    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] <= 2
    assert step["run"] == "bash scripts/ci/pick_second_site.sh"
    assert step["env"]["MEHO_CI_SECOND_SITE"] == "${{ vars.MEHO_CI_SECOND_SITE }}"
    assert step["env"]["TEST_MAP"] == changes["outputs"]["test_map"]
    assert step["env"]["RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert step["env"]["ACTOR"] == "${{ github.actor }}"
    assert step["env"]["COVERAGE_LABEL"] == (
        "${{ contains(github.event.pull_request.labels.*.name, 'ci-coverage-validate')"
        " && 'true' || 'false' }}"
    )
    # The token only needs to read the run history, which the job could already.
    assert changes["permissions"] == {"contents": "read", "actions": "read"}
    assert changes["outputs"]["second_site"] == "${{ steps.second-site.outputs.second_site }}"


# --- The watchdog: second_site_watchdog.sh ---------------------------------------


def _shards(**statuses: str) -> str:
    """An answer of the jobs API. Keys are like ``s4``; other jobs are added."""
    jobs = [
        {"name": "Detect migration + docs-only changes", "status": "completed"},
        {"name": "Python unit shard 1/6", "status": "queued"},
    ]
    for key, status in statuses.items():
        jobs.append({"name": f"Python unit shard {key[1:]}/6", "status": status})
    return json.dumps({"jobs": jobs})


def _no_status(*keys: str, **statuses: str) -> str:
    """Like ``_shards``, but the shards in ``keys`` have no status field."""
    answer = json.loads(_shards(**statuses))
    answer["jobs"] += [{"name": f"Python unit shard {key[1:]}/6"} for key in keys]
    return json.dumps(answer)


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


_WAITING = _shards(s4="queued", s5="queued", s6="queued")
_STARTED = _shards(s4="in_progress", s5="in_progress", s6="in_progress")
# The watchdog reads the jobs once a minute for 30 minutes.
_POLLS = 30


def _assert_read_right_before_each_cancel(calls: list[str]) -> None:
    for index, call in enumerate(calls):
        if call.startswith("run cancel"):
            assert calls[index - 1].startswith("api "), calls


def test_watchdog_reads_the_shard_names_of_the_new_job() -> None:
    names = [
        f"Python unit shard {shard}/6"
        for shard in _ci_jobs()[_NEW_JOB]["strategy"]["matrix"]["shard"]
    ]
    pattern = re.search(r'test\("([^"]+)"\)', _WATCHDOG.read_text())
    assert pattern is not None
    for name in names:
        assert re.search(pattern.group(1), name), name
    assert not re.search(pattern.group(1), "Python unit shard 1/6")


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
    assert "MEHO_CI_SECOND_SITE" in result.stdout


def test_watchdog_waits_for_a_shard_that_starts_late(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING, _WAITING, _STARTED])
    assert result.returncode == 0
    assert _cancels(calls) == []


@pytest.mark.parametrize("status", ["pending", "requested", "waiting"])
def test_watchdog_treats_other_statuses_as_waiting(tmp_path: Path, status: str) -> None:
    result, calls = _watch(tmp_path, [_shards(s4=status, s5="in_progress", s6="completed")])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1


def test_watchdog_reads_the_jobs_right_before_it_cancels(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1
    assert len(calls) == _POLLS + 2, "the polls, one fresh read and the cancel"
    _assert_read_right_before_each_cancel(calls)


def test_watchdog_leaves_shards_that_started_during_the_last_wait(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING] * _POLLS + [_STARTED])
    assert result.returncode == 0
    assert _cancels(calls) == []
    assert "has a runner now" in result.stdout


def test_watchdog_does_not_cancel_from_old_status_when_the_read_fails(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING] * _POLLS + ["FAIL"])
    assert result.returncode == 1
    assert _cancels(calls) == []
    assert "Could not stop this run, or could not read its jobs" in result.stdout


def test_watchdog_does_not_cancel_from_old_status_on_an_odd_answer(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING] * _POLLS + ["not json"])
    assert result.returncode == 1
    assert _cancels(calls) == []


def test_watchdog_failed_reads_during_the_wait_do_not_count(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING] + ["FAIL"] * (_POLLS - 1) + [_STARTED])
    assert result.returncode == 0
    assert _cancels(calls) == []


def test_watchdog_tries_the_last_read_again(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING] * _POLLS + ["FAIL", _WAITING])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1
    _assert_read_right_before_each_cancel(calls)


def test_watchdog_never_cancels_when_it_cannot_read_the_jobs(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, ["FAIL"])
    assert result.returncode == 1
    assert _cancels(calls) == []


def test_watchdog_cancels_when_a_fresh_read_shows_a_waiting_shard(tmp_path: Path) -> None:
    """Two shards still wait and the answer leaves out the third."""
    result, calls = _watch(tmp_path, [_WAITING] * _POLLS + [_shards(s4="queued", s5="queued")])
    assert result.returncode == 0
    assert len(_cancels(calls)) == 1
    _assert_read_right_before_each_cancel(calls)
    assert "2 unit shard(s) waited 30 minutes" in result.stdout


def test_watchdog_never_cancels_on_shards_with_no_status(tmp_path: Path) -> None:
    """An incomplete answer: three shards, none with a status."""
    result, calls = _watch(tmp_path, [_no_status("s4", "s5", "s6")])
    assert result.returncode == 1
    assert _cancels(calls) == []
    assert len(calls) == _POLLS + 3, "the polls go on, then three fresh reads"
    assert "Could not stop this run, or could not read its jobs in full" in result.stdout


def test_watchdog_does_not_end_early_on_shards_with_no_status(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_no_status("s6", s4="in_progress", s5="in_progress")])
    assert result.returncode == 1
    assert _cancels(calls) == []
    assert "Nothing left to watch" not in result.stdout


def test_watchdog_never_cancels_when_no_listed_shard_waits(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_shards(s4="in_progress", s5="completed")])
    assert result.returncode == 1
    assert _cancels(calls) == []


def test_watchdog_reports_when_it_cannot_stop_the_run(tmp_path: Path) -> None:
    result, calls = _watch(tmp_path, [_WAITING], cancel_exit=1)
    assert result.returncode == 1
    assert len(_cancels(calls)) == 3
    _assert_read_right_before_each_cancel(calls)
    assert "Could not stop this run" in result.stdout


def test_watchdog_reads_only_this_attempt(tmp_path: Path) -> None:
    _, calls = _watch(tmp_path, [_shards(s4="completed", s5="completed", s6="completed")])
    assert calls[0] == f"api repos/{_REPO}/actions/runs/42/attempts/1/jobs?per_page=100"


def test_watchdog_job_runs_only_when_the_site_was_picked() -> None:
    job = _ci_jobs()["second-site-watchdog"]
    condition = " ".join(job["if"].split())
    assert condition == (
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "needs.changes.outputs.second_site == 'on' && "
        "github.run_attempt == '1'"
    )
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read", "actions": "write"}
    assert job["steps"][-1]["run"] == "bash scripts/ci/second_site_watchdog.sh"


# --- The smoke check: second_site_smoke.sh ---------------------------------------

_REACH = "https://reach.example"
_IMAGE = "docker.io/valkey/valkey:8"
_ADDRESS = "secret.example"
_ADDRESS_URL = f"https://{_ADDRESS}/"


def _smoke(
    tmp_path: Path,
    deny: str | None,
    *reach: str,
    image: str | None = _IMAGE,
    site: str = "site2",
    exits: dict[str, int] | None = None,
    pull_exit: int = 0,
    resolved: str = "0.000100",
    connected: str = "0.000000",
    statuses: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the smoke check with a fake curl and docker.

    ``exits`` gives the exit code for a URL, ``statuses`` the HTTP status of
    a reach check. By default a reach check gets 200 and a deny check times
    out.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "curl.log"
    rules = "\n".join(f'  "{url}") code={code} ;;' for url, code in (exits or {}).items())
    status_rules = "\n".join(
        f'  "{url}") status={code} ;;' for url, code in (statuses or {}).items()
    )
    _fake(
        bin_dir,
        "curl",
        f"""echo "$*" >> "{calls}"
for url; do :; done
code=0
status=200
case "$url" in
{status_rules}
esac
if [[ "$*" == *--noproxy* ]]; then
  code=28; printf '%s %s' '{resolved}' '{connected}'
else
  printf '%s' "$status"
fi
case "$url" in
{rules}
esac
exit "$code"
""",
    )
    _fake(
        bin_dir,
        "docker",
        f"""echo "docker $*" >> "{calls}"
echo "error from cache.{_ADDRESS}" >&2
exit {pull_exit}""",
    )
    env = {"MEHO_CI_SITE": site}
    if deny is not None:
        env["MEHO_CI_DENY_PROBE_HOSTS"] = deny
    args = ["--image", image] if image is not None else []
    result = _run(_SMOKE, tmp_path, env, *args, *reach)
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
    result, calls = _smoke(tmp_path, f"{_ADDRESS},second.example:8443", _REACH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "address 1 of 2: exit code 28" in result.stdout
    assert "address 2 of 2: exit code 28" in result.stdout
    assert f"pulled {_IMAGE}" in result.stdout
    assert "passed" in result.stdout
    assert f"docker pull --quiet {_IMAGE}" in calls
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
def test_smoke_timeout_only(tmp_path: Path, code: int) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, exits={_ADDRESS_URL: code})
    assert result.returncode == 1
    assert f"Address 1 of 1: exit code {code}" in result.stdout
    assert "passed" not in result.stdout
    _assert_private(result, _ADDRESS)


def test_smoke_fails_on_a_timeout_after_the_connection_was_made(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, connected="0.031000")
    assert result.returncode == 1
    assert "connection was made" in result.stdout
    _assert_private(result, _ADDRESS)


def test_smoke_dns_timeout_is_a_fail(tmp_path: Path) -> None:
    """curl ends with 28 here too, but no connection was tried."""
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, resolved="0.000000")
    assert result.returncode == 1
    assert "Address 1 of 1: exit code 28" in result.stdout
    assert "never resolved" in result.stdout
    assert "passed" not in result.stdout
    _assert_private(result, _ADDRESS)


def test_smoke_fails_when_curl_reports_no_times(tmp_path: Path) -> None:
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, resolved="", connected="")
    assert result.returncode == 1
    assert "never resolved" in result.stdout


@pytest.mark.parametrize("resolved", ["0.000014", "0.000331", "0.115978"])
def test_smoke_passes_on_a_timeout_after_the_lookup(tmp_path: Path, resolved: str) -> None:
    """The measured times: an IP address, a hosts file name, a real DNS name."""
    result, _ = _smoke(tmp_path, _ADDRESS, _REACH, resolved=resolved)
    assert result.returncode == 0, result.stdout


def test_smoke_checks_every_address(tmp_path: Path) -> None:
    deny = f"{_ADDRESS}, second.example:8443 ,third.example"
    result, calls = _smoke(tmp_path, deny, _REACH, exits={"https://second.example:8443/": 7})
    assert result.returncode == 1
    assert "address 1 of 3: exit code 28" in result.stdout
    assert "Address 2 of 3: exit code 7" in result.stdout
    assert "address 3 of 3: exit code 28" in result.stdout
    assert len(calls) == 5, "one reach check, one pull and three deny checks"
    _assert_private(result, _ADDRESS, "second.example", "third.example")


def test_smoke_reads_a_list_that_wraps_onto_a_new_line(tmp_path: Path) -> None:
    """The second address is reachable, so the check must fail."""
    deny = f"{_ADDRESS},\nsecond.example\r\nthird.example"
    result, calls = _smoke(tmp_path, deny, _REACH, exits={"https://second.example/": 7})
    assert result.returncode == 1
    assert "address 1 of 3: exit code 28" in result.stdout
    assert "Address 2 of 3: exit code 7" in result.stdout
    assert "address 3 of 3: exit code 28" in result.stdout
    assert calls[-1].split()[-1] == "https://third.example/"
    _assert_private(result, _ADDRESS, "second.example", "third.example")


@pytest.mark.parametrize("status", ["502", "404", "403", "301", "000"])
def test_smoke_needs_a_2xx_answer_to_count_a_url_as_reached(tmp_path: Path, status: str) -> None:
    """A proxy that cannot reach the server answers 502, and curl ends with 0."""
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, statuses={_REACH: status})
    assert result.returncode == 1
    assert f"Cannot reach {_REACH} (HTTP status {status})" in result.stdout
    assert "reached" not in result.stdout
    assert len(calls) == 1, "no other check runs when a reach check fails"


def test_smoke_reach_check_follows_redirects_and_reads_the_status(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH)
    assert result.returncode == 0, result.stdout
    assert f"reached {_REACH} (HTTP status 200)" in result.stdout
    reach_call = calls[0]
    assert reach_call.split()[-1] == _REACH
    assert "--location" in reach_call
    assert "%{http_code}" in reach_call


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
    assert "%{time_namelookup} %{time_connect}" in deny_call
    for call in calls:
        assert "--insecure" not in call
        assert " -k " not in call


@pytest.mark.parametrize("deny", [None, "", "   ", ",", " , ,"])
def test_smoke_empty_list_is_a_fail(tmp_path: Path, deny: str | None) -> None:
    result, calls = _smoke(tmp_path, deny, _REACH)
    assert result.returncode == 1
    assert "is empty" in result.stdout
    assert calls == []


@pytest.mark.parametrize("site", ["", "other", "extra", "SITE2"])
def test_smoke_fails_on_the_wrong_site(tmp_path: Path, site: str) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, site=site)
    assert result.returncode == 1
    assert "MEHO_CI_SITE" in result.stdout
    assert calls == []


def test_smoke_fails_without_a_url_to_reach(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS)
    assert result.returncode == 1
    assert calls == []


@pytest.mark.parametrize(
    "image",
    [None, "", "valkey/valkey:8", "mirror.example/valkey/valkey:8", "docker.io/valkey/valkey"],
)
def test_smoke_needs_a_public_image(tmp_path: Path, image: str | None) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, image=image)
    assert result.returncode == 1
    assert "--image" in result.stdout
    assert calls == []


def test_smoke_fails_when_the_image_pull_fails(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, pull_exit=1)
    assert result.returncode == 1
    assert f"docker pull {_IMAGE} failed (exit code 1)" in result.stdout
    assert not any(call.endswith(_ADDRESS_URL) for call in calls), "no deny check after it"
    # docker's own error text is dropped: it can name the cache.
    _assert_private(result, _ADDRESS)


def test_smoke_fails_when_a_needed_url_is_out_of_reach(tmp_path: Path) -> None:
    result, calls = _smoke(tmp_path, _ADDRESS, _REACH, exits={_REACH: 28})
    assert result.returncode == 1
    assert f"Cannot reach {_REACH} (curl exit code 28)" in result.stdout
    assert len(calls) == 1, "no other check runs when a reach check fails"
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
