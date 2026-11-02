# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the test map run of the unit tests (tests/_unit_test_map.py, #5642).

The rules run on a small fake repository. The CI tests run the real step
scripts from ``.github/workflows/ci.yml``, with a fake ``gh`` command, the
way the runner starts a ``run:`` step.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests import _unit_connector_select as connector_select
from tests import _unit_test_map as test_map

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
SRC = "backend/src/meho_backplane"
CONTRACT = "tests/test_contract.py"

TREE = {
    f"{SRC}/__init__.py": "",
    f"{SRC}/core.py": "X = 1\n",
    f"{SRC}/schemas.py": "class S:\n    pass\n",
    f"{SRC}/service.py": "from meho_backplane.schemas import S\n",
    f"{SRC}/api.py": "from .service import S\n",
    f"{SRC}/boot.py": "Y = 2\n",
    f"{SRC}/templates/page.html": "<p></p>\n",
    "backend/tests/__init__.py": "",
    "backend/tests/conftest.py": "import meho_backplane.boot\n",
    "backend/tests/_helpers.py": "from meho_backplane.schemas import S\n",
    "backend/tests/test_core.py": "def test_core():\n    pass\n",
    "backend/tests/test_service.py": (
        "class TestService:\n    def test_run(self):\n        pass\n\n    def test_other(self):\n"
        "        pass\n"
    ),
    "backend/tests/test_api.py": (
        "from meho_backplane.api import S\n\n\ndef test_api():\n    pass\n"
    ),
    "backend/tests/test_schemas_direct.py": (
        "from meho_backplane import schemas\n\n\ndef test_s():\n    pass\n"
    ),
    "backend/tests/test_via_helper.py": (
        "from tests._helpers import S\n\n\ndef test_h():\n    pass\n"
    ),
    "backend/tests/test_unrelated.py": "def test_u():\n    pass\n",
    f"backend/{CONTRACT}": "def test_contract():\n    pass\n",
    "backend/tests/integration/test_int.py": "from meho_backplane.schemas import S\n",
    "backend/alembic/versions/0001_x.py": "",
}

# Test items of the fake suite (pytest test ids), as the junit files list them.
ITEMS = [
    "tests/test_api.py::test_api",
    "tests/test_contract.py::test_contract",
    "tests/test_core.py::test_core[a]",
    "tests/test_core.py::test_core[b]",
    "tests/test_core.py::test_core[c]",
    "tests/test_service.py::TestService::test_run[x]",
    "tests/test_service.py::TestService::test_run[y]",
    "tests/test_unrelated.py::test_u",
    "tests/test_schemas_direct.py::test_s",
    "tests/test_via_helper.py::test_h",
    "tests/integration/test_int.py::test_i",
    "tests/migrations/test_m.py::test_m",
]

# Source file -> coverage test names that ran it. ``schemas`` and ``boot``
# run only on import, so no test name is linked to them.
CONTEXTS = {
    f"{SRC}/__init__.py": set(),
    f"{SRC}/core.py": {"tests.test_core.test_core"},
    f"{SRC}/schemas.py": set(),
    f"{SRC}/service.py": {"tests.test_service.TestService.test_run"},
    f"{SRC}/api.py": {"tests.test_unrelated.test_u", "tests.integration.test_int.test_i"},
    f"{SRC}/boot.py": set(),
}


def _write_tree(root: Path, tree: dict[str, str]) -> None:
    for rel, text in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _map(made_at: datetime = NOW) -> dict[str, Any]:
    return test_map.build_map(CONTEXTS, ITEMS, "abc1234def", made_at)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    _write_tree(root, TREE)
    (root / "map.json").write_text(json.dumps(_map()), encoding="utf-8")
    monkeypatch.setattr(connector_select, "CONTRACT_TESTS", (CONTRACT,))
    return root


def _pick(repo: Path, changed: list[str], now: datetime = NOW) -> test_map.Picked:
    return test_map.pick(changed, repo / "map.json", repo, now)


# --- build --------------------------------------------------------------------


