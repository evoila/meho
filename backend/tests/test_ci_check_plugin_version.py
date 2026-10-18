# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_plugin_version.py`` (#4004).

Claude Code gives users a new copy of a plugin only when its version goes
up. The script fails when the plugin version is not the same in
``plugin.json`` and ``marketplace.json``, or when a change to the plugin
does not raise the version. The tests run the script as a subprocess
against small throw-away git repositories, plus once against this
checkout, so a version mismatch on ``main`` also fails the unit lane.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_plugin_version.py"
_PLUGIN = "clients/claude-code-plugin"


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write_versions(
    root: Path, plugin: str, entry: str | None = None, meta: str | None = None
) -> None:
    manifest = root / _PLUGIN / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "meho", "version": plugin}))
    market = root / ".claude-plugin" / "marketplace.json"
    market.parent.mkdir(parents=True, exist_ok=True)
    market.write_text(
        json.dumps(
            {
                "name": "meho",
                "metadata": {"version": meta or plugin},
                "plugins": [{"name": "meho", "version": entry or plugin}],
            }
        )
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose first commit has the plugin at version 0.1.0."""
    _git(tmp_path, "init", "-q")
    _write_versions(tmp_path, "0.1.0")
    skill = tmp_path / _PLUGIN / "skills" / "a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("one\n")
    test_file = tmp_path / _PLUGIN / "test" / "a.test.mjs"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("// one\n")
    _commit(tmp_path, "base")
    return tmp_path


def test_this_checkout_shows_one_version() -> None:
    result = _run(_REPO_ROOT)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("entry", "meta"),
    [("0.1.1", None), (None, "0.1.1")],
    ids=["marketplace-entry", "marketplace-metadata"],
)
def test_versions_that_disagree_fail(repo: Path, entry: str | None, meta: str | None) -> None:
    _write_versions(repo, "0.1.0", entry=entry, meta=meta)
    result = _run(repo)
    assert result.returncode == 1
    assert "not the same in every place" in result.stderr


def test_plugin_change_without_a_new_version_fails(repo: Path) -> None:
    (repo / _PLUGIN / "skills" / "a" / "SKILL.md").write_text("two\n")
    _commit(repo, "change")
    result = _run(repo, "--base", "HEAD^1")
    assert result.returncode == 1
    assert "still 0.1.0" in result.stderr
    assert f"{_PLUGIN}/skills/a/SKILL.md" in result.stderr


def test_plugin_change_with_a_lower_version_fails(repo: Path) -> None:
    (repo / _PLUGIN / "skills" / "a" / "SKILL.md").write_text("two\n")
    _write_versions(repo, "0.0.9")
    _commit(repo, "change")
    assert _run(repo, "--base", "HEAD^1").returncode == 1


def test_plugin_change_with_a_new_version_passes(repo: Path) -> None:
    (repo / _PLUGIN / "skills" / "a" / "SKILL.md").write_text("two\n")
    _write_versions(repo, "0.1.1")
    _commit(repo, "change")
    result = _run(repo, "--base", "HEAD^1")
    assert result.returncode == 0, result.stderr
    assert "0.1.1" in result.stdout


@pytest.mark.parametrize(
    "path",
    [f"{_PLUGIN}/test/a.test.mjs", "docs/other.md"],
    ids=["plugin-test-file", "outside-plugin"],
)
def test_change_users_do_not_get_needs_no_new_version(repo: Path, path: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("two\n")
    _commit(repo, "change")
    result = _run(repo, "--base", "HEAD^1")
    assert result.returncode == 0, result.stderr


def test_version_that_is_not_x_y_z_fails(repo: Path) -> None:
    _write_versions(repo, "0.1")
    result = _run(repo)
    assert result.returncode == 1
    assert "not of the form X.Y.Z" in result.stderr
