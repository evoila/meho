# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the list of required spec-shelf files (#5575).

CI checks out a few vendor API files from the private spec shelf. The list of
those files lives in ``ci/spec-shelf/``, one small file per connector or
product version, so a new connector adds a file and does not edit
``.github/workflows/ci.yml``. ``scripts/ci/spec_shelf.sh`` reads the folder:
``paths`` prints the sparse-checkout folders, ``verify`` checks that each file
is there.

These tests run the real script and the real workflow step, against the real
lists and against small made-up lists and a made-up shelf. The shell starts
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
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "spec_shelf.sh"
_LISTS = _REPO_ROOT / "ci" / "spec-shelf"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_JOBS = ("python-unit-shard", "python-integration-shard")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _run(*args: str, cwd: Path = _REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _file_entries(path: Path) -> list[str]:
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def _entries(lists: Path) -> list[str]:
    return [e for path in sorted(lists.glob("*.txt")) for e in _file_entries(path)]


def _write_lists(tmp_path: Path, files: dict[str, str]) -> Path:
    lists = tmp_path / "lists"
    lists.mkdir()
    for name, text in files.items():
        (lists / name).write_text(text)
    return lists


def _make_shelf(tmp_path: Path, entries: list[str]) -> Path:
    shelf = tmp_path / "shelf"
    for entry in entries:
        path = shelf / "docs" / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    return shelf


def _steps(job: str) -> list[dict[str, Any]]:
    workflow = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))
    return cast("list[dict[str, Any]]", workflow["jobs"][job]["steps"])


# --- The real lists -----------------------------------------------------------


def test_real_lists_give_one_folder_per_list_file() -> None:
    result = _run("paths", "ci/spec-shelf")
    assert result.returncode == 0, result.stderr
    folders = result.stdout.splitlines()
    assert folders == sorted({f"docs/{e.split('/', 1)[0]}" for e in _entries(_LISTS)})
    # One list file per shelf folder, named after it.
    assert folders == sorted(f"docs/{p.stem}" for p in _LISTS.glob("*.txt"))


def test_each_list_file_names_only_its_own_folder() -> None:
    for path in sorted(_LISTS.glob("*.txt")):
        own = _file_entries(path)
        assert own, f"{path.name} lists no file"
        assert all(e.startswith(f"{path.stem}/") for e in own), path.name


def test_workflow_names_no_listed_file() -> None:
    text = _CI_WORKFLOW.read_text()
    for entry in _entries(_LISTS):
        assert entry not in text
        assert entry.rsplit("/", 1)[1] not in text, entry


# --- The workflow wiring ------------------------------------------------------


@pytest.mark.parametrize("job", _JOBS)
def test_both_jobs_take_the_paths_and_the_check_from_the_script(job: str) -> None:
    steps = _steps(job)
    names = [s.get("name", "") for s in steps]
    paths_at = names.index("List the spec-shelf paths")
    checkout_at = next(i for i, n in enumerate(names) if n.startswith("Checkout vendor spec-shelf"))
    verify_at = names.index("Verify spec-shelf provisioned the pinned specs")
    assert paths_at < checkout_at < verify_at

    paths_step, checkout, verify = steps[paths_at], steps[checkout_at], steps[verify_at]
    gate = "env.SPEC_SHELF_TOKEN_PRESENT == 'true'"
    assert paths_step["if"] == checkout["if"] == verify["if"] == gate
    assert paths_step["id"] == "spec-shelf-paths"
    assert "scripts/ci/spec_shelf.sh paths ci/spec-shelf" in paths_step["run"]
    assert checkout["with"]["sparse-checkout"] == "${{ steps.spec-shelf-paths.outputs.paths }}"
    assert verify["run"].strip() == "scripts/ci/spec_shelf.sh verify ci/spec-shelf spec-shelf"
    assert (
        paths_step["working-directory"] == verify["working-directory"] == "${{ github.workspace }}"
    )


def test_paths_step_writes_a_multiline_output(tmp_path: Path) -> None:
    script = next(s for s in _steps(_JOBS[0]) if s.get("id") == "spec-shelf-paths")["run"]
    output = tmp_path / "github_output"
    output.write_text("")
    step = tmp_path / "step.sh"
    step.write_text(script)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(step)],
        cwd=_REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    assert lines[0] == "paths<<SPEC_SHELF_PATHS_EOF"
    assert lines[-1] == "SPEC_SHELF_PATHS_EOF"
    assert lines[1:-1] == _run("paths", "ci/spec-shelf").stdout.splitlines()


