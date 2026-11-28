# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the one-day trial of unit shard 6 on a GitHub-hosted runner (#7187).

The job ``python-unit-shard-hosted-trial`` runs beside the normal shard 6. The
promises these tests check:

* it runs only on a full pull request run from this repository, never in the
  merge queue, the nightly run or on a fork;
* its text names no secret, no ``environment`` and no ``id-token``;
* no job needs it, so no required check changes, and it never fails the run;
* its test steps are the steps of the second-site shard job, without the
  site check and without the report upload.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from tests.test_ci_second_runner_site import (
    _CI,
    _assert_ci_outside_jobs_names_no_secret,
    _assert_no_secret_environment_or_id_token,
    _ci_jobs,
    _expression,
    _job_text,
    _without_comments,
)

_JOB = "python-unit-shard-hosted-trial"
_SECOND_SITE = "python-unit-shard-second-site"
_LEFT_OUT = {
    "Check that this runner is on the second site",
    "Hide the proxy name in the test reports",
    "Upload per-test times (input for the shard split)",
}


def _full_pull_request_run(**changes: str) -> dict[str, Any]:
    outputs = {
        "docs_only": "false",
        "backend_python": "true",
        "single_connector": "",
        "test_map": "false",
        **changes,
    }
    return {
        "github.event_name": "pull_request",
        "github.event.pull_request.head.repo.full_name": "example-org/example-repo",
        "github.repository": "example-org/example-repo",
        **{f"needs.changes.outputs.{key}": value for key, value in outputs.items()},
    }


def _runs(context: dict[str, Any]) -> bool:
    return bool(_expression("${{ " + _ci_jobs()[_JOB]["if"] + " }}", context))


def test_job_runs_on_a_full_pull_request_run() -> None:
    assert _runs(_full_pull_request_run())


@pytest.mark.parametrize(
    "changes",
    [
        {"docs_only": "true"},
        {"backend_python": "false"},
        {"single_connector": "vmware"},
        {"test_map": "true"},
    ],
)
def test_job_skips_a_short_pull_request_run(changes: dict[str, str]) -> None:
    assert not _runs(_full_pull_request_run(**changes))


@pytest.mark.parametrize("event", ["merge_group", "schedule", "push", "workflow_dispatch"])
def test_job_never_runs_outside_a_pull_request(event: str) -> None:
    assert not _runs({**_full_pull_request_run(), "github.event_name": event})


def test_job_never_runs_for_a_fork() -> None:
    context = _full_pull_request_run()
    context["github.event.pull_request.head.repo.full_name"] = "someone/fork"
    assert not _runs(context)


def test_job_shape() -> None:
    job = _ci_jobs()[_JOB]
    assert job["name"] == "Hosted trial, unit shard 6/6 (not required)"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"] == {"shard": [6]}
    assert job["continue-on-error"] is True
    assert job["needs"] == "changes"
    assert job["permissions"] == {"contents": "read"}
    assert job["cache-mode"] == "read"
    assert job["timeout-minutes"] == _ci_jobs()[_SECOND_SITE]["timeout-minutes"]


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


def test_no_job_needs_the_trial() -> None:
    for name, job in _ci_jobs().items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert _JOB not in needs, name


def test_shard_6_still_runs_in_the_normal_shard_job() -> None:
    matrix = str(_ci_jobs()["python-unit-shard"]["strategy"]["matrix"]["shard"])
    assert "'[1, 2, 3, 4, 5, 6]'" in matrix


def test_steps_are_the_second_site_steps_without_the_left_out_ones() -> None:
    jobs = _ci_jobs()
    second_site = [s for s in jobs[_SECOND_SITE]["steps"] if s["name"] not in _LEFT_OUT]
    assert jobs[_JOB]["steps"] == second_site
    names = {s["name"] for s in jobs[_JOB]["steps"]}
    assert not names & _LEFT_OUT
    uses = [str(s.get("uses", "")) for s in jobs[_JOB]["steps"]]
    assert not any(u.startswith(("actions/upload-artifact@", "actions/cache/save@")) for u in uses)
