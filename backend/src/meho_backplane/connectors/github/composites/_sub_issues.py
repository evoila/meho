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

The ``sub_issue_id`` gotcha
---------------------------

GitHub's endpoint takes the sub-issue's **database id** (the ``id``
field on the issue payload), **not** its issue number (the ``#N`` in the
URL). The REST create-issue response an agent already holds carries that
``id``, so the board-complete flow (create child issue -> link it) has
the value in hand; the parameter schema and this docstring both call the
distinction out because passing the number is the obvious mistake.

Governance posture
------------------

A single REST write, registered ``safety_level="caution"`` /
``requires_approval=False`` (see :mod:`._register`) -- linking a
sub-issue is a low-blast-radius, reversible board-hygiene mutation. As
with the board write composites, the composite is the reviewed unit: the
dispatcher gates it via ``policy_gate`` before invoking the handler, so
the one write it performs is fully governed and no ``enforce_subop_policy``
seam is needed (there is no additional internal write to re-gate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = ["sub_issue_add_composite"]

# REST wire path for the add-sub-issue endpoint, keyed with ``str.format``
# placeholders. Mounted under ``https://api.github.com`` by the
# connector's ``_base_url``; ``mount_op_path`` is identity for github.com.
_SUB_ISSUES_PATH = "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"


async def sub_issue_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Link a sub-issue to a parent issue via ``POST .../sub_issues``.

    Op-id: ``gh.composite.sub_issue_add``. ``sub_issue_id`` is the
    sub-issue's **database id** (not its issue number -- see the module
    docstring). ``replace_parent=True`` moves a sub-issue that already
    has a different parent under this one; omitted, GitHub uses its
    default (reject when the sub-issue already has a parent).

    Returns
    -------
    dict[str, Any]
        ``{"parent": <parent issue payload>, "parent_number": <int>,
        "sub_issue_id": <int>, "status": "linked"}``. GitHub returns the
        full parent issue object (``201 Created``) on success; it is
        surfaced verbatim under ``parent`` so the caller can read the
        updated ``sub_issues_summary`` count.

    Raises
    ------
    httpx.HTTPStatusError
        A non-2xx status -- 404 (unknown parent / sub-issue), 422 (the
        sub-issue is in a different repo owner, or already parented and
        ``replace_parent`` was not set). Propagates for the dispatcher's
        outer branch to map to the matching structured result.
    """
    owner = params["owner"]
    repo = params["repo"]
    issue_number = params["issue_number"]
    sub_issue_id = params["sub_issue_id"]
    replace_parent = params.get("replace_parent")

    path = await connector.mount_op_path(
        target,
        _SUB_ISSUES_PATH.format(owner=owner, repo=repo, issue_number=issue_number),
        operator,
    )
    body: dict[str, Any] = {"sub_issue_id": sub_issue_id}
    if replace_parent is not None:
        body["replace_parent"] = bool(replace_parent)

    parent = await connector._post_json(target, path, operator=operator, verb="POST", json=body)
    return {
        "parent": parent,
        "parent_number": issue_number,
        "sub_issue_id": sub_issue_id,
        "status": "linked",
    }
