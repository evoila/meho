# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Untrusted-PR runner-isolation invariant across every workflow (F06).

The `meho-runners-ci` / `-ci-heavy` self-hosted pool is internal
infrastructure. A workflow that runs on that pool in response to a
**fork** pull request executes attacker-controlled code (the fork's
checked-out content, and for a fork `pull_request` event the fork's own
copy of the workflow body) on internal infrastructure — the class of
self-hosted-runner risk GitHub documents in its secure-use reference.

The finding (meho-internal#268) was that the fix for this had only been
applied to the principal CI file: `migration-compat.yml`,
`dependency-license-check.yml`, `readme-version-check.yml`,
`secret-scan.yml` and `security-scan.yml` scheduled the internal pool on
`pull_request` with no fork guard. This test pins the invariant across
**every** workflow so a future addition cannot silently reopen it — the
exact "audit every workflow, not just the principal CI file" mandate of
the finding.

Invariant: no job reachable by a fork `pull_request` /
`pull_request_target` may run on the internal pool unless it is either
skipped on fork PRs (a fork guard `if:`) or routes fork PRs to a
disposable GitHub-hosted runner. The check is deliberately a structural
heuristic on the established idioms in `docs/codebase/devops.md`
("Untrusted pull-request isolation"); it catches the gross regression
(an unconditional internal-pool job that a fork PR can schedule), which
is precisely the F06 bug.

The authoritative enforcement is GitHub settings (fork-PR approval
policy + runner-group access), which a PR cannot edit and this test
cannot read; this test guards the in-repo arm.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Any label naming the internal self-hosted pool (meho-runners-ci,
# meho-runners-ci-heavy, and the legacy meho-runners form).
_INTERNAL_POOL_MARKER = "meho-runners"
# The fork guard the publish workflows use: on a pull_request the job
# runs only when the head repo IS this repo (fork excluded).
_FORK_GUARD = "head.repo.full_name == github.repository"
# The disposable-runner routing expression: a fork pull_request resolves
# runs-on to a GitHub-hosted label instead of the internal pool.
_ROUTE_COND = "head.repo.full_name != github.repository"
_HOSTED_TARGET = "ubuntu-latest"
# A job gated to push only is not reachable by a pull_request at all.
_PUSH_ONLY = "github.event_name == 'push'"
# The merge-queue-only route (#6325): the job uses an internal pool only on
# a merge_group event, and a GitHub-hosted runner on every other event.
# Forks never start merge_group runs. Only this exact expression counts.
_MERGE_QUEUE_ROUTE = re.compile(
    r"\$\{\{ github\.event_name == 'merge_group' && '(?P<pool>[^']+)' \|\| 'ubuntu-latest' \}\}"
)
# The internal pool labels the merge-queue route may name.
_KNOWN_POOLS = frozenset(
    {"meho-runners-ci", "meho-runners-ci-heavy", "meho-runners-ci-heavy-queue"}
)


def _load_workflow(path: Path) -> dict[Any, Any]:
    with path.open() as fh:
        return cast("dict[Any, Any]", yaml.safe_load(fh))


def _triggers(wf: dict[Any, Any]) -> Any:
    # PyYAML parses the bare `on:` key as the boolean True (YAML 1.1
    # treats on/off/yes/no as booleans), so the trigger block lands under
    # the True key, not the string "on".
    return wf.get("on", wf.get(True))


def _trigger_names(triggers: Any) -> set[str]:
    if isinstance(triggers, dict):
        return {str(k) for k in triggers}
    if isinstance(triggers, list):
        return {str(k) for k in triggers}
    if triggers is None:
        return set()
    return {str(triggers)}


def _is_fork_reachable_workflow(wf: dict[Any, Any]) -> bool:
    names = _trigger_names(_triggers(wf))
    return "pull_request" in names or "pull_request_target" in names


def _runs_on_str(job: dict[str, Any]) -> str:
    runs_on = job.get("runs-on")
    if runs_on is None:
        return ""
    if isinstance(runs_on, list):
        return " ".join(str(x) for x in runs_on)
    return str(runs_on)


def _job_is_safe(job: dict[str, Any], triggers: set[str]) -> bool:
    runs_on = _runs_on_str(job)
    job_if = str(job.get("if", ""))

    # Reusable-workflow job (no runs-on, delegates to `uses:`): the runner
    # is defined in the called workflow, out of scope here.
    if not runs_on and "uses" in job:
        return True

    # Not on the internal pool at all -> disposable / GitHub-hosted.
    if _INTERNAL_POOL_MARKER not in runs_on:
        return True

    # The merge-queue-only route (#6325). Safe only when the whole runs-on
    # is the exact expression, it names a known pool, and the workflow has
    # no pull_request_target trigger. No other rule can rescue such a job.
    route = _MERGE_QUEUE_ROUTE.fullmatch(runs_on)
    if route is not None:
        return route.group("pool") in _KNOWN_POOLS and "pull_request_target" not in triggers

    # References the internal pool. Safe only if one of:
    #   1. it routes fork PRs to a GitHub-hosted runner, or
    #   2. a fork guard skips the job on fork PRs, or
    #   3. the job runs on push events only (never a pull_request).
    if _ROUTE_COND in runs_on and _HOSTED_TARGET in runs_on:
        return True
    if _FORK_GUARD in job_if:
        return True
    return _PUSH_ONLY in job_if


def _workflow_files() -> list[Path]:
    files = sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))
    return files


