# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sub-issue linkage ``gh.composite.*`` handler (1 composite, #2081).

Filing a task under a parent on a board-driven team needs a parent /
sub-issue link. GitHub exposes this over REST --
``POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues`` -- but
that op is not part of the gh-rest connector's live surface (the
connector's ``execute`` is a stub; there is no ingested catalog), so
before this Task a sub-issue link dropped back to the ``gh`` CLI. This
composite makes the link callable through the connector's own dispatch
path.

:func:`sub_issue_add_composite` (``gh.composite.sub_issue_add``) issues
the write directly on the resolved connector's session
(:meth:`~meho_backplane.connectors.adapters.http.HttpConnector._post_json`
mounted through ``mount_op_path``), the #2255 direct-session substrate,
so it works on a fresh deploy with zero gh catalog ingest.

The child is named by its number
--------------------------------

GitHub's endpoint takes the sub-issue's **database id** (the ``id`` field on
the issue payload), not its number. MEHO takes the child's **number**
instead and reads ``GET /repos/{owner}/{repo}/issues/{sub_issue_number}``
first. That read proves the child is an issue in the target's repository,
and it gives the database id for the link. A database id alone cannot be
checked: GitHub has no call that finds an issue by it.

Governance posture
------------------

A ticket write: ``safety_level="safe"`` / ``requires_approval=False`` (see
:mod:`._register` and
``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``). The parent
and the child must both be in the repository the target names
(:func:`._scope.require_target_repo`); anything else is refused before any
call to GitHub. The dispatcher gates the composite via ``policy_gate``
before it runs the handler, and the handler makes one write, so no
``enforce_subop_policy`` seam is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.github.composites._git import GitRepo
from meho_backplane.connectors.github.composites._scope import (
    TicketWriteScopeError,
    require_target_repo,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = ["sub_issue_add_composite"]

_ISSUE_PATH: Final = "/repos/{owner}/{repo}/issues/{number}"
_SUB_ISSUES_PATH: Final = "/repos/{owner}/{repo}/issues/{number}/sub_issues"


async def sub_issue_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Link a sub-issue to a parent issue via ``POST .../sub_issues``.

    Op-id: ``gh.composite.sub_issue_add``. The parent (``issue_number``) and
    the child (``sub_issue_number``) are both issues of ``owner/repo``, which
    must be the target's repository. ``replace_parent=True`` moves a child
    that already has another parent; omitted, GitHub uses its default
    (refuse when the child already has a parent).

    Returns
    -------
    dict[str, Any]
        ``{"parent": <parent issue payload>, "parent_number": <int>,
        "sub_issue_number": <int>, "sub_issue_id": <int>, "status": "linked"}``.

    Raises
    ------
    TicketWriteScopeError
        ``owner/repo`` is not the target's repository, the target names
        none, or the child is a pull request or has no database id.
    httpx.HTTPStatusError
        A non-2xx status from GitHub -- 404 (unknown parent or child), 422
        (the child already has a parent and ``replace_parent`` was not set).
        Propagates for the dispatcher to map.
    """
    require_target_repo(target, params.get("owner"), params.get("repo"))
    repo = GitRepo(connector, target, operator, params["owner"], params["repo"])
    issue_number = int(params["issue_number"])
    sub_issue_number = int(params["sub_issue_number"])

    child = await repo.get(_ISSUE_PATH, number=str(sub_issue_number))
    # A moved issue answers with a redirect to its new repository, so check
    # where the answer comes from, not only what was asked.
    child_repo = str(child.get("repository_url") or "").rsplit("/repos/", 1)[-1]
    if child_repo.lower() != f"{repo.owner}/{repo.repo}".lower():
        raise TicketWriteScopeError(
            f"#{sub_issue_number} is not an issue of {repo.owner}/{repo.repo} "
            f"(GitHub answered for {child_repo or 'an unknown repository'!r})"
        )
    if child.get("pull_request") is not None:
        raise TicketWriteScopeError(
            f"#{sub_issue_number} in {repo.owner}/{repo.repo} is a pull request, not an issue"
        )
    sub_issue_id = child.get("id")
    if not isinstance(sub_issue_id, int) or isinstance(sub_issue_id, bool):
        raise TicketWriteScopeError(
            f"GitHub returned no database id for #{sub_issue_number} in {repo.owner}/{repo.repo}"
        )

    body: dict[str, Any] = {"sub_issue_id": sub_issue_id}
    replace_parent = params.get("replace_parent")
    if replace_parent is not None:
        body["replace_parent"] = bool(replace_parent)
    parent = await repo.send("POST", _SUB_ISSUES_PATH, json=body, number=str(issue_number))
    return {
        "parent": parent,
        "parent_number": issue_number,
        "sub_issue_number": sub_issue_number,
        "sub_issue_id": sub_issue_id,
        "status": "linked",
    }
