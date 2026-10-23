# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time preview for ``gh.composite.branch_commit_files``.

The write waits for approval. Without a preview the approver would see only
the operation id and the target. This builder stores what the commit would
do: the repository and branch, the commit the branch points to right now
(one live read of the ref), the message, and each file's path, size and
SHA-256. The file content is never stored: it can hold anything, and the
digest is enough to check it later.

The file list comes from :func:`._git.describe_files`, the same function the
handler runs, so the approver reads exactly what the commit would write.
"""

from __future__ import annotations

from typing import Any

import httpx

from meho_backplane.connectors.github.composites._git import GitRepo, describe_files
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = ["BRANCH_COMMIT_FILES_OP_ID", "branch_commit_files_preview"]

BRANCH_COMMIT_FILES_OP_ID = "gh.composite.branch_commit_files"


async def branch_commit_files_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the approval preview: branch, its current commit, and the file digests."""
    params = ctx.params
    owner = params.get("owner")
    repo = params.get("repo")
    branch = params.get("branch")
    if not all(isinstance(value, str) and value for value in (owner, repo, branch)):
        return None
    files, problems = describe_files(params.get("files"))
    preview: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "message": params.get("message"),
        "base_sha": None,
        "file_count": len(files),
        "total_bytes": sum(entry["size"] for entry in files),
        "files": files,
    }
    if problems:
        preview["problems"] = problems
    connector = ctx.connector_instance
    if connector is None or ctx.target is None:
        return preview
    repo_session = GitRepo(connector, ctx.target, ctx.operator, str(owner), str(repo))  # type: ignore[arg-type]
    try:
        base_sha = await repo_session.ref_sha(f"heads/{branch}")
    except (httpx.HTTPError, OSError) as exc:
        preview["base_sha_error"] = f"{type(exc).__name__}: {exc}"
        return preview
    if base_sha is None:
        preview["base_sha_error"] = f"branch {branch!r} does not exist"
        return preview
    preview["base_sha"] = base_sha
    return preview


register_preview_builder(BRANCH_COMMIT_FILES_OP_ID, branch_commit_files_preview)
