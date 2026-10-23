# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time preview for ``gh.composite.branch_commit_files``.

The write waits for approval. Without a preview the approver would see only
the operation id and the target. This builder stores what the commit would
do: the repository and branch, the commit the caller wants to build on
(``base_sha``), the commit the branch points to right now (one live read),
the message, and each file's path, size and SHA-256. When the branch is not
at ``base_sha`` any more, the preview says so under ``problems``, and the
handler will refuse the call after approval too.

The preview does not show the file content. The approval row keeps the
call's parameters, content included, only so the call can run after
approval; no read view shows them.

The checks come from :func:`._git.check_commit_request`, the same function
the handler runs, so the approver reads exactly what the commit would write
and exactly the problems that would stop it.
"""

from __future__ import annotations

from typing import Any

import httpx

from meho_backplane.connectors.github.composites._git import GitRepo, check_commit_request
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

__all__ = ["BRANCH_COMMIT_FILES_OP_ID", "branch_commit_files_preview"]

BRANCH_COMMIT_FILES_OP_ID = "gh.composite.branch_commit_files"


async def branch_commit_files_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the approval preview: branch, base commit, live head, and the file digests."""
    params = ctx.params
    owner = params.get("owner")
    repo = params.get("repo")
    branch = params.get("branch")
    if not all(isinstance(value, str) and value for value in (owner, repo, branch)):
        return None
    files, problems = check_commit_request(params)
    preview: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "base_sha": params.get("base_sha"),
        "branch_sha_now": None,
        "message": params.get("message"),
        "file_count": len(files),
        "total_bytes": sum(entry["size"] for entry in files),
        "files": files,
    }
    connector = ctx.connector_instance
    if problems or connector is None or ctx.target is None:
        if problems:
            preview["problems"] = problems
        return preview
    repo_session = GitRepo(connector, ctx.target, ctx.operator, str(owner), str(repo))  # type: ignore[arg-type]
    try:
        sha_now = await repo_session.ref_sha(f"heads/{branch}")
    except (httpx.HTTPError, OSError) as exc:
        preview["branch_sha_error"] = preview_failure_reason(exc)
        return preview
    preview["branch_sha_now"] = sha_now
    if sha_now is None:
        preview["problems"] = [f"branch {branch!r} does not exist"]
    elif sha_now != params.get("base_sha"):
        preview["problems"] = [
            f"branch {branch!r} is at {sha_now}, not at base_sha; the call will stop"
        ]
    return preview


register_preview_builder(BRANCH_COMMIT_FILES_OP_ID, branch_commit_files_preview)
