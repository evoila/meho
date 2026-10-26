#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Re-check every open pull request that adds a migration after main moved.

Runs in the push-to-main job of ``.github/workflows/migration-heads.yml``,
after the check on ``main`` itself passed. A pull request that adds a
migration was checked against the ``main`` of that moment. When another
migration lands on ``main``, that green check may be stale. This script
gives every such pull request a fresh result:

1. list every open pull request that touches ``backend/alembic/versions/``
   (from a branch in this repo or from a fork, it makes no difference;
   every page of the list and every page of a pull request's changed
   files is read, so no pull request is too old or too big),
2. fetch its head commit and copy its migration directory out of git
   with ``git archive``,
3. run ``check_migration_heads.py`` on that copy with ``main``'s
   directory as ``--main``,
4. create a check run named ``Migration chain (single head)`` on the
   pull request's head commit with the result.

The pull request branch does not need to carry the workflow, and no code
from the pull request runs: the checkout is ``main``, and the check
script only reads the migration files with ``ast``. That is why this job
can run on a disposable GitHub-hosted runner for every pull request.

Every ``gh`` and ``git`` call goes through one ``run`` function, so the
tests can stand in for GitHub without a network.

Exit codes
----------

* 0 — every pull request got a result (or had nothing to re-check)
* 1 — at least one pull request could not be checked (printed as a
  warning; a slow fetch, a failed command, a page of changed files that
  cannot be read or an archive that cannot be unpacked stops only that
  pull request, the merge-queue run still catches a clash there)
* 2 — bad environment or arguments
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The check name the ``check`` job of the workflow reports under. The
#: re-check posts under the same name so the newest result replaces the
#: stale one on the pull request.
CHECK_NAME = "Migration chain (single head)"
#: Migrations directory, relative to the repo root.
VERSIONS_DIR = "backend/alembic/versions"
CHECK_SCRIPT: pathlib.Path = pathlib.Path(__file__).with_name("check_migration_heads.py")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Seconds one ``gh`` or ``git`` call may take before it is given up on.
COMMAND_TIMEOUT = 300

#: One page of open pull requests, with the first page of paths each
#: one changes. ``gh pr list`` stops at its ``--limit``; this query is
#: paged by hand with ``after`` until ``hasNextPage`` is false. 100 is
#: GitHub's largest page, for pull requests and for the files of one pull
#: request alike. A pull request with more files than that is paged
#: further with :data:`FILES_QUERY` before it is skipped.
LIST_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        headRefOid
        isCrossRepository
        files(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { path }
        }
      }
    }
  }
}
"""

#: The next page of changed paths of one pull request.
FILES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
}
"""