def test_workflows_directory_present() -> None:
    assert _WORKFLOWS_DIR.is_dir(), f"missing {_WORKFLOWS_DIR}"
    assert _workflow_files(), "no workflow files found"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_fork_reachable_job_schedules_internal_pool(path: Path) -> None:
    wf = _load_workflow(path)
    if not _is_fork_reachable_workflow(wf):
        pytest.skip(f"{path.name} has no pull_request trigger")

    triggers = _trigger_names(_triggers(wf))
    jobs = wf.get("jobs", {})
    offenders = [
        name
        for name, job in jobs.items()
        if isinstance(job, dict) and not _job_is_safe(job, triggers)
    ]
    assert not offenders, (
        f"{path.name}: job(s) {offenders} can be scheduled on the internal "
        f"'{_INTERNAL_POOL_MARKER}*' pool by a fork pull_request without a "
        "fork guard or a disposable-runner route. See docs/codebase/devops.md "
        "'Untrusted pull-request isolation' (F06)."
    )


def test_migration_compat_routes_fork_off_internal_pool() -> None:
    # AC3: a fork PR touching only scripts/ci/check_migration_compat.py
    # cannot schedule an internal runner.
    wf = _load_workflow(_WORKFLOWS_DIR / "migration-compat.yml")
    check = wf["jobs"]["check"]
    runs_on = _runs_on_str(check)
    assert _ROUTE_COND in runs_on and _HOSTED_TARGET in runs_on, (
        "migration-compat.yml 'check' job must route fork PRs to a "
        f"disposable runner; runs-on is {runs_on!r}"
    )


def test_security_scan_skips_fork_prs() -> None:
    # security-scan runs inside an internal Harbor-proxied container that a
    # GitHub-hosted runner cannot reach, so it must skip fork PRs (the
    # required context still runs on merge_group + push).
    wf = _load_workflow(_WORKFLOWS_DIR / "security-scan.yml")
    semgrep = wf["jobs"]["semgrep"]
    assert _FORK_GUARD in str(semgrep.get("if", "")), (
        "security-scan.yml 'semgrep' job must carry the fork guard"
    )


_MQ_ROUTE = "${{ github.event_name == 'merge_group' && 'meho-runners-ci' || 'ubuntu-latest' }}"


def test_merge_queue_only_route_is_safe() -> None:
    # #6325: the own pool is used only on merge_group, which a fork cannot
    # start; every other event gets a GitHub-hosted runner. No fork guard.
    job = {"if": "github.event_name == 'merge_group'", "runs-on": _MQ_ROUTE}
    assert _job_is_safe(job, {"pull_request", "merge_group", "push"})


@pytest.mark.parametrize(
    ("runs_on", "job_if", "triggers"),
    [
        # Another expression with an own pool: the fallback is not hosted.
        (
            "${{ github.event_name == 'merge_group' && 'meho-runners-ci'"
            " || 'meho-runners-ci-heavy' }}",
            "",
            {"pull_request", "merge_group"},
        ),
        # The exact expression, but the pool is not a known label.
        (
            "${{ github.event_name == 'merge_group' && 'meho-runners-other' || 'ubuntu-latest' }}",
            "",
            {"pull_request", "merge_group"},
        ),
        # The exact expression in a workflow that runs on pull_request_target,
        # even with a fork guard.
        (
            _MQ_ROUTE,
            "github.event.pull_request.head.repo.full_name == github.repository",
            {"pull_request_target", "merge_group"},
        ),
        # A pull_request event reaches the own-pool side of the expression.
        (
            "${{ (github.event_name == 'merge_group' || github.event_name == 'pull_request')"
            " && 'meho-runners-ci' || 'ubuntu-latest' }}",
            "github.event_name == 'pull_request' || github.event_name == 'merge_group'",
            {"pull_request", "merge_group"},
        ),
    ],
    ids=["own-pool-fallback", "unknown-pool", "pull-request-target", "pull-request-reaches-pool"],
)
def test_merge_queue_route_variants_fail(runs_on: str, job_if: str, triggers: set[str]) -> None:
    job = {"if": job_if, "runs-on": runs_on}
    assert not _job_is_safe(job, triggers)


