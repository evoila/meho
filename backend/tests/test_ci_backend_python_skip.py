# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the no-backend path in ``.github/workflows/ci.yml`` (#5296).

A pull request that changes no file under ``backend/`` (apart from ``.md``
files) and does not change ``ci.yml`` skips the heavy Python lanes. It runs
only the backend tests that read files outside ``backend/``, in the job
``python-outside-backend``. push and merge_group always run everything.

These tests run the real step scripts, taken from the workflow file: the
``changes`` detector inside a small git repository, and the
``python-unit-lane`` fan-in with the job results filled in. The shell starts
the way the runner starts a ``run:`` step (``bash --noprofile --norc -eo
pipefail``).
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

_SKIPPED_ON_NO_BACKEND = ("python-lint", "python-unit-shard", "python-integration")


def _jobs() -> dict[str, Any]:
    workflow = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))
    return cast("dict[str, Any]", workflow["jobs"])


def _step_script(job: str, step_id: str | None = None, name: str | None = None) -> str:
    for step in _jobs()[job]["steps"]:
        if (step_id and step.get("id") == step_id) or (name and step.get("name") == name):
            return cast("str", step["run"])
    raise AssertionError(f"step not found in job {job}")


def _bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = cwd / "step.sh"
    path.write_text(script)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- The `changes` detector ---------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _detect(tmp_path: Path, event: str, changed: list[str] | None) -> dict[str, str]:
    """Run the detect step and return its outputs.

    ``changed`` lists the files the pull request changes. ``None`` builds a
    repository with one commit only, so ``git diff HEAD^1 HEAD`` fails.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    if changed is not None:
        for name in changed:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change")

    script = _step_script("changes", step_id="detect").replace("${{ github.event_name }}", event)
    output = tmp_path / "github_output"
    output.write_text("")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_OUTPUT": str(output)}
    result = _bash(script, repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.mark.parametrize(
    "changed",
    [
        ["cli/internal/cmd/root.go"],
        ["deploy/charts/meho/values.yaml", "docs/codebase/devops.md"],
        [".github/workflows/image.yml", "scripts/install.sh"],
        ["backend/README.md", "cli/main.go"],
    ],
)
def test_pull_request_without_backend_change_skips_the_python_lanes(
    tmp_path: Path, changed: list[str]
) -> None:
    outputs = _detect(tmp_path, "pull_request", changed)
    assert outputs["backend_python"] == "false"
    assert outputs["docs_only"] == "false"


@pytest.mark.parametrize(
    "changed",
    [
        ["backend/src/meho_backplane/app.py"],
        ["backend/pyproject.toml", "docs/codebase/devops.md"],
        ["cli/main.go", "backend/tests/test_x.py"],
        [".github/workflows/ci.yml"],
    ],
)
def test_pull_request_with_backend_or_ci_change_runs_everything(
    tmp_path: Path, changed: list[str]
) -> None:
    assert _detect(tmp_path, "pull_request", changed)["backend_python"] == "true"


def test_docs_only_pull_request_is_also_no_backend(tmp_path: Path) -> None:
    outputs = _detect(tmp_path, "pull_request", ["docs/x.md", "backend/README.md"])
    assert outputs == {"migrations": "false", "docs_only": "true", "backend_python": "false"}


@pytest.mark.parametrize("event", ["push", "merge_group"])
def test_push_and_merge_group_always_run_everything(tmp_path: Path, event: str) -> None:
    outputs = _detect(tmp_path, event, ["cli/main.go"])
    assert outputs["backend_python"] == "true"
    assert outputs["docs_only"] == "false"


def test_unknown_diff_runs_everything(tmp_path: Path) -> None:
    assert _detect(tmp_path, "pull_request", None)["backend_python"] == "true"


def test_non_ascii_backend_path_runs_everything(tmp_path: Path) -> None:
    # By default git prints `"backend/tests/fixtures/caf\303\251.json"`, in
    # quotes, and the backend/ check would miss it (#5310).
    outputs = _detect(tmp_path, "pull_request", ["backend/tests/fixtures/café.json"])
    assert outputs["backend_python"] == "true"


def test_non_ascii_path_outside_backend_still_skips_the_python_lanes(tmp_path: Path) -> None:
    outputs = _detect(tmp_path, "pull_request", ["cli/internal/cmd/café.go"])
    assert outputs["backend_python"] == "false"
    assert outputs["docs_only"] == "false"


def test_quoted_path_runs_everything(tmp_path: Path) -> None:
    # git always quotes a path that holds a double quote; the detector then
    # keeps every fail-safe default.
    outputs = _detect(tmp_path, "pull_request", ['cli/internal/cmd/say "hi".go'])
    assert outputs == {"migrations": "true", "docs_only": "false", "backend_python": "true"}


# --- Job gates ----------------------------------------------------------------


@pytest.mark.parametrize("job", _SKIPPED_ON_NO_BACKEND)
def test_heavy_python_jobs_skip_when_no_backend_changed(job: str) -> None:
    assert "needs.changes.outputs.backend_python != 'false'" in _jobs()[job]["if"]


def test_outside_backend_job_runs_only_on_a_no_backend_pull_request() -> None:
    job = _jobs()["python-outside-backend"]
    assert job["needs"] == "changes"
    condition = " ".join(job["if"].split())
    assert "github.event_name == 'pull_request'" in condition
    assert "head.repo.full_name == github.repository" in condition
    assert "needs.changes.outputs.backend_python == 'false'" in condition
    assert "needs.changes.outputs.docs_only != 'true'" in condition


def test_outside_backend_job_runs_the_listed_files() -> None:
    script = _step_script(
        "python-outside-backend", name="Pytest (tests that read files outside backend)"
    )
    assert "tests/_outside_backend_tests.py" in script
    assert '"${FILES[@]}"' in script


def test_fan_in_waits_for_the_outside_backend_job() -> None:
    assert "python-outside-backend" in _jobs()["python-unit-lane"]["needs"]


# --- The `python-unit-lane` fan-in -------------------------------------------


def _fan_in(
    tmp_path: Path,
    *,
    docs_only: str,
    backend_python: str,
    lint: str,
    shards: str,
    outside: str,
    detect: str = "success",
) -> subprocess.CompletedProcess[str]:
    script = _step_script(
        "python-unit-lane", name="Assert lint + typecheck + every pytest shard passed"
    )
    for expression, value in {
        "${{ needs.changes.result }}": detect,
        "${{ needs.changes.outputs.docs_only }}": docs_only,
        "${{ needs.changes.outputs.backend_python }}": backend_python,
        "${{ needs.python-lint.result }}": lint,
        "${{ needs.python-unit-shard.result }}": shards,
        "${{ needs.python-outside-backend.result }}": outside,
    }.items():
        assert expression in script, expression
        script = script.replace(expression, value)
    assert "${{" not in script
    return _bash(script, tmp_path, {"PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize(
    ("docs_only", "backend_python", "lint", "shards", "outside", "passes"),
    [
        # No-backend path: lint + shards skip, the short job must pass.
        ("false", "false", "skipped", "skipped", "success", True),
        ("false", "false", "skipped", "skipped", "failure", False),
        ("false", "false", "skipped", "skipped", "cancelled", False),
        ("false", "false", "skipped", "skipped", "skipped", False),
        # Full run: lint + shards must pass, the short job skips.
        ("false", "true", "success", "success", "skipped", True),
        ("false", "true", "success", "skipped", "skipped", False),
        ("false", "true", "skipped", "success", "skipped", False),
        ("false", "true", "success", "success", "failure", False),
        # An empty output (not set) is a full run too.
        ("false", "", "skipped", "skipped", "success", False),
        # Docs-only: everything skips.
        ("true", "false", "skipped", "skipped", "skipped", True),
        ("true", "false", "skipped", "skipped", "failure", False),
    ],
)
def test_fan_in_result(
    tmp_path: Path,
    docs_only: str,
    backend_python: str,
    lint: str,
    shards: str,
    outside: str,
    passes: bool,
) -> None:
    result = _fan_in(
        tmp_path,
        docs_only=docs_only,
        backend_python=backend_python,
        lint=lint,
        shards=shards,
        outside=outside,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_fan_in_fails_when_the_detector_did_not_pass(tmp_path: Path) -> None:
    result = _fan_in(
        tmp_path,
        detect="failure",
        docs_only="",
        backend_python="",
        lint="skipped",
        shards="skipped",
        outside="skipped",
    )
    assert result.returncode != 0
