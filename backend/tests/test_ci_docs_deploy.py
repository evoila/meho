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
_REPUBLISH = _REPO_ROOT / "scripts" / "ci" / "docs_republish.sh"
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


def _deploy(site: Path, tag: str, mike: Path = _MIKE) -> subprocess.CompletedProcess[str]:
    """Commit a page that names ``tag``, push the tag, run the script."""
    (site / "docs" / "index.md").write_text(f"Docs of {tag} end.\n")
    _git(site, "add", "-A")
    _git(site, "commit", "-q", "-m", tag)
    _git(site, "tag", tag)
    _git(site, "push", "-q", "origin", "main", tag)
    return _run(site, mike, str(_SCRIPT), tag)


def _run(site: Path, mike: Path, *command: str) -> subprocess.CompletedProcess[str]:
    output = site / "github_output"
    output.write_text("")
    env = {**_GIT_ENV, "MIKE": str(mike), "GITHUB_OUTPUT": str(output)}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", *command],
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


def _republish(
    site: Path, page: str | None, mike: Path = _MIKE
) -> subprocess.CompletedProcess[str]:
    """Commit a page that names ``page`` on main (when given), run the republish."""
    if page is not None:
        (site / "docs" / "index.md").write_text(f"Docs of {page} end.\n")
        _git(site, "add", "-A")
        _git(site, "commit", "-q", "-m", page)
        _git(site, "push", "-q", "origin", "main")
    return _run(site, mike, str(_REPUBLISH))


def _racing_mike(site: Path, before_first_deploy: str) -> Path:
    """A mike that runs ``before_first_deploy`` (bash) once, before its first deploy.

    The bash lines can push gh-pages from another clone, so the script's own
    push is refused, like when a tag deploy and a main republish overlap.
    """
    wrapper = site.parent / "mike-race"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"flag={site.parent}/raced\n"
        'if [ "$1" = deploy ] && [ ! -e "$flag" ]; then\n'
        '  touch "$flag"\n'
        f"{before_first_deploy}\n"
        "fi\n"
        f'exec {_MIKE} "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


