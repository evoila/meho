# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/release/check_connector_kit_release.py``.

The release check compares the connector kit's name list at the last
release tag with the list now. When the kit changed, the release notes need
a ``### Connector kit`` section that names the new kit version, and the kit
version must have gone up far enough. The tests run the script as a
subprocess, with ``--old-names`` in place of git.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_SCRIPT = _repo_root() / "scripts" / "release" / "check_connector_kit_release.py"

_ENTRY = {"kind": "function", "inputs": ["value: str"], "returns": "str"}
_CHANGED = {"kind": "function", "inputs": ["value: str", "*", "limit: int"], "returns": "str"}


def _names(version: str, **entries: dict[str, Any]) -> dict[str, Any]:
    return {"kit_version": version, "names": entries}


def _changelog(kit_section: str | None) -> str:
    lines = ["# Changelog", "", "## [Unreleased]", "", "## [0.50.0] - 2026-11-20", ""]
    lines += ["### Added", "", "- Something new (#1).", ""]
    if kit_section is not None:
        lines += ["### Connector kit", "", kit_section, ""]
    lines += ["## [0.49.0] - 2026-11-10", "", "### Connector kit", "", "- Kit version 9.9.", ""]
    return "\n".join(lines)


def _run(
    tmp_path: Path,
    old: dict[str, Any],
    new: dict[str, Any],
    kit_section: str | None,
) -> subprocess.CompletedProcess[str]:
    old_file = tmp_path / "old.json"
    new_file = tmp_path / "new.json"
    changelog = tmp_path / "CHANGELOG.md"
    old_file.write_text(json.dumps(old))
    new_file.write_text(json.dumps(new))
    changelog.write_text(_changelog(kit_section))
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--version",
            "0.50.0",
            "--old-names",
            str(old_file),
            "--new-names",
            str(new_file),
            "--changelog",
            str(changelog),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_unchanged_kit_needs_no_section(tmp_path: Path) -> None:
    kit = _names("1.0", a=_ENTRY)
    result = _run(tmp_path, kit, kit, None)
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_added_name_with_section_and_new_version_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.1 adds `b`.",
    )
    assert result.returncode == 0, result.stderr


def test_changed_kit_without_section_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, _names("1.0", a=_ENTRY), _names("1.1", a=_ENTRY, b=_ENTRY), None)
    assert result.returncode == 1
    assert "### Connector kit" in result.stderr
    assert "1.1" in result.stderr


def test_section_naming_another_version_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.10 adds `b`.",
    )
    assert result.returncode == 1
    assert "does not name the new kit version 1.1" in result.stderr


def test_changed_name_needs_the_first_number(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_CHANGED),
        "- Kit version 1.1 changes `a`.",
    )
    assert result.returncode == 1
    assert "first number" in result.stderr


def test_added_name_needs_a_new_version(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.0", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.0 adds `b`.",
    )
    assert result.returncode == 1
    assert "must go up" in result.stderr


def test_removing_a_name_that_was_not_old_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY, b=_ENTRY),
        _names("2.0", a=_ENTRY),
        "- Kit version 2.0 removes `b`.",
    )
    assert result.returncode == 1
    assert "did not mark them as old: b" in result.stderr


def test_removing_an_old_name_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.1", a=_ENTRY, b={**_ENTRY, "old": "use a"}),
        _names("2.0", a=_ENTRY),
        "- Kit version 2.0 removes `b`: use `a`.",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", ["old", "changelog"])
def test_unreadable_input_exits_2(tmp_path: Path, missing: str) -> None:
    kit = tmp_path / "kit.json"
    kit.write_text(json.dumps(_names("1.0", a=_ENTRY)))
    args = ["--old-names", str(kit), "--new-names", str(kit)]
    args += ["--changelog", str(tmp_path / "CHANGELOG.md")]
    if missing == "old":
        args[1] = str(tmp_path / "missing.json")
    else:
        (tmp_path / "CHANGELOG.md").unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--version", "0.50.0", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
