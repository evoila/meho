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


def _job_is_safe(job: dict[str, Any]) -> bool:
    runs_on = _runs_on_str(job)
    job_if = str(job.get("if", ""))

    # Reusable-workflow job (no runs-on, delegates to `uses:`): the runner
    # is defined in the called workflow, out of scope here.
    if not runs_on and "uses" in job:
        return True

    # Not on the internal pool at all -> disposable / GitHub-hosted.
    if _INTERNAL_POOL_MARKER not in runs_on:
        return True

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

    jobs = wf.get("jobs", {})
    offenders = [
        name for name, job in jobs.items() if isinstance(job, dict) and not _job_is_safe(job)
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