# --- verify -------------------------------------------------------------------


def test_verify_passes_and_prints_one_line_per_file(tmp_path: Path) -> None:
    entries = _entries(_LISTS)
    shelf = _make_shelf(tmp_path, entries)
    result = _run("verify", str(_LISTS), str(shelf))
    assert result.returncode == 0, result.stdout + result.stderr
    ok = [line for line in result.stdout.splitlines() if line.startswith("spec-shelf OK: ")]
    assert ok[:-1] == [f"spec-shelf OK: {shelf}/docs/{e}" for e in sorted(set(entries))]
    assert ok[-1] == f"spec-shelf OK: all {len(set(entries))} listed files present."


def test_verify_fails_with_the_drift_error_for_each_missing_file(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path,
        {"a-1.0.txt": "a-1.0/one.json\na-1.0/sub/two.yaml\n", "b-2.txt": "b-2/three.tsv\n"},
    )
    shelf = _make_shelf(tmp_path, ["a-1.0/one.json"])
    result = _run("verify", str(lists), "shelf", cwd=tmp_path)
    assert result.returncode == 1
    errors = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert errors == [
        f"::error title=Spec-shelf layout drift::shelf/docs/{e} missing after the spec-shelf "
        "checkout — the real-spec lanes would silently skip. Fix the shelf layout or the "
        f"list in {lists}/."
        for e in ("a-1.0/sub/two.yaml", "b-2/three.tsv")
    ]
    assert "spec-shelf OK: shelf/docs/a-1.0/one.json" in result.stdout
    assert "listed files present" not in result.stdout
    assert shelf.is_dir()


def test_verify_fails_when_the_shelf_is_not_there(tmp_path: Path) -> None:
    result = _run("verify", str(_LISTS), str(tmp_path / "no-shelf"))
    assert result.returncode == 1
    assert "Spec-shelf layout drift" in result.stdout


def test_a_folder_in_place_of_a_file_is_missing(tmp_path: Path) -> None:
    lists = _write_lists(tmp_path, {"a.txt": "a/spec.json\n"})
    shelf = tmp_path / "shelf"
    (shelf / "docs" / "a" / "spec.json").mkdir(parents=True)
    assert _run("verify", str(lists), str(shelf)).returncode == 1


# --- The list format ----------------------------------------------------------


def test_comments_blank_lines_crlf_and_doubles_are_handled(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path,
        {
            "b.txt": "# the b product\n\nb/x.json   # main spec\r\nb/x.json\n",
            "a.txt": "a/deep/y.yaml",  # no newline at the end
        },
    )
    result = _run("paths", str(lists))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["docs/a", "docs/b"]
    shelf = _make_shelf(tmp_path, ["a/deep/y.yaml", "b/x.json"])
    verify = _run("verify", str(lists), str(shelf))
    assert verify.returncode == 0, verify.stdout
    assert verify.stdout.count("spec-shelf OK: ") == 3


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({}, "no *.txt list file"),
        ({"a.txt": "# only a comment\n\n"}, "no file listed"),
        ({"a.txt": "/abs/spec.json\n"}, "bad path"),
        ({"a.txt": "a/../../etc/spec.json\n"}, "bad path"),
        ({"a.txt": "./a/spec.json\n"}, "bad path"),
        ({"a.txt": "spec.json\n"}, "has no folder"),
    ],
)
@pytest.mark.parametrize("command", ["paths", "verify"])
def test_a_bad_list_fails_both_commands(
    tmp_path: Path, files: dict[str, str], message: str, command: str
) -> None:
    lists = _write_lists(tmp_path, files)
    args = [command, str(lists)] + ([str(tmp_path)] if command == "verify" else [])
    result = _run(*args)
    assert result.returncode == 1
    assert "::error title=Spec-shelf list error::" in result.stderr
    assert message in result.stderr
    assert "spec-shelf OK" not in result.stdout


@pytest.mark.parametrize(
    "args", [[], ["paths"], ["verify", "ci/spec-shelf"], ["list", "ci/spec-shelf"]]
)
def test_wrong_usage_fails(args: list[str]) -> None:
    result = _run(*args)
    assert result.returncode == 1
    assert "usage:" in result.stderr
