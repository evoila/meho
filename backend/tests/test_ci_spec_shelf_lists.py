# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the lists of required spec-shelf files.

``ci/spec-shelf/`` holds one small list per product version. Each line names
one vendor API file on the private spec shelf. ``scripts/ci/spec_shelf.sh``
reads the lists: ``paths`` prints the shelf folders that the two Python test
jobs check out, and ``verify`` fails the job when a listed file is missing.

The tests run the real script on made-up lists and a made-up shelf in
``tmp_path``. They also check that ``ci.yml`` names no shelf file itself, so
a new connector adds one list file and no line in the workflow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "spec_shelf.sh"
_LISTS = _REPO_ROOT / "ci" / "spec-shelf"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SHELF_JOBS = ("python-unit-shard", "python-integration-shard")
_DRIFT = "::error title=Spec-shelf layout drift::"
_LIST_ERROR = "::error title=Spec-shelf list error::"


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(_SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_lists(root: Path, lists: dict[str, str]) -> Path:
    folder = root / "lists"
    folder.mkdir()
    for name, body in lists.items():
        (folder / name).write_text(body)
    return folder


def _make_shelf(root: Path, files: list[str]) -> Path:
    shelf = root / "shelf"
    for rel in files:
        path = shelf / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("made-up spec\n")
    return shelf


def _real_files() -> list[str]:
    files: set[str] = set()
    for list_file in _LISTS.glob("*.txt"):
        for line in list_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                files.add(line)
    return sorted(files)


def _shelf_steps(job: str) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text()))
    steps = workflow["jobs"][job]["steps"]
    checkout = next(
        s for s in steps if str(s.get("name", "")).startswith("Checkout vendor spec-shelf")
    )
    verify = next(
        s for s in steps if s.get("name") == "Verify spec-shelf provisioned the pinned specs"
    )
    return checkout, verify


# --- the real lists -------------------------------------------------------


def test_real_lists_are_valid_and_each_names_one_shelf_folder() -> None:
    lists = sorted(_LISTS.glob("*.txt"))
    assert lists, "ci/spec-shelf/ has no list"
    for list_file in lists:
        folders = {
            "/".join(line.strip().split("/")[:2])
            for line in list_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert folders == {f"docs/{list_file.stem}"}, list_file.name
    result = _run("paths", _LISTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == sorted(f"docs/{p.stem}" for p in lists)


def test_real_lists_pass_on_a_shelf_with_every_file(tmp_path: Path) -> None:
    files = _real_files()
    shelf = _make_shelf(tmp_path, files)
    result = _run("verify", _LISTS, shelf)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"spec-shelf OK: {shelf}/{f}" for f in files]


@pytest.mark.parametrize("job", _SHELF_JOBS)
def test_ci_yml_reads_the_lists_through_the_script(job: str) -> None:
    checkout, verify = _shelf_steps(job)
    assert checkout["with"]["sparse-checkout"] == "${{ steps.spec-shelf-list.outputs.paths }}"
    assert "scripts/ci/spec_shelf.sh verify ci/spec-shelf spec-shelf" in verify["run"]
    workflow = cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text()))
    list_step = next(s for s in workflow["jobs"][job]["steps"] if s.get("id") == "spec-shelf-list")
    assert "scripts/ci/spec_shelf.sh paths ci/spec-shelf" in list_step["run"]
    assert list_step["if"] == checkout["if"] == verify["if"]


def test_ci_yml_names_no_shelf_file_or_folder() -> None:
    text = _WORKFLOW.read_text()
    for rel in _real_files():
        assert rel not in text, rel
        assert Path(rel).name not in text, rel
    for list_file in _LISTS.glob("*.txt"):
        assert f"docs/{list_file.stem}" not in text, list_file.name


# --- the script on made-up lists ------------------------------------------


def test_missing_file_fails_with_the_drift_error(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path, {"product-1.0.txt": "docs/product-1.0/a.json\ndocs/product-1.0/b.json\n"}
    )
    shelf = _make_shelf(tmp_path, ["docs/product-1.0/a.json"])
    result = _run("verify", lists, shelf)
    assert result.returncode == 1
    assert f"{_DRIFT}{shelf}/docs/product-1.0/b.json missing" in result.stdout
    assert "spec-shelf OK" not in result.stdout


def test_every_missing_file_is_named(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path,
        {"a-1.txt": "docs/a-1/x.yaml\n", "b-2.txt": "docs/b-2/y.yaml\n"},
    )
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    result = _run("verify", lists, shelf)
    assert result.returncode == 1
    assert result.stdout.count(_DRIFT) == 2


