# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The migration tests in a merge-queue run (#7105).

In a ``merge_group`` run, the ``changes`` job in ``.github/workflows/ci.yml``
sets ``migrations`` with the same file pattern as on a pull request, on the
diff from the group's base commit to its head. When that diff cannot be read,
it keeps ``migrations=true``. The nightly run always runs the migration tests.

The tests run the real ``detect`` step, taken from the workflow file, in a
small git repository. Its ``origin`` remote is the repository itself, so the
step can fetch the base commit. The shell starts the way the runner starts a
``run:`` step (``bash --noprofile --norc -eo pipefail``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_CONNECTOR_SELECT = "backend/tests/_unit_connector_select.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _jobs() -> dict[str, Any]:
    workflow = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))
    return cast("dict[str, Any]", workflow["jobs"])


def _detect_step() -> dict[str, Any]:
    (step,) = [s for s in _jobs()["changes"]["steps"] if s.get("id") == "detect"]
    return cast("dict[str, Any]", step)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: list[str], message: str) -> str:
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{message}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _queue_repo(
    tmp_path: Path, groups: list[list[str]], *, remote: bool = True
) -> tuple[Path, str]:
    """A repository with ``main`` and one queue commit per pull request in ``groups``.

    Returns the repository and the base commit. HEAD is the head of the group.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    tool = repo / _CONNECTOR_SELECT
    tool.parent.mkdir(parents=True)
    tool.write_text((_REPO_ROOT / _CONNECTOR_SELECT).read_text())
    base = _commit(repo, ["README.md"], "base")
    for i, files in enumerate(groups):
        _commit(repo, files, f"pull request {i}")
    if remote:
        _git(repo, "remote", "add", "origin", str(repo))
    return repo, base


def _run(
    tmp_path: Path, repo: Path, event: str, base: str, head: str
) -> tuple[dict[str, str], str]:
    script = cast("str", _detect_step()["run"]).replace("${{ github.event_name }}", event)
    output = tmp_path / "github_output"
    output.write_text("")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(runner_temp),
        "QUEUE_BASE_SHA": base,
        "QUEUE_HEAD_SHA": head,
    }
    path = tmp_path / "step.sh"
    path.write_text(script)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return outputs, result.stdout


_FULL_RUN_REST = {
    "docs_only": "false",
    "backend_python": "true",
    "redaction_compare": "true",
    "single_connector": "",
}


def test_the_step_reads_the_base_and_head_of_the_queue_group() -> None:
    assert _detect_step()["env"] == {
        "QUEUE_BASE_SHA": "${{ github.event.merge_group.base_sha }}",
        "QUEUE_HEAD_SHA": "${{ github.event.merge_group.head_sha }}",
    }


def test_queue_group_without_migration_change_skips_the_migration_tests(
    tmp_path: Path,
) -> None:
    repo, base = _queue_repo(tmp_path, [["cli/main.go"], ["backend/src/meho_backplane/app.py"]])
    outputs, _ = _run(tmp_path, repo, "merge_group", base, _git(repo, "rev-parse", "HEAD"))
    assert outputs == {"migrations": "false", **_FULL_RUN_REST}


@pytest.mark.parametrize(
    "migration_file",
    [
        "backend/alembic/versions/0001_x.py",
        "backend/tests/migrations/test_x.py",
        "backend/src/meho_backplane/db/migrate.py",
        "scripts/ci/check_migration_heads.py",
        ".github/workflows/migration-compat.yml",
    ],
)
def test_queue_group_with_a_migration_change_runs_the_migration_tests(
    tmp_path: Path, migration_file: str
) -> None:
    repo, base = _queue_repo(tmp_path, [["cli/main.go"], [migration_file]])
    outputs, _ = _run(tmp_path, repo, "merge_group", base, _git(repo, "rev-parse", "HEAD"))
    assert outputs == {"migrations": "true", **_FULL_RUN_REST}


def test_migration_change_in_an_earlier_pull_request_of_the_group_counts(
    tmp_path: Path,
) -> None:
    # HEAD^1..HEAD would see only the last pull request of the group.
    repo, base = _queue_repo(tmp_path, [["backend/alembic/versions/0001_x.py"], ["cli/main.go"]])
    outputs, _ = _run(tmp_path, repo, "merge_group", base, _git(repo, "rev-parse", "HEAD"))
    assert outputs["migrations"] == "true"


def test_queue_group_uses_the_pull_request_pattern(tmp_path: Path) -> None:
    # One pattern for both events: the queue branch reads the same variable.
    script = cast("str", _detect_step()["run"])
    assert script.count('grep -qE "${migration_files}"') == 2
    assert script.count("migration_files='") == 1


@pytest.mark.parametrize(
    ("base", "head", "reason"),
    [
        ("", "HEAD", "no base or head commit"),
        ("BASE", "", "no base or head commit"),
        ("not-a-commit", "HEAD", "no base or head commit"),
        ("BASE", "HEAD", "could not fetch the base commit"),
        ("f" * 40, "HEAD", "could not fetch the base commit"),
        ("BASE", "BASE", "empty diff"),
    ],
)
def test_queue_group_fails_closed_when_the_diff_cannot_be_read(
    tmp_path: Path, base: str, head: str, reason: str
) -> None:
    remote = reason != "could not fetch the base commit" or base != "BASE"
    repo, base_sha = _queue_repo(tmp_path, [["cli/main.go"]], remote=remote)
    head_sha = _git(repo, "rev-parse", "HEAD")
    names = {"BASE": base_sha, "HEAD": head_sha}
    outputs, stdout = _run(
        tmp_path, repo, "merge_group", names.get(base, base), names.get(head, head)
    )
    assert outputs == {"migrations": "true", **_FULL_RUN_REST}
    assert reason in stdout
    assert "fail-safe" in stdout


def test_queue_group_with_a_quoted_path_fails_closed(tmp_path: Path) -> None:
    repo, base = _queue_repo(tmp_path, [['cli/a"b.go']])
    outputs, stdout = _run(tmp_path, repo, "merge_group", base, _git(repo, "rev-parse", "HEAD"))
    assert outputs["migrations"] == "true"
    assert "quoted path in the diff" in stdout


def test_nightly_run_always_runs_the_migration_tests(tmp_path: Path) -> None:
    repo, base = _queue_repo(tmp_path, [["cli/main.go"]])
    outputs, _ = _run(tmp_path, repo, "schedule", base, _git(repo, "rev-parse", "HEAD"))
    assert outputs["migrations"] == "true"
    job_if = " ".join(_jobs()["python-migration-tests"]["if"].split())
    assert job_if.startswith("github.event_name == 'schedule' ||")


def test_migration_job_runs_in_the_queue_only_on_a_migration_change() -> None:
    job_if = " ".join(_jobs()["python-migration-tests"]["if"].split())
    assert (
        "(github.event_name == 'merge_group' && needs.changes.outputs.migrations == 'true')"
        in job_if
    )
    assert _jobs()["python-migration-tests"]["needs"] == "changes"