def test_build_map_links_files_to_test_functions() -> None:
    result = _map()
    assert result["format"] == test_map.MAP_FORMAT
    assert result["commit"] == "abc1234def"
    assert result["made_at"] == "2026-11-02T03:00:00Z"
    # The integration and migration tests run in other jobs: left out.
    assert result["total_tests"] == 10
    tests = [test for test, _count in result["tests"]]
    assert not [t for t in tests if t.startswith(("tests/integration/", "tests/migrations/"))]
    assert dict(map(tuple, result["tests"]))["tests/test_core.py::test_core"] == 3
    files = {path: [tests[i] for i in ids] for path, ids in result["files"].items()}
    assert files[f"{SRC}/core.py"] == ["tests/test_core.py::test_core"]
    assert files[f"{SRC}/api.py"] == ["tests/test_unrelated.py::test_u"]
    # Known to the map, but no test ran it.
    assert files[f"{SRC}/schemas.py"] == []


def test_build_map_refuses_data_without_test_names() -> None:
    with pytest.raises(ValueError, match="no test names"):
        test_map.build_map({f"{SRC}/core.py": set()}, ITEMS, "abc", NOW)


def test_junit_test_ids_from_a_real_pytest_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_tree(
        project,
        {
            "pytest.ini": "[pytest]\n",
            "tests/__init__.py": "",
            "tests/_base.py": "class BaseCase:\n    def test_inherited(self):\n        pass\n",
            "tests/sub/__init__.py": "",
            "tests/sub/test_a.py": (
                "import pytest\n"
                "from tests._base import BaseCase\n\n\n"
                "@pytest.mark.parametrize('n', [1, 2])\n"
                "def test_p(n):\n    pass\n\n\n"
                "class TestK:\n    def test_m(self):\n        pass\n\n\n"
                "class TestSub(BaseCase):\n    pass\n"
            ),
        },
    )
    junit = tmp_path / "junit.xml"
    env = {"PATH": "/usr/bin:/bin", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    ids = test_map.junit_test_ids([junit], project)

    assert sorted(ids) == [
        "tests/sub/test_a.py::TestK::test_m",
        "tests/sub/test_a.py::TestSub::test_inherited",
        "tests/sub/test_a.py::test_p[1]",
        "tests/sub/test_a.py::test_p[2]",
    ]


def test_coverage_contexts_reads_test_names(tmp_path: Path) -> None:
    from coverage import CoverageData

    data_file = tmp_path / ".coverage"
    data = CoverageData(str(data_file))
    data.set_context("tests.test_core.test_core")
    data.add_lines({"src/meho_backplane/core.py": [1, 2]})
    data.set_context("")
    data.add_lines({"src/meho_backplane/schemas.py": [1]})
    data.write()

    assert test_map.coverage_contexts(str(data_file)) == {
        f"{SRC}/core.py": {"tests.test_core.test_core"},
        f"{SRC}/schemas.py": set(),
    }


def test_build_command_writes_the_map(tmp_path: Path) -> None:
    from coverage import CoverageData

    _write_tree(tmp_path, {"tests/__init__.py": "", "tests/test_core.py": ""})
    data = CoverageData(str(tmp_path / ".coverage"))
    data.set_context("tests.test_core.test_core")
    data.add_lines({"src/meho_backplane/core.py": [1]})
    data.write()
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_core" name="test_core[a]"/>'
        '<testcase classname="tests.test_core" name="test_core[b]"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    out = tmp_path / "unit-test-map.json"

    code = test_map._main(
        [
            "build",
            "--data",
            str(tmp_path / ".coverage"),
            "--commit",
            "abc",
            "--out",
            str(out),
            "--backend-root",
            str(tmp_path),
            str(junit),
        ]
    )

    assert code == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["tests"] == [["tests/test_core.py::test_core", 2]]
    assert result["files"] == {f"{SRC}/core.py": [0]}


# --- pick ---------------------------------------------------------------------


def test_changed_source_file_picks_the_tests_that_ran_it(repo: Path) -> None:
    picked = _pick(repo, [f"{SRC}/core.py", "docs/notes.md", "backend/README.md"])

    assert picked.tests == [CONTRACT, "tests/test_core.py::test_core"]
    assert picked.line == "test map from 2026-11-02 (commit abc1234): 4 of 10 tests"


def test_schema_file_picks_the_tests_of_the_modules_that_import_it(repo: Path) -> None:
    """No test ran ``schemas.py``: import analysis picks the tests instead."""
    picked = _pick(repo, [f"{SRC}/schemas.py"])

    assert picked.tests == [
        # Imports api, which imports service, which imports schemas.
        "tests/test_api.py",
        CONTRACT,
        # Imports schemas itself.
        "tests/test_schemas_direct.py",
        # Imports a test helper that imports schemas.
        "tests/test_via_helper.py",
        # The tests the map links to the importers service and api.
        "tests/test_service.py::TestService::test_run",
        "tests/test_unrelated.py::test_u",
    ]
    assert "test_core" not in str(picked.tests)
    assert picked.line.endswith(": 7 of 10 tests")


def test_deleted_schema_file_still_picks_the_modules_that_import_it(repo: Path) -> None:
    (repo / SRC / "schemas.py").unlink()

    picked = _pick(repo, [f"{SRC}/schemas.py"])

    assert "tests/test_schemas_direct.py" in picked.tests
    assert "tests/test_api.py" in picked.tests


def test_conftest_that_imports_the_changed_file_means_the_full_suite(repo: Path) -> None:
    picked = _pick(repo, [f"{SRC}/boot.py"])

    assert picked.tests == []
    assert picked.line == (f"full suite: backend/tests/conftest.py imports {SRC}/boot.py")


def test_migration_means_the_full_suite(repo: Path) -> None:
    picked = _pick(repo, [f"{SRC}/core.py", "backend/alembic/versions/0001_x.py"])

    assert picked.tests == []
    assert picked.line == "full suite: a migration changed (backend/alembic/versions/0001_x.py)"


@pytest.mark.parametrize(
    "path",
    [
        f"{SRC}/templates/page.html",
        "backend/pyproject.toml",
        "backend/uv.lock",
        ".github/workflows/ci.yml",
        "cli/internal/api/client.gen.go",
    ],
)
def test_file_the_map_cannot_judge_means_the_full_suite(repo: Path, path: str) -> None:
    picked = _pick(repo, [f"{SRC}/core.py", path])

    assert picked.tests == []
    assert picked.line == f"full suite: the test map cannot judge {path}"


@pytest.mark.parametrize(
    "path",
    ["backend/tests/conftest.py", "backend/tests/_helpers.py", "backend/tests/data/x.json"],
)
def test_test_helper_means_the_full_suite(repo: Path, path: str) -> None:
    picked = _pick(repo, [path])

    assert picked.line == f"full suite: a test helper or test data changed ({path})"


def test_new_source_file_means_the_full_suite(repo: Path) -> None:
    _write_tree(repo, {f"{SRC}/new.py": "Z = 3\n"})

    picked = _pick(repo, [f"{SRC}/new.py"])

    assert picked.line == f"full suite: the test map does not know {SRC}/new.py"


def test_changed_test_file_picks_its_own_tests(repo: Path) -> None:
    _write_tree(repo, {"backend/tests/test_new.py": "def test_a():\n    pass\n"})

    picked = _pick(repo, ["backend/tests/test_unrelated.py", "backend/tests/test_new.py"])

    assert picked.tests == [CONTRACT, "tests/test_new.py", "tests/test_unrelated.py"]
    # The new file is not in the map: its test functions are counted.
    assert picked.line.endswith(": 3 of 10 tests")


def test_other_suites_and_deleted_test_files_pick_nothing(repo: Path) -> None:
    picked = _pick(
        repo,
        [
            "backend/tests/integration/test_int.py",
            "backend/tests/migrations/test_m.py",
            "backend/tests/test_gone.py",
        ],
    )

    assert picked.tests == [CONTRACT]


def test_renamed_test_picks_its_whole_file(repo: Path) -> None:
    """The map names a test the file no longer has: pytest must not get its id."""
    (repo / "backend/tests/test_service.py").write_text(
        "class TestService:\n    def test_new(self):\n        pass\n", encoding="utf-8"
    )

    picked = _pick(repo, [f"{SRC}/service.py"])

    assert picked.tests == [CONTRACT, "tests/test_service.py"]


def test_inherited_test_in_the_same_file_keeps_its_id(repo: Path) -> None:
    (repo / "backend/tests/test_service.py").write_text(
        "class Base:\n    def test_run(self):\n        pass\n\n\n"
        "class TestService(Base):\n    pass\n",
        encoding="utf-8",
    )

    picked = _pick(repo, [f"{SRC}/service.py"])

    assert picked.tests == [CONTRACT, "tests/test_service.py::TestService::test_run"]


def test_test_of_a_deleted_test_file_is_dropped(repo: Path) -> None:
    (repo / "backend/tests/test_core.py").unlink()

    picked = _pick(repo, [f"{SRC}/core.py"])

    assert picked.tests == [CONTRACT]


def test_too_many_tests_means_the_full_suite(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_map, "MAX_TESTS", 4)

    picked = _pick(repo, [f"{SRC}/core.py"])

    assert picked.tests == []
    assert picked.line == "full suite: the test map picked 4 tests, the limit is 3"


def test_old_map_means_the_full_suite(repo: Path) -> None:
    assert _pick(repo, [f"{SRC}/core.py"], NOW + timedelta(days=6, hours=23)).tests
    picked = _pick(repo, [f"{SRC}/core.py"], NOW + timedelta(days=7, minutes=1))

    assert picked.line == "full suite: the test map is older than 7 days (made 2026-11-02)"


def test_no_map_or_another_format_means_the_full_suite(repo: Path) -> None:
    assert test_map.pick([f"{SRC}/core.py"], repo / "missing.json", repo, NOW).line == (
        "full suite: no test map"
    )
    (repo / "map.json").write_text(json.dumps({"format": 99}), encoding="utf-8")

    assert _pick(repo, [f"{SRC}/core.py"]).line == ("full suite: the test map has format 99, not 1")


def test_empty_diff_means_the_full_suite(repo: Path) -> None:
    assert _pick(repo, ["", "  "]).line == "full suite: no changed files"


def test_missing_contract_file_means_the_full_suite(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connector_select, "CONTRACT_TESTS", (CONTRACT, "tests/test_gone.py"))

    picked = _pick(repo, [f"{SRC}/core.py"])

    assert picked.line == "full suite: contract test files not found: tests/test_gone.py"


def test_pick_reads_the_artifact_zip(repo: Path) -> None:
    archive = repo / "map.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(repo / "map.json", test_map.MAP_FILE)

    picked = test_map.pick([f"{SRC}/core.py"], archive, repo, NOW)

    assert picked.tests == [CONTRACT, "tests/test_core.py::test_core"]


def test_pick_command_writes_the_list_only_for_a_selected_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(test_map, "datetime", _FrozenDatetime)
    out = repo / "selection.txt"
    args = ["pick", "--map", str(repo / "map.json"), "--out", str(out), "--repo-root", str(repo)]

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{SRC}/core.py\n"))
    assert test_map._main(args) == 0
    assert out.read_text(encoding="utf-8") == f"{CONTRACT}\ntests/test_core.py::test_core\n"
    assert "test map from 2026-11-02" in capfd.readouterr().out

    out.unlink()
    (repo / "map.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{SRC}/core.py\n"))
    assert test_map._main(args) == 0
    assert not out.exists()
    assert capfd.readouterr().out.startswith("full suite: the test map tool failed (")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        return cls(2026, 11, 2, 4, 0, tzinfo=UTC)


