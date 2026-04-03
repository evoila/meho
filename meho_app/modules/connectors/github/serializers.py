# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Response Serializers.

Pure functions for converting GitHub API responses to MEHO format.
No side effects, no I/O -- just dict -> dict transformations.

GitHub's REST API uses snake_case natively (unlike ArgoCD's camelCase),
so most fields can be extracted directly.
"""


def serialize_repository(repo: dict) -> dict:
    """
    Serialize a GitHub repository to MEHO format.

    Extracts identity, metadata, and status fields from the GitHub API
    repository response.
    """
    return {
        "name": repo.get("name"),
        "full_name": repo.get("full_name"),
        "description": repo.get("description"),
        "default_branch": repo.get("default_branch"),
        "language": repo.get("language"),
        "visibility": repo.get("visibility"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "html_url": repo.get("html_url"),
        "archived": repo.get("archived"),
        "fork": repo.get("fork"),
        "open_issues_count": repo.get("open_issues_count"),
    }


def serialize_commit(commit: dict) -> dict:
    """
    Serialize a GitHub commit to MEHO format.

    Extracts SHA, first line of message, author info, and URL.
    The commit object has nested structure: commit.commit.author for
    git metadata vs commit.author for GitHub user.
    """
    c = commit.get("commit", {})
    author = c.get("author", {})
    committer = c.get("committer", {})
    return {
        "sha": commit.get("sha"),
        "message": c.get("message", "").split("\n")[0],  # First line only
        "author_name": author.get("name"),
        "author_email": author.get("email"),
        "date": author.get("date"),
        "html_url": commit.get("html_url"),
        "committer_name": committer.get("name"),
    }


def serialize_comparison(comparison: dict) -> dict:
    """
    Serialize a GitHub ref comparison to MEHO format.

    Includes status (ahead/behind/identical/diverged), commit counts,
    serialized commits, and changed files summary.
    """
    commits = comparison.get("commits") or []
    files = comparison.get("files") or []

    return {
        "status": comparison.get("status"),
        "ahead_by": comparison.get("ahead_by"),
        "behind_by": comparison.get("behind_by"),
        "total_commits": comparison.get("total_commits"),
        "commits": [serialize_commit(c) for c in commits],
        "files": [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "changes": f.get("changes"),
            }
            for f in files
        ],
    }


def serialize_pull_request(pr: dict) -> dict:
    """
    Serialize a GitHub pull request to MEHO format.

    Extracts identity, state, author, refs, and merge info.
    """
    user = pr.get("user") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}

    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "author": user.get("login"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "head_ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "html_url": pr.get("html_url"),
        "draft": pr.get("draft"),
        "mergeable": pr.get("mergeable"),
    }


def serialize_workflow_run(run: dict) -> dict:
    """
    Serialize a GitHub Actions workflow run to MEHO format.

    Extracts run identity, status/conclusion, triggering info, and timing.
    """
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "status": run.get("status"),  # queued, in_progress, completed
        "conclusion": run.get("conclusion"),  # success, failure, cancelled, etc.
        "workflow_id": run.get("workflow_id"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "event": run.get("event"),  # push, pull_request, schedule, etc.
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "html_url": run.get("html_url"),
        "run_attempt": run.get("run_attempt"),
    }


def serialize_workflow_job(job: dict) -> dict:
    """
    Serialize a GitHub Actions workflow job to MEHO format.

    Includes step-level status and timing for granular failure diagnosis.
    """
    steps = job.get("steps") or []
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "runner_name": job.get("runner_name"),
        "steps": [
            {
                "name": s.get("name"),
                "status": s.get("status"),
                "conclusion": s.get("conclusion"),
                "number": s.get("number"),
                "started_at": s.get("started_at"),
                "completed_at": s.get("completed_at"),
            }
            for s in steps
        ],
    }


def serialize_deployment(deployment: dict, statuses: list[dict] | None = None) -> dict:
    """
    Serialize a GitHub deployment to MEHO format.

    Includes deployment identity, ref/sha, and recent status history.
    Statuses are fetched separately and passed in.
    """
    creator = deployment.get("creator") or {}
    status_list = statuses or []

    return {
        "id": deployment.get("id"),
        "environment": deployment.get("environment"),
        "sha": deployment.get("sha"),
        "ref": deployment.get("ref"),
        "task": deployment.get("task"),
        "description": deployment.get("description"),
        "created_at": deployment.get("created_at"),
        "creator": creator.get("login"),
        "statuses": [
            {
                "state": s.get("state"),
                "description": s.get("description"),
                "created_at": s.get("created_at"),
                "environment": s.get("environment"),
            }
            for s in status_list
        ],
    }


def serialize_commit_status(
    combined_status: dict,
    check_runs: dict,
) -> dict:
    """
    Serialize commit status by merging legacy statuses and check runs.

    GitHub has two separate CI status systems:
    1. Legacy commit statuses (from external CI tools)
    2. Check runs (from GitHub Actions and GitHub Apps)

    Both must be queried and merged for a complete picture.
    """
    # Legacy statuses from /commits/{ref}/status
    statuses_list = combined_status.get("statuses") or []
    serialized_statuses = [
        {
            "state": s.get("state"),
            "context": s.get("context"),
            "description": s.get("description"),
            "target_url": s.get("target_url"),
        }
        for s in statuses_list
    ]

    # Check runs from /commits/{ref}/check-runs
    checks_list = check_runs.get("check_runs") or []
    serialized_checks = [
        {
            "name": cr.get("name"),
            "status": cr.get("status"),
            "conclusion": cr.get("conclusion"),
            "html_url": cr.get("html_url"),
            "annotations_count": (cr.get("output") or {}).get("annotations_count"),
        }
        for cr in checks_list
    ]

    return {
        "state": combined_status.get("state"),
        "total_count": combined_status.get("total_count"),
        "statuses": serialized_statuses,
        "checks": serialized_checks,
    }
