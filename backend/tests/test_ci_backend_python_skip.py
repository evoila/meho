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

_SKIPPED_ON_NO_BACKEND = ("python-lint", "python-unit-shard", "python-integration-shard")


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


_CONNECTOR_SELECT = "backend/tests/_unit_connector_select.py"


def _detect(
    tmp_path: Path,
    event: str,
    changed: list[str] | None,
    *,
    tool_dir: Path | None = None,
    runner_temp: Path | None = None,
) -> dict[str, str]:
    """Run the detect step and return its outputs.

    ``changed`` lists the files the pull request changes. ``None`` builds a
    repository with one commit only, so ``git diff HEAD^1 HEAD`` fails.
    ``tool_dir`` goes first on ``PATH``, so a test can replace a tool.
    ``runner_temp`` is where the step writes its list of changed files; the
    default is a folder under ``tmp_path``. The repository holds the real
    connector selection tool, which the step runs after the classifiers.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n")
    tool = repo / _CONNECTOR_SELECT
    tool.parent.mkdir(parents=True)
    tool.write_text((_REPO_ROOT / _CONNECTOR_SELECT).read_text())
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
    search_path = "/usr/bin:/bin:/usr/local/bin"
    if tool_dir is not None:
        search_path = f"{tool_dir}:{search_path}"
    if runner_temp is None:
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
    env = {"PATH": search_path, "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(runner_temp)}
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
        ["ci/eval-baseline.json", "scripts/ci/check_plugin_version.py"],
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
        ["ci/spec-shelf/product-1.0.txt"],
        ["scripts/ci/spec_shelf.sh", "docs/codebase/devops.md"],
    ],
)
def test_pull_request_with_backend_or_ci_change_runs_everything(
    tmp_path: Path, changed: list[str]
) -> None:
    assert _detect(tmp_path, "pull_request", changed)["backend_python"] == "true"


def test_docs_only_pull_request_is_also_no_backend(tmp_path: Path) -> None:
    outputs = _detect(tmp_path, "pull_request", ["docs/x.md", "backend/README.md"])
    assert outputs == {
        "migrations": "false",
        "docs_only": "true",
        "backend_python": "false",
        "single_connector": "",
    }


@pytest.mark.parametrize("event", ["push", "merge_group"])
def test_push_and_merge_group_always_run_everything(tmp_path: Path, event: str) -> None:
    outputs = _detect(tmp_path, event, ["cli/main.go"])
    assert outputs["backend_python"] == "true"
    assert outputs["docs_only"] == "false"


def test_unknown_diff_runs_everything(tmp_path: Path) -> None:
    assert _detect(tmp_path, "pull_request", None)["backend_python"] == "true"


def test_large_diff_with_a_backend_change_runs_everything(tmp_path: Path) -> None:
    # The list of changed files is longer than a pipe buffer (64 KiB). A
    # `grep -q` behind a pipe stops at the first non-docs line, the write
    # into the pipe fails, and with `pipefail` the check read the diff as
    # docs-only. The detector must read the list without a pipe.
    # The migration check has the same shape; a migration file sorts first.
    changed = ["backend/alembic/versions/0001_big_diff.py"] + [
        f"backend/tests/data/big-diff/{'x' * 150}-{i:04d}.txt" for i in range(600)
    ]
    assert sum(len(name) + 1 for name in changed) > 65536
    outputs = _detect(tmp_path, "pull_request", changed)
    assert outputs["migrations"] == "true"
    assert outputs["docs_only"] == "false"
    assert outputs["backend_python"] == "true"


_FULL_RUN = {
    "migrations": "true",
    "docs_only": "false",
    "backend_python": "true",
    "single_connector": "",
}

_LOKI_CHANGE = ["backend/src/meho_backplane/connectors/loki/__init__.py"]


def _fake_tool(tmp_path: Path, name: str, exit_code: int, *, only_with: str = "") -> Path:
    """Put a ``name`` command that exits with ``exit_code`` first on PATH.

    With ``only_with``, the command fails only when its arguments hold that
    text, and otherwise runs the real tool from ``/usr/bin``.
    """
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir(exist_ok=True)
    fake = tool_dir / name
    if only_with:
        fake.write_text(
            "#!/bin/sh\n"
            f'case " $* " in *" {only_with} "*) exit {exit_code} ;; esac\n'
            f'exec /usr/bin/{name} "$@"\n'
        )
    else:
        fake.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    fake.chmod(0o755)
    return tool_dir


def test_failing_classifier_runs_everything(tmp_path: Path) -> None:
    # A classifier `grep` that fails (exit code 2, not the "no match" exit
    # code 1) is an error, not an answer. The detector must then keep every
    # fail-safe default, also for a change that is not docs-only.
    tool_dir = _fake_tool(tmp_path, "grep", 2)
    outputs = _detect(
        tmp_path, "pull_request", ["backend/src/meho_backplane/app.py"], tool_dir=tool_dir
    )
    assert outputs == _FULL_RUN


def test_connector_only_change_selects_the_connector(tmp_path: Path) -> None:
    # The control for the error-path test below: with working tools, a
    # change to one connector folder gives the short connector-only run.
    outputs = _detect(tmp_path, "pull_request", _LOKI_CHANGE)
    assert outputs == {
        "migrations": "false",
        "docs_only": "false",
        "backend_python": "true",
        "single_connector": "loki",
    }


def test_failing_classifier_with_a_connector_change_runs_everything(tmp_path: Path) -> None:
    # The connector selection runs only after every classifier gave an
    # answer. Here the migration check answers and the docs-only check
    # (the one `grep -qvE`) fails: the shorter connector-only run must
    # not be selected.
    tool_dir = _fake_tool(tmp_path, "grep", 2, only_with="-qvE")
    outputs = _detect(tmp_path, "pull_request", _LOKI_CHANGE, tool_dir=tool_dir)
    assert outputs == _FULL_RUN


def test_failing_connector_selection_runs_everything(tmp_path: Path) -> None:
    # When the selection tool fails, nothing the classifiers found is
    # trusted either: a change outside backend/ then runs everything.
    tool_dir = _fake_tool(tmp_path, "python3", 1)
    outputs = _detect(tmp_path, "pull_request", ["cli/main.go"], tool_dir=tool_dir)
    assert outputs == _FULL_RUN


def test_list_file_cannot_be_written_runs_everything(tmp_path: Path) -> None:
    # The step writes the changed files to a file under RUNNER_TEMP. When
    # that write fails, no classifier runs and the full run stays, also for
    # a docs-only change.
    outputs = _detect(
        tmp_path, "pull_request", ["docs/x.md"], runner_temp=tmp_path / "missing-runner-temp"
    )
    assert outputs == _FULL_RUN


def test_detect_step_reads_the_list_from_a_file() -> None:
    # No here-string and no pipe for the list of changed files: both can
    # fail before `grep` runs, and that failure read as "no match".
    script = _step_script("changes", step_id="detect")
    assert "<<<" not in script
    assert "| grep" not in script
    assert "| python3" not in script
    assert 'mktemp "${RUNNER_TEMP:-/tmp}/' in script


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
    assert outputs == {
        "migrations": "true",
        "docs_only": "false",
        "backend_python": "true",
        "single_connector": "",
    }


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
        "${{ needs.changes.outputs.single_connector }}": "",
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


# --- The `python-integration` fan-in -----------------------------------------


def _integration_fan_in(
    tmp_path: Path, *, docs_only: str, backend_python: str, shards: str
) -> subprocess.CompletedProcess[str]:
    script = _step_script("python-integration", name="Assert every integration shard passed")
    for expression, value in {
        "${{ needs.changes.result }}": "success",
        "${{ needs.changes.outputs.docs_only }}": docs_only,
        "${{ needs.changes.outputs.backend_python }}": backend_python,
        "${{ needs.python-integration-shard.result }}": shards,
    }.items():
        assert expression in script, expression
        script = script.replace(expression, value)
    assert "${{" not in script
    return _bash(script, tmp_path, {"PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize(
    ("docs_only", "backend_python", "shards", "passes"),
    [
        # No-backend path: the shards skip; the merge queue runs them.
        ("false", "false", "skipped", True),
        ("false", "false", "failure", False),
        ("false", "false", "cancelled", False),
        # Full run: the shards must pass.
        ("false", "true", "success", True),
        ("false", "true", "skipped", False),
        ("false", "", "skipped", False),
        # Docs-only: the shards skip.
        ("true", "false", "skipped", True),
    ],
)
def test_integration_fan_in_result(
    tmp_path: Path, docs_only: str, backend_python: str, shards: str, passes: bool
) -> None:
    result = _integration_fan_in(
        tmp_path, docs_only=docs_only, backend_python=backend_python, shards=shards
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