# --- ci.yml -------------------------------------------------------------------


def _jobs() -> dict[str, Any]:
    workflow = cast("dict[str, Any]", yaml.safe_load(CI_YML.read_text(encoding="utf-8")))
    return cast("dict[str, Any]", workflow["jobs"])


def _step(job: str, *, step_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    for step in _jobs()[job]["steps"]:
        if (step_id and step.get("id") == step_id) or (name and step.get("name") == name):
            return cast("dict[str, Any]", step)
    raise AssertionError(f"step not found in job {job}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _artifact(artifact_id: int, *, branch: str = "main", head_repo: int = 1) -> dict[str, Any]:
    """One entry of the GitHub "list artifacts" answer (repo id 1 is this repo)."""
    return {
        "id": artifact_id,
        "name": "unit-test-map",
        "expired": False,
        "workflow_run": {
            "head_branch": branch,
            "head_repository_id": head_repo,
            "repository_id": 1,
        },
    }


def _run_map_step(
    tmp_path: Path,
    event: str,
    changed: dict[str, str],
    artifacts: list[dict[str, Any]] | None = None,
) -> tuple[str, str, Path, list[str]]:
    """Run the real step ``map`` of the ``changes`` job.

    A fake ``gh`` lists ``artifacts`` (default: one from a run of this repo
    on main) and applies the step's own ``--jq`` filter with the real ``jq``.
    Any download gives the zip of the fake map. Returns the step's stdout,
    the value of ``test_map``, the folder of the picked list and the ``gh``
    calls.
    """
    repo = tmp_path / "repo"
    _write_tree(repo, TREE)
    for name in ("_unit_test_map.py", "_unit_connector_select.py"):
        shutil.copy(BACKEND_DIR / "tests" / name, repo / "backend" / "tests" / name)
    # The real tool reads the real contract set: make those files exist.
    _write_tree(repo, {f"backend/{name}": "" for name in connector_select.CONTRACT_TESTS})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _write_tree(repo, changed)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    archive = tmp_path / "map.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(test_map.MAP_FILE, json.dumps(_map(datetime.now(UTC))))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls"
    listing = tmp_path / "artifacts.json"
    listing.write_text(
        json.dumps({"artifacts": [_artifact(4242)] if artifacts is None else artifacts}),
        encoding="utf-8",
    )
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$2" >> "{calls}"\n'
        'case "$2" in\n'
        f'  */zip) cat "{archive}" ;;\n'
        f'  *) [ "$3" = "--jq" ] && jq -r "$4" "{listing}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(_step("changes", step_id="map")["run"], encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "example/repo",
        "RUNNER_TEMP": str(runner_temp),
        "EVENT_NAME": event,
    }
    run = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    gh_calls = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    if event != "pull_request":
        assert not gh_calls, "no artifact lookup outside a pull request"
    return run.stdout, outputs["test_map"], runner_temp / "unit-test-map", gh_calls


needs_git = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("jq") is None, reason="git or jq not installed"
)


