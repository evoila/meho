# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/recheck_migration_prs.py`` (#4204).

After a migration lands on ``main``, every open pull request that adds a
migration must get a fresh result under the check name the workflow
uses, no matter where its branch lives (this repo or a fork) and no
matter whether the branch carries the workflow file. A fake stands in
for ``gh`` and ``git``; the real check script runs on the files.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT_PATH: pathlib.Path = _REPO_ROOT / "scripts" / "ci" / "recheck_migration_prs.py"
_WORKFLOW_PATH: pathlib.Path = _REPO_ROOT / ".github" / "workflows" / "migration-heads.yml"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("_recheck_migration_prs_under_test", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_module = _load_script_module()
VERSIONS_DIR: str = _module.VERSIONS_DIR


def _write(directory: pathlib.Path, name: str, revision: str, down_revision: str | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    parent = "None" if down_revision is None else repr(down_revision)
    (directory / name).write_text(
        f'"""Synthetic migration."""\n\n'
        f"revision: str = {revision!r}\n"
        f"down_revision: str | None = {parent}\n\n\n"
        f"def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n"
    )


@dataclass
class FakePullRequest:
    number: int
    head_sha: str
    is_fork: bool
    root: pathlib.Path
    #: Paths the listing reports as changed; migrations are added by ``add_migration``.
    files: list[str] = field(default_factory=list)
    #: When set, ``git rev-parse FETCH_HEAD`` answers this instead of ``head_sha``.
    moved_to: str | None = None
    fetch_fails: bool = False
    #: ``git fetch`` raises ``subprocess.TimeoutExpired`` instead of answering.
    fetch_times_out: bool = False

    def add_migration(self, name: str, revision: str, down_revision: str | None) -> None:
        _write(self.root / VERSIONS_DIR, name, revision, down_revision)
        self.files.append(f"{VERSIONS_DIR}/{name}")

    def add_link(self, name: str, target: pathlib.Path) -> None:
        """A symbolic link in the migration directory, kept as a link in the archive."""
        directory = self.root / VERSIONS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).symlink_to(target)
        self.files.append(f"{VERSIONS_DIR}/{name}")


#: Pull requests (and changed files of one pull request) per page of the
#: fake listing, the same as GitHub's maximum.
PAGE_SIZE = 100


def _files_page(files: list[str], start: int) -> dict[str, Any]:
    return {
        "pageInfo": {
            "hasNextPage": start + PAGE_SIZE < len(files),
            "endCursor": str(start + PAGE_SIZE),
        },
        "nodes": [{"path": path} for path in files[start : start + PAGE_SIZE]],
    }


@dataclass
class FakeGitHub:
    """Answers the ``gh`` and ``git`` calls the script makes.

    The listing is served in pages of :data:`PAGE_SIZE`, with the index
    of the next pull request as the cursor, so a test can check that the
    script reads every page. Anything else (the check script) runs for
    real.
    """

    prs: list[FakePullRequest]
    calls: list[list[str]] = field(default_factory=list)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    _fetched: FakePullRequest | None = None

    def _graphql(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        flags = args[3::2]
        variables = dict(arg.split("=", 1) for arg in args[4::2])
        assert variables["owner"] == "evoila" and variables["name"] == "meho"
        if "pullRequests(states: OPEN, first: 100, after: $after)" in variables["query"]:
            assert flags == ["-f"] * len(variables)
            start = int(variables.get("after", "0"))
            repository: dict[str, Any] = {
                "pullRequests": {
                    "pageInfo": {
                        "hasNextPage": start + PAGE_SIZE < len(self.prs),
                        "endCursor": str(start + PAGE_SIZE),
                    },
                    "nodes": [
                        {
                            "number": pr.number,
                            "headRefOid": pr.head_sha,
                            "isCrossRepository": pr.is_fork,
                            "files": _files_page(pr.files, 0),
                        }
                        for pr in self.prs[start : start + PAGE_SIZE]
                    ],
                }
            }
        else:
            assert "pullRequest(number: $number)" in variables["query"]
            assert "files(first: 100, after: $after)" in variables["query"]
            assert flags == ["-f", "-f", "-f", "-F", "-f"]
            assert list(variables) == ["query", "owner", "name", "number", "after"]
            pr = next(pr for pr in self.prs if pr.number == int(variables["number"]))
            repository = {"pullRequest": {"files": _files_page(pr.files, int(variables["after"]))}}
        body = {"data": {"repository": repository}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def __call__(
        self, args: list[str], *, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:3] == ["gh", "api", "graphql"]:
            return self._graphql(args)
        if args[:2] == ["git", "fetch"]:
            number = int(args[-1].removeprefix("pull/").removesuffix("/head"))
            self._fetched = next(pr for pr in self.prs if pr.number == number)
            if self._fetched.fetch_times_out:
                raise subprocess.TimeoutExpired(args, 300)
            if self._fetched.fetch_fails:
                return subprocess.CompletedProcess(args, 128, "", "fatal: couldn't find remote ref")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "rev-parse"]:
            assert self._fetched is not None
            return subprocess.CompletedProcess(
                args, 0, (self._fetched.moved_to or self._fetched.head_sha) + "\n", ""
            )
        if args[:2] == ["git", "archive"]:
            assert self._fetched is not None
            assert args[4] == self._fetched.head_sha and args[5] == VERSIONS_DIR
            with tarfile.open(args[3], "w") as tar:
                tar.add(self._fetched.root / VERSIONS_DIR, arcname=VERSIONS_DIR)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["gh", "api"]:
            assert args[2:] == ["repos/evoila/meho/check-runs", "-X", "POST", "--input", "-"]
            assert stdin is not None
            self.check_runs.append(json.loads(stdin))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)


def _main_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """``main`` after a migration landed: 0105 -> 0106 -> 0107."""
    main = tmp_path / "main" / VERSIONS_DIR
    _write(main, "0105_a.py", "0105", None)
    _write(main, "0106_b.py", "0106", "0105")
    _write(main, "0107_c.py", "0107", "0106")
    return main


def _pr(tmp_path: pathlib.Path, number: int, *, is_fork: bool) -> FakePullRequest:
    pr = FakePullRequest(number, f"{number:040x}", is_fork, tmp_path / f"pr-{number}")
    pr.add_migration("0105_a.py", "0105", None)
    pr.add_migration("0106_b.py", "0106", "0105")
    return pr


def _run(tmp_path: pathlib.Path, github: FakeGitHub) -> int:
    return cast(
        "int",
        _module.recheck(
            github,
            _module.Target(
                "evoila/meho", _main_dir(tmp_path), "deadbeef", "https://example.invalid/run/1"
            ),
            tmp_path / "work",
        ),
    )


def test_fork_pr_behind_main_gets_a_failing_check_run_on_its_head(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    fork = _pr(tmp_path, 41, is_fork=True)
    fork.add_migration("0107_mine.py", "0107", "0106")
    github = FakeGitHub([fork])
    assert _run(tmp_path, github) == 0
    assert len(github.check_runs) == 1
    check_run = github.check_runs[0]
    assert check_run["name"] == "Migration chain (single head)"
    assert check_run["head_sha"] == fork.head_sha
    assert check_run["status"] == "completed"
    assert check_run["conclusion"] == "failure"
    assert check_run["details_url"] == "https://example.invalid/run/1"
    summary = check_run["output"]["summary"]
    assert "revision 0107 is used by 2 files: 0107_c.py, 0107_mine.py" in summary
    assert "Rename your migration to 0108 and set down_revision to '0107'." in summary
    assert "#41 (fork, head" in capfd.readouterr().out
    assert ["git", "fetch", "--no-tags", "--depth=1", "origin", "pull/41/head"] in github.calls


def test_pr_without_the_workflow_file_still_gets_a_result(tmp_path: pathlib.Path) -> None:
    """The branch carries only migrations, no workflow file. Nothing in
    the re-check runs on the branch, so it gets a result anyway."""
    pr = _pr(tmp_path, 42, is_fork=False)
    pr.add_migration("0108_mine.py", "0108", "0107")
    github = FakeGitHub([pr])
    assert _run(tmp_path, github) == 0
    assert [run["conclusion"] for run in github.check_runs] == ["success"]
    assert github.check_runs[0]["head_sha"] == pr.head_sha
    assert github.check_runs[0]["output"]["title"] == "One chain, one head"
    assert not any(call[:3] == ["gh", "workflow", "run"] for call in github.calls)
    assert not any(call[:2] == ["git", "checkout"] for call in github.calls)


def test_fork_and_same_repo_prs_are_both_rechecked(tmp_path: pathlib.Path) -> None:
    fork = _pr(tmp_path, 41, is_fork=True)
    fork.add_migration("0107_mine.py", "0107", "0106")
    same_repo = _pr(tmp_path, 42, is_fork=False)
    same_repo.add_migration("0108_mine.py", "0108", "0107")
    github = FakeGitHub([same_repo, fork])
    assert _run(tmp_path, github) == 0
    assert [(run["head_sha"], run["conclusion"]) for run in github.check_runs] == [
        (fork.head_sha, "failure"),
        (same_repo.head_sha, "success"),
    ]


def test_pr_that_does_not_touch_migrations_is_skipped(tmp_path: pathlib.Path) -> None:
    pr = FakePullRequest(43, "c" * 40, False, tmp_path / "pr-43", files=["backend/src/app.py"])
    github = FakeGitHub([pr])
    assert _run(tmp_path, github) == 0
    assert github.check_runs == []
    assert not any(call[:2] == ["git", "fetch"] for call in github.calls)


def test_pr_whose_head_moved_is_left_to_its_own_pull_request_run(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    pr = _pr(tmp_path, 44, is_fork=False)
    pr.add_migration("0107_mine.py", "0107", "0106")
    pr.moved_to = "e" * 40
    github = FakeGitHub([pr])
    assert _run(tmp_path, github) == 1
    assert github.check_runs == []
    assert "::warning::Could not re-check #44 (same repo): head moved" in capfd.readouterr().out


def test_fetch_failure_is_a_warning_and_the_other_prs_still_run(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    broken = _pr(tmp_path, 45, is_fork=True)
    broken.add_migration("0107_mine.py", "0107", "0106")
    broken.fetch_fails = True
    fine = _pr(tmp_path, 46, is_fork=False)
    fine.add_migration("0108_mine.py", "0108", "0107")
    github = FakeGitHub([broken, fine])
    assert _run(tmp_path, github) == 1
    assert [run["head_sha"] for run in github.check_runs] == [fine.head_sha]
    out = capfd.readouterr().out
    assert "::warning::Could not re-check #45 (fork): git fetch failed" in out
    assert "Re-checked 2 pull request(s); 1 could not be checked." in out


def test_fetch_timeout_is_a_warning_and_the_next_pr_still_gets_a_result(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """One slow fetch must not stop the re-check of the pull requests
    after it; they would keep a stale green result."""
    slow = _pr(tmp_path, 47, is_fork=True)
    slow.add_migration("0107_mine.py", "0107", "0106")
    slow.fetch_times_out = True
    fine = _pr(tmp_path, 48, is_fork=False)
    fine.add_migration("0107_mine.py", "0107", "0106")
    github = FakeGitHub([slow, fine])
    assert _run(tmp_path, github) == 1
    assert [(run["head_sha"], run["conclusion"]) for run in github.check_runs] == [
        (fine.head_sha, "failure")
    ]
    out = capfd.readouterr().out
    assert "::warning::Could not re-check #47 (fork): git fetch timed out after 300 s" in out
    assert "#48 (same repo, head" in out
    assert "Re-checked 2 pull request(s); 1 could not be checked." in out


def test_a_migration_pr_beyond_the_first_200_listed_is_still_rechecked(
    tmp_path: pathlib.Path,
) -> None:
    """The listing is read page by page until the last one. A repository
    with 200 other open pull requests must not hide the one that adds a
    migration."""
    others = [
        FakePullRequest(n, f"{n:040x}", False, tmp_path / f"pr-{n}", files=["backend/src/app.py"])
        for n in range(1, 201)
    ]
    late = _pr(tmp_path, 201, is_fork=False)
    late.add_migration("0107_mine.py", "0107", "0106")
    github = FakeGitHub([*others, late])
    assert _run(tmp_path, github) == 0
    assert [(run["head_sha"], run["conclusion"]) for run in github.check_runs] == [
        (late.head_sha, "failure")
    ]
    listings = [call for call in github.calls if call[:3] == ["gh", "api", "graphql"]]
    cursors = [[arg for arg in call if arg.startswith("after=")] for call in listings]
    assert cursors == [[], ["after=100"], ["after=200"]]
    assert not any(call[:3] == ["gh", "pr", "list"] for call in github.calls)


def test_a_migration_after_the_first_100_changed_files_is_still_found(
    tmp_path: pathlib.Path,
) -> None:
    """The listing carries only the first 100 changed paths of a pull
    request. A big pull request whose migration comes later must not
    keep its old green check."""
    big = _pr(tmp_path, 49, is_fork=False)
    big.files = [f"backend/src/module_{n:03d}.py" for n in range(150)]
    big.add_migration("0107_mine.py", "0107", "0106")
    small = _pr(tmp_path, 50, is_fork=False)
    small.add_migration("0108_mine.py", "0108", "0107")
    github = FakeGitHub([big, small])
    assert _run(tmp_path, github) == 0
    assert [(run["head_sha"], run["conclusion"]) for run in github.check_runs] == [
        (big.head_sha, "failure"),
        (small.head_sha, "success"),
    ]
    file_pages = [
        [arg for arg in call if arg.startswith(("number=", "after="))]
        for call in github.calls
        if call[:3] == ["gh", "api", "graphql"] and any("number=" in arg for arg in call)
    ]
    assert file_pages == [["number=49", "after=100"]]


def test_an_archive_that_cannot_be_unpacked_skips_only_that_pr(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A link that points outside the work directory is rejected by the
    extraction filter. That must stop only this pull request; the next
    one still gets its result."""
    outside = tmp_path / "outside.py"
    outside.write_text("")
    bad = _pr(tmp_path, 51, is_fork=True)
    bad.add_link("0107_link.py", outside)
    fine = _pr(tmp_path, 52, is_fork=False)
    fine.add_migration("0107_mine.py", "0107", "0106")
    github = FakeGitHub([bad, fine])
    assert _run(tmp_path, github) == 1
    assert [(run["head_sha"], run["conclusion"]) for run in github.check_runs] == [
        (fine.head_sha, "failure")
    ]
    assert not (tmp_path / "work" / "pr-51" / VERSIONS_DIR / "0107_link.py").exists()
    out = capfd.readouterr().out
    assert "::warning::Could not re-check #51 (fork): could not unpack the migration files" in out
    assert "#52 (same repo, head" in out
    assert "Re-checked 2 pull request(s); 1 could not be checked." in out


def test_workflow_recheck_job_needs_nothing_from_the_pr_branch() -> None:
    """The re-check runs from main with `checks: write`, on a disposable
    runner, and never dispatches a workflow on the PR branch."""
    with _WORKFLOW_PATH.open() as fh:
        workflow = cast("dict[Any, Any]", yaml.safe_load(fh))
    triggers = cast("dict[str, Any]", workflow.get("on", workflow.get(True)))
    assert "workflow_dispatch" not in triggers
    assert workflow["jobs"]["check"]["name"] == _module.CHECK_NAME
    recheck = workflow["jobs"]["recheck-open-prs"]
    assert recheck["if"] == "github.event_name == 'push'"
    assert recheck["runs-on"] == "ubuntu-latest"
    assert recheck["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "checks": "write",
    }
    commands = [str(step.get("run", "")) for step in recheck["steps"]]
    assert any("scripts/ci/recheck_migration_prs.py" in command for command in commands)
    assert not any("gh workflow run" in command for command in commands)
