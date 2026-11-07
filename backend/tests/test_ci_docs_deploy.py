# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the docs deploy of a tag (#3860).

``.github/workflows/docs-site.yml`` runs ``scripts/ci/docs_deploy.sh`` on
every ``v*`` tag. A final tag publishes under its minor and moves ``latest``
there. A release candidate (rc) tag must never change what readers of
``latest`` see: before its minor has a final tag it publishes under the
minor, after that under its own name.

The tests run the real script with the real mike against a bare ``origin``
repo in ``tmp_path``, with a tiny mkdocs site whose page names its tag. They
check the page content through ``latest`` on gh-pages, not only mike's
``versions.json``: ``latest`` is a symlink to a version folder, so a deploy
into that folder changes ``latest`` without touching the alias.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "docs_deploy.sh"
_MIKE = Path(sys.executable).parent / "mike"
# Git without the machine's own config (hooks, signing, default branch), so
# the test sees only what it sets up.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

pytestmark = pytest.mark.skipif(
    not _MIKE.exists() or shutil.which("git") is None,
    reason="mike (docs dependency group) or git not installed",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A docs repo whose ``origin`` is a bare repo, like the CI checkout."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "test")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "mkdocs.yml").write_text("site_name: test\n")
    (work / "docs").mkdir()
    return work


def _deploy(site: Path, tag: str) -> subprocess.CompletedProcess[str]:
    """Commit a page that names ``tag``, push the tag, run the script."""
    (site / "docs" / "index.md").write_text(f"Docs of {tag} end.\n")
    _git(site, "add", "-A")
    _git(site, "commit", "-q", "-m", tag)
    _git(site, "tag", tag)
    _git(site, "push", "-q", "origin", "main", tag)
    output = site / "github_output"
    output.write_text("")
    env = {**_GIT_ENV, "MIKE": str(_MIKE), "GITHUB_OUTPUT": str(output)}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(_SCRIPT), tag],
        cwd=site,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _published(site: Path) -> Path:
    """A fresh checkout of the remote gh-pages branch, as Pages serves it."""
    pages = site.parent / "pages"
    shutil.rmtree(pages, ignore_errors=True)
    _git(site.parent, "clone", "-q", "-b", "gh-pages", str(site.parent / "origin.git"), "pages")
    return pages


def _shows(pages: Path, version: str, tag: str) -> bool:
    """Whether the page of ``version`` is the one committed for ``tag``."""
    return f"Docs of {tag} end." in (pages / version / "index.html").read_text()


def _output_version(site: Path) -> str:
    return (site / "github_output").read_text().strip()


def test_rc_never_changes_the_pages_of_latest(site: Path) -> None:
    result = _deploy(site, "v0.9.0")
    assert result.returncode == 0, result.stderr
    assert _shows(_published(site), "latest", "v0.9.0")

    # rc of a minor with no final tag yet: publishes under the minor.
    result = _deploy(site, "v1.0.0-rc.1")
    assert result.returncode == 0, result.stderr
    assert _output_version(site) == "version=1.0"
    pages = _published(site)
    assert _shows(pages, "1.0", "v1.0.0-rc.1")
    assert _shows(pages, "latest", "v0.9.0")

    # The final tag moves latest to the minor.
    result = _deploy(site, "v1.0.0")
    assert result.returncode == 0, result.stderr
    assert _output_version(site) == "version=1.0"
    pages = _published(site)
    assert _shows(pages, "latest", "v1.0.0")
    assert json.loads((pages / "versions.json").read_text())[0]["aliases"] == ["latest"]

    # rc of a minor that already has a final tag: its own folder, and the
    # pages behind latest stay the final ones.
    result = _deploy(site, "v1.0.1-rc.1")
    assert result.returncode == 0, result.stderr
    pages = _published(site)
    assert _shows(pages, "latest", "v1.0.0")
    assert _shows(pages, "1.0", "v1.0.0")
    assert _shows(pages, "1.0.1-rc.1", "v1.0.1-rc.1")
    assert _output_version(site) == "version=1.0.1-rc.1"

    # The next final tag of the minor moves latest on.
    result = _deploy(site, "v1.0.1")
    assert result.returncode == 0, result.stderr
    assert _shows(_published(site), "latest", "v1.0.1")


def test_malformed_tag_publishes_nothing(site: Path) -> None:
    result = _deploy(site, "v1-hotfix")
    assert result.returncode == 1
    assert "not a vMAJOR.MINOR" in result.stderr
    assert _output_version(site) == ""
    assert _git(site, "ls-remote", "--heads", "origin", "gh-pages") == ""