@needs_git
def test_ci_pull_request_with_a_schema_change_runs_the_picked_tests(tmp_path: Path) -> None:
    stdout, selected, folder, _calls = _run_map_step(
        tmp_path, "pull_request", {f"{SRC}/schemas.py": "class S:\n    x = 1\n"}
    )

    assert selected == "true"
    picked = (folder / "selection.txt").read_text(encoding="utf-8").splitlines()
    assert "tests/test_schemas_direct.py" in picked
    assert "tests/test_api.py" in picked
    assert set(connector_select.CONTRACT_TESTS) <= set(picked)
    assert "test map from " in stdout
    assert stdout.count("\n") == 2, "one line, plus the same line as a notice"


@needs_git
def test_ci_pull_request_with_a_migration_runs_the_full_suite(tmp_path: Path) -> None:
    stdout, selected, _folder, _calls = _run_map_step(
        tmp_path, "pull_request", {"backend/alembic/versions/0002_y.py": "x = 1\n"}
    )

    assert selected == "false"
    assert "full suite: a migration changed (backend/alembic/versions/0002_y.py)" in stdout


@needs_git
@pytest.mark.parametrize("event", ["push", "merge_group", "schedule"])
def test_ci_push_and_merge_group_never_use_the_map(tmp_path: Path, event: str) -> None:
    stdout, selected, _folder, _calls = _run_map_step(
        tmp_path, event, {f"{SRC}/schemas.py": "class S:\n    x = 1\n"}
    )

    assert selected == "false"
    assert f"full suite: not a pull request ({event})" in stdout