def run_command(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one command and capture its output.

    Never raises on exit code. Raises :class:`subprocess.TimeoutExpired`
    when the command takes longer than :data:`COMMAND_TIMEOUT`.
    """
    return subprocess.run(
        args, input=stdin, capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT
    )


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    is_fork: bool


@dataclass(frozen=True)
class Verdict:
    conclusion: str
    text: str


@dataclass(frozen=True)
class Target:
    """Where the results go and what they are compared against."""

    repo: str
    main_dir: pathlib.Path
    main_sha: str
    details_url: str | None


class RecheckError(Exception):
    """The pull request could not be checked. The message says why."""


def _reason(exc: RecheckError | subprocess.TimeoutExpired) -> str:
    """One readable line for a failure of one ``gh`` or ``git`` call."""
    if isinstance(exc, subprocess.TimeoutExpired):
        cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(str(part) for part in exc.cmd[:2])
        return f"{cmd} timed out after {exc.timeout:.0f} s"
    return str(exc)


def _warn(number: int, is_fork: bool, exc: RecheckError | subprocess.TimeoutExpired) -> None:
    """Report one pull request that could not be checked, then let the caller go on."""
    origin = "fork" if is_fork else "same repo"
    print(f"::warning::Could not re-check #{number} ({origin}): {_reason(exc)}")


def _graphql(run: Runner, repo: str, query: str, **variables: str | int) -> dict[str, Any]:
    """Run one GraphQL query and return its ``data.repository`` object."""
    owner, _, name = repo.partition("/")
    args = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}"]
    args += ["-f", f"name={name}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    result = run(args)
    if result.returncode != 0:
        raise RecheckError(f"GitHub query failed: {result.stderr.strip()}")
    repository: dict[str, Any] = json.loads(result.stdout)["data"]["repository"]
    return repository


def _list_page(run: Runner, repo: str, after: str | None) -> dict[str, Any]:
    variables: dict[str, str | int] = {} if after is None else {"after": after}
    page: dict[str, Any] = _graphql(run, repo, LIST_QUERY, **variables)["pullRequests"]
    return page


def _has_migration(files: dict[str, Any]) -> bool:
    paths = [str(f["path"]) for f in files["nodes"]]
    return any(path.startswith(f"{VERSIONS_DIR}/") for path in paths)


def _touches_migrations(run: Runner, repo: str, item: dict[str, Any]) -> bool:
    """Whether the pull request changes a file under the migrations directory.

    The listing carries only the first 100 changed paths. A pull request
    with more files is paged further until a migration shows up or the
    paths run out, so a migration after the first page is not missed.
    """
    files: dict[str, Any] = item["files"]
    while True:
        if _has_migration(files):
            return True
        if not files["pageInfo"]["hasNextPage"]:
            return False
        after = str(files["pageInfo"]["endCursor"])
        number = int(item["number"])
        files = _graphql(run, repo, FILES_QUERY, number=number, after=after)["pullRequest"]["files"]


def list_open_migration_prs(run: Runner, repo: str) -> tuple[list[PullRequest], int]:
    """Every open pull request that changes a file under the migrations directory.

    Reads the whole list, page by page. A repository with many open pull
    requests must not hide its oldest migration pull requests. Returns
    the pull requests found and the number of pull requests whose
    changed files could not be read; each of those is reported and
    skipped, the others are still listed.
    """
    prs: list[PullRequest] = []
    skipped = 0
    after: str | None = None
    while True:
        page = _list_page(run, repo, after)
        for item in page["nodes"]:
            pr = PullRequest(
                number=int(item["number"]),
                head_sha=str(item["headRefOid"]),
                is_fork=bool(item["isCrossRepository"]),
            )
            try:
                touches = _touches_migrations(run, repo, item)
            except (RecheckError, subprocess.TimeoutExpired) as exc:
                skipped += 1
                _warn(pr.number, pr.is_fork, exc)
                continue
            if touches:
                prs.append(pr)
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = str(page["pageInfo"]["endCursor"])
    return sorted(prs, key=lambda pr: pr.number), skipped


def export_versions(run: Runner, pr: PullRequest, dest: pathlib.Path) -> pathlib.Path:
    """Copy the pull request's migration directory into *dest*.

    Fetches ``refs/pull/N/head``, which exists in this repo for fork pull
    requests too. When the head moved since the listing, the newer head
    has its own ``pull_request`` run, so this one is skipped. The archive
    is unpacked with the ``data`` filter, which rejects links and paths
    that leave *dest*; such a pull request is skipped with a reason
    instead of stopping the re-check.
    """
    fetched = run(["git", "fetch", "--no-tags", "--depth=1", "origin", f"pull/{pr.number}/head"])
    if fetched.returncode != 0:
        raise RecheckError(f"git fetch failed: {fetched.stderr.strip()}")
    head = run(["git", "rev-parse", "FETCH_HEAD"])
    if head.returncode != 0 or head.stdout.strip() != pr.head_sha:
        raise RecheckError(
            f"head moved from {pr.head_sha} to {head.stdout.strip() or '?'}; "
            "the pull_request run covers the new head"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tar_path = dest.with_name(f"{dest.name}.tar")
    archived = run(["git", "archive", "--output", str(tar_path), pr.head_sha, VERSIONS_DIR])
    if archived.returncode != 0:
        raise RecheckError(f"git archive failed: {archived.stderr.strip()}")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise RecheckError(f"could not unpack the migration files: {exc}") from exc
    return dest / VERSIONS_DIR


def check_pull_request(
    run: Runner, pr: PullRequest, target: Target, work_dir: pathlib.Path
) -> Verdict:
    versions_dir = export_versions(run, pr, work_dir / f"pr-{pr.number}")
    result = run(
        [sys.executable, str(CHECK_SCRIPT), str(versions_dir), "--main", str(target.main_dir)]
    )
    text = (result.stdout + result.stderr).strip()
    return Verdict(conclusion="success" if result.returncode == 0 else "failure", text=text)


def create_check_run(run: Runner, pr: PullRequest, target: Target, verdict: Verdict) -> None:
    """Post the result as a check run on the pull request's head commit."""
    title = (
        "One chain, one head"
        if verdict.conclusion == "success"
        else "Migration chain clashes with main"
    )
    payload: dict[str, object] = {
        "name": CHECK_NAME,
        "head_sha": pr.head_sha,
        "status": "completed",
        "conclusion": verdict.conclusion,
        "external_id": f"recheck-{target.main_sha}",
        "output": {
            "title": title,
            "summary": (
                f"Re-check against main `{target.main_sha}` after a migration landed there.\n\n"
                f"```\n{verdict.text}\n```"
            ),
        },
    }
    if target.details_url:
        payload["details_url"] = target.details_url
    result = run(
        ["gh", "api", f"repos/{target.repo}/check-runs", "-X", "POST", "--input", "-"],
        stdin=json.dumps(payload),
    )
    if result.returncode != 0:
        raise RecheckError(f"could not create the check run: {result.stderr.strip()}")


def recheck(run: Runner, target: Target, work_dir: pathlib.Path) -> int:
    """Give every open migration pull request a fresh result.

    A pull request that cannot be checked (a slow or failed ``gh`` or
    ``git`` call while reading its files, fetching or archiving it, or
    posting its result; a head that moved; an archive that cannot be
    unpacked) is reported and skipped. The others still get their result.
    """
    prs, failed = list_open_migration_prs(run, target.repo)
    if not prs and not failed:
        print("No open pull request adds a migration. Nothing to re-check.")
        return 0
    for pr in prs:
        origin = "fork" if pr.is_fork else "same repo"
        try:
            verdict = check_pull_request(run, pr, target, work_dir)
            create_check_run(run, pr, target, verdict)
        except (RecheckError, subprocess.TimeoutExpired) as exc:
            failed += 1
            _warn(pr.number, pr.is_fork, exc)
            continue
        print(f"#{pr.number} ({origin}, head {pr.head_sha}): {verdict.conclusion}")
    print(f"Re-checked {len(prs)} pull request(s); {failed} could not be checked.")
    return 1 if failed else 0


def _default_details_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-check every open pull request that adds a migration after main moved.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GH_REPO"),
        help="owner/name of the repository (default: $GH_REPO)",
    )
    parser.add_argument(
        "--main-sha",
        default=os.environ.get("GITHUB_SHA", "main"),
        help="the main commit this re-check is for, shown in the result (default: $GITHUB_SHA)",
    )
    parser.add_argument(
        "--main-dir",
        type=pathlib.Path,
        default=pathlib.Path(VERSIONS_DIR),
        help="main's migrations directory in the current checkout (default: %(default)s)",
    )
    parser.add_argument(
        "--details-url",
        default=_default_details_url(),
        help="link shown on the check run (default: this workflow run)",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        print("recheck_migration_prs: --repo or $GH_REPO is required", file=sys.stderr)
        return 2
    if not args.main_dir.is_dir():
        print(f"recheck_migration_prs: not a directory: {args.main_dir}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="recheck-migrations-") as tmp:
        try:
            target = Target(args.repo, args.main_dir.resolve(), args.main_sha, args.details_url)
            return recheck(run_command, target, pathlib.Path(tmp))
        except (RecheckError, subprocess.TimeoutExpired) as exc:
            print(f"recheck_migration_prs: {_reason(exc)}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