def test_folder_in_place_of_a_file_is_drift(tmp_path: Path) -> None:
    lists = _write_lists(tmp_path, {"p-1.txt": "docs/p-1/spec.json\n"})
    shelf = tmp_path / "shelf"
    (shelf / "docs" / "p-1" / "spec.json").mkdir(parents=True)
    result = _run("verify", lists, shelf)
    assert result.returncode == 1
    assert _DRIFT in result.stdout


def test_missing_shelf_fails(tmp_path: Path) -> None:
    lists = _write_lists(tmp_path, {"p-1.txt": "docs/p-1/spec.json\n"})
    result = _run("verify", lists, tmp_path / "no-shelf")
    assert result.returncode == 1
    assert _DRIFT in result.stdout


def test_comments_blank_lines_crlf_and_duplicates(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path,
        {
            "p-1.txt": "# the p 1 files\n\n  docs/p-1/b.json  \r\ndocs/p-1/a.json\r\n",
            "p-1-extra.txt": "docs/p-1/a.json",  # no final newline, and a name already listed
            "q-2.txt": "docs/q-2/sub/c.txt\n",
        },
    )
    paths = _run("paths", lists)
    assert paths.returncode == 0, paths.stderr
    assert paths.stdout.splitlines() == ["docs/p-1", "docs/q-2"]
    shelf = _make_shelf(tmp_path, ["docs/p-1/a.json", "docs/p-1/b.json", "docs/q-2/sub/c.txt"])
    verify = _run("verify", lists, shelf)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert verify.stdout.splitlines() == [
        f"spec-shelf OK: {shelf}/docs/p-1/a.json",
        f"spec-shelf OK: {shelf}/docs/p-1/b.json",
        f"spec-shelf OK: {shelf}/docs/q-2/sub/c.txt",
    ]


def test_other_files_in_the_list_folder_are_not_read(tmp_path: Path) -> None:
    lists = _write_lists(
        tmp_path, {"p-1.txt": "docs/p-1/a.json\n", "notes.md": "docs/z-9/never.json\n"}
    )
    result = _run("paths", lists)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["docs/p-1"]


@pytest.mark.parametrize("mode", ["paths", "verify"])
def test_missing_list_folder_fails(tmp_path: Path, mode: str) -> None:
    args = [tmp_path / "no-lists"] + ([tmp_path] if mode == "verify" else [])
    result = _run(mode, *args)
    assert result.returncode == 1
    assert _LIST_ERROR in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("mode", ["paths", "verify"])
def test_list_folder_without_lists_fails(tmp_path: Path, mode: str) -> None:
    lists = _write_lists(tmp_path, {"README.md": "docs/p-1/a.json\n"})
    args = [lists] + ([tmp_path] if mode == "verify" else [])
    result = _run(mode, *args)
    assert result.returncode == 1
    assert "no .txt list" in result.stderr


@pytest.mark.parametrize("mode", ["paths", "verify"])
def test_list_that_names_no_file_fails(tmp_path: Path, mode: str) -> None:
    lists = _write_lists(
        tmp_path, {"p-1.txt": "docs/p-1/a.json\n", "empty-2.txt": "# nothing yet\n\n"}
    )
    shelf = _make_shelf(tmp_path, ["docs/p-1/a.json"])
    args = [lists] + ([shelf] if mode == "verify" else [])
    result = _run(mode, *args)
    assert result.returncode == 1
    assert "empty-2.txt names no file" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "line",
    [
        "/docs/p-1/a.json",
        "p-1/a.json",
        "docs/p-1",
        "docs/p-1/",
        "docs//a.json",
        "docs/p-1/../../etc/passwd",
        "docs/../p-1/a.json",
        "docs/p-1/./a.json",
        "docs/p-1/two words.json",
        "spec-shelf/docs/p-1/a.json",
    ],
)
def test_bad_line_fails(tmp_path: Path, line: str) -> None:
    lists = _write_lists(tmp_path, {"p-1.txt": f"{line}\n"})
    result = _run("paths", lists)
    assert result.returncode == 1
    assert "bad line" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("args", [[], ["paths"], ["verify", "lists"], ["check", "lists"]])
def test_wrong_usage_fails(args: list[str]) -> None:
    result = _run(*args)
    assert result.returncode == 1
    assert "usage" in result.stderr