@needs_git
def test_ci_map_comes_only_from_a_run_of_this_repo_on_main(tmp_path: Path) -> None:
    newest_first = [
        _artifact(9001, head_repo=2),
        _artifact(9002, branch="feature"),
        _artifact(4242),
    ]
    _stdout, selected, _folder, calls = _run_map_step(
        tmp_path, "pull_request", {f"{SRC}/schemas.py": "class S:\n    x = 1\n"}, newest_first
    )

    assert selected == "true"
    assert calls[-1] == "repos/example/repo/actions/artifacts/4242/zip"


@needs_git
def test_ci_no_map_from_a_run_of_this_repo_on_main_means_the_full_suite(tmp_path: Path) -> None:
    stdout, selected, _folder, calls = _run_map_step(
        tmp_path,
        "pull_request",
        {f"{SRC}/schemas.py": "class S:\n    x = 1\n"},
        [_artifact(9001, head_repo=2)],
    )

    assert selected == "false"
    assert "full suite: no test map from a nightly run of this repo on main" in stdout
    assert not any(call.endswith("/zip") for call in calls)


def test_ci_map_step_runs_only_on_a_pull_request_that_needs_the_shards() -> None:
    condition = _step("changes", step_id="map")["if"]
    assert "github.event_name == 'pull_request'" in condition
    assert "steps.detect.outputs.docs_only == 'false'" in condition
    assert "steps.detect.outputs.backend_python == 'true'" in condition
    assert "steps.detect.outputs.single_connector == ''" in condition

    changes = _jobs()["changes"]
    assert changes["permissions"] == {"contents": "read", "actions": "read"}
    output = changes["outputs"]["test_map"]
    assert "steps.map.outputs.test_map == 'true'" in output
    assert "steps.upload-selection.outcome == 'success'" in output
    upload = _step("changes", step_id="upload-selection")
    assert upload["with"]["name"] == "unit-test-selection"


