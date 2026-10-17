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
   (from a branch in this repo or from a fork, it makes no difference),
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
  warning; the merge-queue run still catches a clash there)
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

#: The check name the ``check`` job of the workflow reports under. The
#: re-check posts under the same name so the newest result replaces the
#: stale one on the pull request.
CHECK_NAME = "Migration chain (single head)"
#: Migrations directory, relative to the repo root.
VERSIONS_DIR = "backend/alembic/versions"
CHECK_SCRIPT: pathlib.Path = pathlib.Path(__file__).with_name("check_migration_heads.py")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_command(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one command and capture its output. Never raises on exit code."""
    return subprocess.run(
        args, input=stdin, capture_output=True, text=True, check=False, timeout=300
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


def list_open_migration_prs(run: Runner) -> list[PullRequest]:
    """Every open pull request that changes a file under the migrations directory."""
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,headRefOid,isCrossRepository,files",
        ]
    )
    if result.returncode != 0:
        raise RecheckError(f"gh pr list failed: {result.stderr.strip()}")
    prs = [
        PullRequest(
            number=int(item["number"]),
            head_sha=str(item["headRefOid"]),
            is_fork=bool(item["isCrossRepository"]),
        )
        for item in json.loads(result.stdout)
        if any(str(f["path"]).startswith(f"{VERSIONS_DIR}/") for f in item["files"])
    ]
    return sorted(prs, key=lambda pr: pr.number)


def export_versions(run: Runner, pr: PullRequest, dest: pathlib.Path) -> pathlib.Path:
    """Copy the pull request's migration directory into *dest*.

    Fetches ``refs/pull/N/head``, which exists in this repo for fork pull
    requests too. When the head moved since the listing, the newer head
    has its own ``pull_request`` run, so this one is skipped.
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
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")
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
    prs = list_open_migration_prs(run)
    if not prs:
        print("No open pull request adds a migration. Nothing to re-check.")
        return 0
    failed = 0
    for pr in prs:
        origin = "fork" if pr.is_fork else "same repo"
        try:
            verdict = check_pull_request(run, pr, target, work_dir)
            create_check_run(run, pr, target, verdict)
        except RecheckError as exc:
            failed += 1
            print(f"::warning::Could not re-check #{pr.number} ({origin}): {exc}")
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
        except RecheckError as exc:
            print(f"recheck_migration_prs: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