def _other_run(site: Path) -> str:
    """Bash lines that push a commit onto gh-pages from another clone."""
    other = site.parent / "other"
    _git(site.parent, "clone", "-q", "-b", "gh-pages", str(site.parent / "origin.git"), "other")
    _git(other, "config", "user.name", "test")
    _git(other, "config", "user.email", "test@example.com")
    (other / "extra.txt").write_text("another run\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "another run")
    return f"git -C {other} push -q origin gh-pages"


def _pages_head(site: Path) -> str:
    return _git(site, "ls-remote", "origin", "refs/heads/gh-pages").split()[0]


def test_republish_publishes_main_under_the_newest_final_release(site: Path) -> None:
    assert _deploy(site, "v0.9.0").returncode == 0
    assert _deploy(site, "v1.0.0").returncode == 0
    # A release candidate is not the newest final release.
    assert _deploy(site, "v1.1.0-rc.1").returncode == 0

    result = _republish(site, "main-fix")
    assert result.returncode == 0, result.stderr
    assert _output_version(site) == "version=1.0"
    pages = _published(site)
    assert _shows(pages, "latest", "main-fix")
    assert _shows(pages, "1.0", "main-fix")
    # The other versions are untouched.
    assert _shows(pages, "0.9", "v0.9.0")
    assert _shows(pages, "1.1", "v1.1.0-rc.1")
    versions = {
        v["version"]: v["aliases"] for v in json.loads((pages / "versions.json").read_text())
    }
    assert versions["1.0"] == ["latest"]


def test_republish_pushes_nothing_when_the_site_is_the_same(site: Path) -> None:
    assert _deploy(site, "v1.0.0").returncode == 0
    assert _republish(site, "main-fix").returncode == 0
    head = _pages_head(site)

    result = _republish(site, None)
    assert result.returncode == 0, result.stderr
    assert "nothing to push" in result.stdout
    assert _pages_head(site) == head
    assert _output_version(site) == ""


def test_republish_without_a_final_release_publishes_nothing(site: Path) -> None:
    assert _deploy(site, "v1.0.0-rc.1").returncode == 0
    head = _pages_head(site)

    result = _republish(site, "main-fix")
    assert result.returncode == 1
    assert "No final release tag" in result.stderr
    assert _pages_head(site) == head


def test_republish_tries_again_when_the_push_is_refused(site: Path) -> None:
    assert _deploy(site, "v1.0.0").returncode == 0

    result = _republish(site, "main-fix", _racing_mike(site, _other_run(site)))
    assert result.returncode == 0, result.stderr
    assert "refused (attempt 1 of 3)" in result.stderr
    pages = _published(site)
    assert (pages / "extra.txt").exists()
    assert _shows(pages, "latest", "main-fix")


def test_tag_deploy_tries_again_when_a_main_republish_pushed_first(site: Path) -> None:
    assert _deploy(site, "v1.0.0").returncode == 0

    result = _deploy(site, "v1.1.0", _racing_mike(site, _other_run(site)))
    assert result.returncode == 0, result.stderr
    assert "refused (attempt 1 of 3)" in result.stderr
    assert _output_version(site) == "version=1.1"
    pages = _published(site)
    assert (pages / "extra.txt").exists()
    assert _shows(pages, "latest", "v1.1.0")
    assert _shows(pages, "1.0", "v1.0.0")


def test_rc_deploy_checks_the_final_tag_again_after_a_refused_push(site: Path) -> None:
    assert _deploy(site, "v0.9.0").returncode == 0
    # While the rc deploy runs, the final tag of its minor is pushed.
    race = f"{_other_run(site)}\ngit tag v1.0.0\ngit push -q origin v1.0.0"

    result = _deploy(site, "v1.0.0-rc.1", _racing_mike(site, race))
    assert result.returncode == 0, result.stderr
    assert "refused (attempt 1 of 3)" in result.stderr
    assert _output_version(site) == "version=1.0.0-rc.1"
    pages = _published(site)
    assert not (pages / "1.0").exists()
    assert _shows(pages, "1.0.0-rc.1", "v1.0.0-rc.1")
    assert _shows(pages, "latest", "v0.9.0")


def test_republish_follows_a_newer_release_that_arrives_during_the_run(site: Path) -> None:
    assert _deploy(site, "v1.0.0").returncode == 0
    # While the republish runs, a newer final release is tagged and deployed.
    race = f"{_other_run(site)}\ngit tag v1.1.0\ngit push -q origin v1.1.0"

    result = _republish(site, "main-fix", _racing_mike(site, race))
    assert result.returncode == 0, result.stderr
    assert "refused (attempt 1 of 3)" in result.stderr
    assert _output_version(site) == "version=1.1"
    pages = _published(site)
    assert _shows(pages, "latest", "main-fix")
    assert _shows(pages, "1.1", "main-fix")
    assert _shows(pages, "1.0", "v1.0.0")
    versions = {
        v["version"]: v["aliases"] for v in json.loads((pages / "versions.json").read_text())
    }
    assert versions == {"1.1": ["latest"], "1.0": []}


def test_republish_refuses_to_delete_a_file_of_another_version(site: Path) -> None:
    assert _deploy(site, "v0.9.0").returncode == 0
    assert _deploy(site, "v1.0.0").returncode == 0
    head = _pages_head(site)
    # A mike whose deploy also swaps a page of 0.9 for a new one, so the
    # file count stays the same.
    index = site.parent / "index"
    wrapper = site.parent / "mike-swap"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'{_MIKE} "$@"\n'
        'if [ "$1" = deploy ]; then\n'
        f"  export GIT_INDEX_FILE={index}\n"
        "  git read-tree gh-pages\n"
        "  git rm -q --cached 0.9/index.html\n"
        "  blob=$(echo new | git hash-object -w --stdin)\n"
        '  git update-index --add --cacheinfo "100644,${blob},0.9/new.html"\n'
        "  tree=$(git write-tree)\n"
        '  git update-ref refs/heads/gh-pages "$(git commit-tree "$tree" -p gh-pages -m swap)"\n'
        "fi\n"
    )
    wrapper.chmod(0o755)

    result = _republish(site, "main-fix", wrapper)
    assert result.returncode == 1
    assert "would lose files outside 1.0/" in result.stderr
    assert "0.9/index.html" in result.stderr
    assert _pages_head(site) == head
    assert _output_version(site) == ""


def test_republish_removes_a_page_that_main_removed(site: Path) -> None:
    (site / "docs" / "old.md").write_text("Old page.\n")
    assert _deploy(site, "v1.0.0").returncode == 0
    _git(site, "rm", "-q", "docs/old.md")
    (site / "docs" / "new.md").write_text("New page.\n")

    result = _republish(site, "main-fix")
    assert result.returncode == 0, result.stderr
    assert "1.0/old/index.html" in result.stdout
    pages = _published(site)
    assert not (pages / "1.0" / "old").exists()
    assert (pages / "1.0" / "new" / "index.html").exists()
    assert _shows(pages, "latest", "main-fix")