def test_ci_unit_shards_run_the_picked_tests_in_one_job() -> None:
    shard = _jobs()["python-unit-shard"]
    matrix = shard["strategy"]["matrix"]["shard"]
    assert "needs.changes.outputs.test_map == 'true'" in matrix
    assert "'[1]'" in matrix
    assert "Python unit tests picked by the test map" in shard["name"]

    names = [step.get("name", "") for step in shard["steps"]]
    download = _step("python-unit-shard", name="Download the picked unit tests")
    assert download["if"] == "needs.changes.outputs.test_map == 'true'"
    assert download["with"]["name"] == "unit-test-selection"
    pytest_index = next(i for i, name in enumerate(names) if name.startswith("Pytest"))
    assert names.index("Download the picked unit tests") < pytest_index

    pytest_step = shard["steps"][pytest_index]
    assert pytest_step["env"]["TEST_MAP"] == "${{ needs.changes.outputs.test_map }}"
    assert 'elif [ "${TEST_MAP}" = "true" ]; then' in pytest_step["run"]
    assert "unit-test-selection/selection.txt" in pytest_step["run"]

    times = _step("python-unit-shard", name="Upload per-test times (input for the shard split)")
    assert "needs.changes.outputs.test_map != 'true'" in times["if"]

    assert _jobs()["python-unit-lane"]["name"] == "Python (ruff + mypy + pytest)"


def test_ci_nightly_run_makes_the_map() -> None:
    assert "dynamic_context = test_function" in (BACKEND_DIR / ".coveragerc.ci").read_text()

    pytest_step = _step("python-coverage", name="Pytest (unit) with offline coverage data")
    assert pytest_step["env"]["COVERAGE_CORE"] == "ctrace", "sysmon cannot record test names"
    assert pytest_step["timeout-minutes"] == 45
    assert '--junitxml="pytest-coverage-junit-${SHARD}.xml"' in pytest_step["run"]
    upload = _step("python-coverage", name="Upload shard coverage data (parallel files)")
    assert "backend/pytest-coverage-junit-*.xml" in upload["with"]["path"]

    build = _step("python-coverage-combine", step_id="test-map")
    assert "github.event_name == 'schedule'" in build["if"]
    assert "pull_request" not in build["if"], "a labelled pull request never makes a map"
    assert "tests/_unit_test_map.py build" in build["run"]
    store = _step("python-coverage-combine", name="Upload the unit test map")
    assert store["with"]["name"] == "unit-test-map"
    assert store["with"]["path"] == f"backend/{test_map.MAP_FILE}"
    assert store["with"]["retention-days"] > test_map.MAX_AGE_DAYS