# --- merge_group jobs must not use a hosted label (#6325) -------------------
#
# A merge-queue run waits for a GitHub-hosted runner when a job uses one, and
# the queue stops when none is free. Every job a merge_group event can start
# must resolve to an own pool.

_FORK_ROUTE = re.compile(
    r"\$\{\{ \(github\.event_name == 'pull_request' && "
    r"github\.event\.pull_request\.head\.repo\.full_name != github\.repository\) && "
    r"'ubuntu-latest' \|\| '(?P<pool>[^']+)' \}\}"
)
# A job `if:` that names one other event first never starts on merge_group.
_OTHER_EVENT_ONLY = re.compile(
    r"github\.event_name == '(push|workflow_dispatch|pull_request|schedule)'(\s*&&.*)?",
    re.S,
)
# Jobs that stay on a hosted label although their workflow has a merge_group
# trigger. Each needs a reason.
_HOSTED_ON_MERGE_GROUP_ALLOWED: dict[tuple[str, str], str] = {
    (
        "docs-site.yml",
        "deploy",
    ): "runs only for tag pushes and main pushes; a merge-queue branch is neither",
}


def _merge_group_label(runs_on: str) -> str:
    """The runner label a job gets when the event is merge_group."""
    route = _MERGE_QUEUE_ROUTE.fullmatch(runs_on)
    if route is not None:
        return route.group("pool")
    fork_route = _FORK_ROUTE.fullmatch(runs_on)
    if fork_route is not None:
        return fork_route.group("pool")
    return runs_on


def _merge_group_hosted_jobs(path: Path, wf: dict[Any, Any]) -> list[str]:
    if "merge_group" not in _trigger_names(_triggers(wf)):
        return []
    found = []
    for name, job in (wf.get("jobs") or {}).items():
        job_if = " ".join(str(job.get("if", "")).split())
        if job_if and "merge_group" not in job_if and _OTHER_EVENT_ONLY.fullmatch(job_if):
            continue
        if "uses" in job and "runs-on" not in job:
            continue
        label = _merge_group_label(_runs_on_str(job))
        if _INTERNAL_POOL_MARKER in label or (path.name, name) in _HOSTED_ON_MERGE_GROUP_ALLOWED:
            continue
        found.append(f"{path.name}:{name} -> {label!r}")
    return found


def test_no_merge_group_job_uses_a_hosted_label() -> None:
    bad: list[str] = []
    for path in _workflow_files():
        bad.extend(_merge_group_hosted_jobs(path, _load_workflow(path)))
    assert not bad, (
        "These jobs can run on a merge_group event but use a GitHub-hosted runner. "
        "A merge-queue run then waits for a hosted runner and the queue can stop. "
        "Use the merge-queue route with an own pool label (see devops.md): " + "; ".join(bad)
    )


def test_merge_group_hosted_check_flags_a_hosted_job() -> None:
    wf = {
        "on": {"merge_group": {}, "pull_request": {}},
        "jobs": {
            "hosted": {"runs-on": "ubuntu-latest"},
            "ok-route": {"runs-on": _MQ_ROUTE},
            "ok-own": {"runs-on": "meho-runners-ci"},
            "pr-only": {"if": "github.event_name == 'pull_request'", "runs-on": "ubuntu-latest"},
        },
    }
    assert _merge_group_hosted_jobs(Path("x.yml"), wf) == ["x.yml:hosted -> 'ubuntu-latest'"]


def test_merge_group_hosted_check_ignores_workflows_without_merge_group() -> None:
    wf = {"on": {"pull_request": {}}, "jobs": {"hosted": {"runs-on": "ubuntu-latest"}}}
    assert _merge_group_hosted_jobs(Path("x.yml"), wf) == []


def test_pull_request_workflows_cancel_old_runs_but_not_merge_group() -> None:
    # A new push cancels the run of the old commit; a merge-queue run is never
    # cancelled by a newer one.
    bad = []
    for path in _workflow_files():
        wf = _load_workflow(path)
        names = _trigger_names(_triggers(wf))
        if "pull_request" not in names and "pull_request_target" not in names:
            continue
        concurrency = wf.get("concurrency")
        if not isinstance(concurrency, dict) and path.name != "merge-queue-cleanup.yml":
            bad.append(f"{path.name}: no concurrency")
            continue
        if not isinstance(concurrency, dict):
            continue
        cancel = concurrency.get("cancel-in-progress")
        if cancel is False:
            bad.append(f"{path.name}: cancel-in-progress is false")
        elif (
            isinstance(cancel, str) and "merge_group" in names and "!= 'merge_group'" not in cancel
        ):
            bad.append(f"{path.name}: cancel-in-progress does not exclude merge_group")
        elif cancel is True and "merge_group" in names:
            bad.append(f"{path.name}: cancels merge_group runs")
    assert not bad, "; ".join(bad)
