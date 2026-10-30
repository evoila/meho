# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_github_composite_operations`` -- registrar for gh-rest composites.

Module-level async function called from the lifespan-driven
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
after the registrar list is populated by the
:mod:`meho_backplane.connectors.github.composites` package's
``__init__`` (which appends this function via
:func:`register_typed_op_registrar`).

Per-composite arguments (summary / description / group_key / tags /
``parameter_schema`` / ``safety_level`` / ``requires_approval``) live
in this module so a future shape change touches one file. The helper
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
handles upsert, body-hash dedupe, embedding pipeline, and
``source_kind="composite"`` persistence.

Scope
-----

Ten composites ship. ``gh.composite.pr_status_summary`` (T4 #1224) and
``gh.composite.repo_view`` (one repository, read) are read-only.
``gh.composite.repo_create`` (one repository in an organization) is a
``caution`` write that waits for approval (``requires_approval=True``). #2081
adds four more: ``gh.composite.project_view`` (read),
``gh.composite.project_item_add`` /
``gh.composite.project_item_set_field`` (Projects-v2 GraphQL writes),
and ``gh.composite.sub_issue_add`` (sub-issue linkage REST write) --
together the "file a board-complete ticket without a gh CLI fallback"
surface. The ``git`` group adds ``gh.composite.git_ref_view`` and
``gh.composite.git_tree_view`` (reads) and
``gh.composite.branch_commit_files`` (a ``dangerous`` write that always
waits for approval; see :mod:`._git`).

The composite-helper's defaults are ``safety_level="dangerous"`` +
``requires_approval=True`` (suited to high-blast-radius write
composites). The reads override to ``safe``; the board + sub-issue
writes override to ``caution`` -- low-blast-radius, reversible
board-hygiene mutations -- all with ``requires_approval=False`` so a
human / service operator can file a board-complete ticket without an
approval round-trip while an agent still routes through the ``caution``
safety default. See :mod:`._board` / :mod:`._sub_issues` for the
per-composite governance rationale.

``safety_level`` value note
---------------------------

The issue body specifies ``safety_level="read"``. The
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
helper validates against the enum ``{"safe", "caution", "dangerous"}``
(see the ``Literal`` annotation on its signature). ``"safe"`` is the
register-time equivalent of the operator-visible "read" label -- the
descriptor row stores ``safety_level="safe"`` and ``op_class="read"``
is computed elsewhere from method semantics. The registrar passes
``"safe"``; the issue body's "read" wording maps to this verbatim.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors.github.composites._board import (
    project_item_add_composite,
    project_item_set_field_composite,
    project_view_composite,
)
from meho_backplane.connectors.github.composites._git import (
    branch_commit_files_composite,
    git_ref_view_composite,
    git_tree_view_composite,
)
from meho_backplane.connectors.github.composites._read import (
    pr_status_summary_composite,
    repo_view_composite,
)
from meho_backplane.connectors.github.composites._repos import (
    repo_create_composite,
)
from meho_backplane.connectors.github.composites._sub_issues import (
    sub_issue_add_composite,
)
from meho_backplane.connectors.github.composites.schemas import (
    PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
    PR_STATUS_SUMMARY_RESPONSE_SCHEMA,
    PROJECT_ITEM_ADD_PARAMETER_SCHEMA,
    PROJECT_ITEM_ADD_RESPONSE_SCHEMA,
    PROJECT_ITEM_SET_FIELD_PARAMETER_SCHEMA,
    PROJECT_ITEM_SET_FIELD_RESPONSE_SCHEMA,
    PROJECT_VIEW_PARAMETER_SCHEMA,
    PROJECT_VIEW_RESPONSE_SCHEMA,
    REPO_CREATE_PARAMETER_SCHEMA,
    REPO_CREATE_RESPONSE_SCHEMA,
    REPO_VIEW_PARAMETER_SCHEMA,
    REPO_VIEW_RESPONSE_SCHEMA,
    SUB_ISSUE_ADD_PARAMETER_SCHEMA,
    SUB_ISSUE_ADD_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.github.composites.schemas_git import (
    BRANCH_COMMIT_FILES_LLM_INSTRUCTIONS,
    BRANCH_COMMIT_FILES_PARAMETER_SCHEMA,
    BRANCH_COMMIT_FILES_RESPONSE_SCHEMA,
    GIT_REF_VIEW_LLM_INSTRUCTIONS,
    GIT_REF_VIEW_PARAMETER_SCHEMA,
    GIT_REF_VIEW_RESPONSE_SCHEMA,
    GIT_TREE_VIEW_LLM_INSTRUCTIONS,
    GIT_TREE_VIEW_PARAMETER_SCHEMA,
    GIT_TREE_VIEW_RESPONSE_SCHEMA,
)
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "register_github_composite_operations",
]


# Natural-key shorthand. Every gh-rest composite registers against the
# same triple the connector advertises -- ``register_connector_v2(
# product="gh", version="3", impl_id="gh-rest", ...)`` in the package
# ``__init__`` -- so the dispatcher's ``connector_id="gh-rest-3"``
# lookup resolves the composite alongside the ingested L2 ops.
_PRODUCT = "gh"
_VERSION = "3"
_IMPL_ID = "gh-rest"


#: Curated agent-actionable group selectors for the gh-rest composite
#: surface, surfaced verbatim by ``list_operation_groups`` so the LLM
#: client picks the right composite group before drilling into
#: ``search_operations``. T4 shipped the ``pulls`` group; #2081 adds
#: the ``board`` (Projects-v2) and ``issues`` (sub-issue linkage) groups.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "pulls": (
        "Use for one-call PR-status questions: 'is PR #N ready to "
        "merge?' / 'what is the CI state on the head commit?' / 'who "
        "approved this PR?'. The composite gathers PR metadata, "
        "head-commit check runs, reviews, and the mergeable state in "
        "a single envelope so the LLM client does not have to "
        "orchestrate three separate L2 calls. Read-only. Pair with the "
        "ingested L2 ops (gh.pr.get_files, gh.pr.get_commits, etc.) "
        "when drill-in beyond the summary is needed."
    ),
    "board": (
        "Use to put a ticket on a GitHub Projects-v2 board and set its "
        "Status / Priority / Size fields -- the GraphQL-only surface a "
        "REST connector cannot reach. Start with project_view to read "
        "the board's node id, its single-select field ids, and each "
        "field's option ids; then project_item_add to place an issue / "
        "PR on the board (by content node id); then "
        "project_item_set_field to set a single-select field. This is "
        "the 'file a board-complete ticket' path -- no gh CLI fallback."
    ),
    "issues": (
        "Use to link a task to its parent as a GitHub sub-issue "
        "(sub_issue_add). Complements the board group: after creating a "
        "child issue you attach it under a parent by the child's "
        "database id. The sub-issue must share the parent's repository "
        "owner."
    ),
    "repos": (
        "Use for questions about one repository as a whole: does it "
        "exist, who owns it, is it public or private, what is its "
        "default branch, is it archived, how many issues are open, when "
        "was the last push, and what may the connector's identity do in "
        "it. Start here before you read branches, files, issues or pull "
        "requests of a repository you have not seen in this session. "
        "Use repo_create to make a new repository in an organisation; it "
        "waits for approval and changes nothing when the name exists."
    ),
    "git": (
        "Use to work with the git objects of a repository: which commit a "
        "branch or tag points to (git_ref_view), the files and folders of a "
        "branch, tag or commit (git_tree_view), and writing text files to a "
        "branch as one new commit (branch_commit_files, waits for approval, "
        "never forces). Start with git_ref_view, then git_tree_view."
    ),
}


class _CompositeSpec(NamedTuple):
    """Per-composite registration arguments.

    Field-table form rather than per-composite kwargs blocks: keeps the
    op_id / handler / schemas / group / tags / policy posture adjacent
    so a future maintainer reading the registrar sees the whole row at
    a glance. Common fields (``product`` / ``version`` / ``impl_id``)
    live on the call site below.
    """

    op_id: str
    handler: Callable[..., Awaitable[dict[str, Any]]]
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    group_key: str
    tags: list[str]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None = None


_COMPOSITES: tuple[_CompositeSpec, ...] = (
    _CompositeSpec(
        op_id="gh.composite.pr_status_summary",
        handler=pr_status_summary_composite,
        summary="Return PR metadata + checks + reviews + mergeable in one call.",
        description=(
            "Composes three L2 sub-ops -- GET:/repos/{owner}/{repo}/"
            "pulls/{pull_number}, GET:/repos/{owner}/{repo}/commits/"
            "{ref}/check-runs (against the PR's head SHA), and "
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews -- "
            "into a single envelope. Answers 'is this PR ready to "
            "merge?' without three separate L2 calls. Read-only; "
            "degrades gracefully when the checks or reviews sub-call "
            "fails (the failed field surfaces as null + a "
            "checks_status / review_status of 'unknown'). Equivalent "
            "of 'gh pr view <n> --json ...' for an LLM client that "
            "needs the answer in one round-trip through MEHO. The "
            "primary PR sub-call is non-optional -- a 404 / 401 on "
            "that call propagates to the operator as a connector "
            "error."
        ),
        parameter_schema=PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
        response_schema=PR_STATUS_SUMMARY_RESPONSE_SCHEMA,
        group_key="pulls",
        tags=["composite", "read-only", "pulls", "status"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="gh.composite.project_view",
        handler=project_view_composite,
        summary="Read a Projects-v2 board: node id + fields (with option ids) + items.",
        description=(
            "Reads a GitHub Projects-v2 board in one GraphQL call -- its node "
            "id, its fields (single-select fields carry their option ids), and "
            "its current items -- via the organization/user projectV2(number) "
            "query. The GraphQL-only Projects-v2 surface has no REST "
            "equivalent, so this is how an agent discovers the project_id / "
            "field_id / option_id node ids the two write composites need. "
            "Read-only. Pair with project_item_add + project_item_set_field to "
            "file a board-complete ticket without a gh CLI fallback."
        ),
        parameter_schema=PROJECT_VIEW_PARAMETER_SCHEMA,
        response_schema=PROJECT_VIEW_RESPONSE_SCHEMA,
        group_key="board",
        tags=["composite", "read-only", "board", "projectv2", "graphql"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="gh.composite.project_item_add",
        handler=project_item_add_composite,
        summary="Add an issue / PR to a Projects-v2 board (addProjectV2ItemById).",
        description=(
            "Adds an issue or pull request to a GitHub Projects-v2 board by "
            "content node id, wrapping the addProjectV2ItemById GraphQL "
            "mutation. project_id comes from gh.composite.project_view; "
            "content_id is the issue/PR node_id the REST payload carries. A "
            "low-blast-radius board-hygiene write -- adding a card to a board "
            "is reversible. Returns the new board item's node id, which "
            "project_item_set_field then targets."
        ),
        parameter_schema=PROJECT_ITEM_ADD_PARAMETER_SCHEMA,
        response_schema=PROJECT_ITEM_ADD_RESPONSE_SCHEMA,
        group_key="board",
        tags=["composite", "write", "board", "projectv2", "graphql"],
        safety_level="caution",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="gh.composite.project_item_set_field",
        handler=project_item_set_field_composite,
        summary="Set a single-select field (Status/Priority/Size) on a board item.",
        description=(
            "Sets a single-select field on a GitHub Projects-v2 board item -- "
            "Status, Priority, Size, or any other single-select field -- "
            "wrapping the updateProjectV2ItemFieldValue GraphQL mutation with a "
            "singleSelectOptionId value. Every argument (project_id, item_id, "
            "field_id, option_id) is a node id resolvable from "
            "gh.composite.project_view. A reversible board-hygiene write."
        ),
        parameter_schema=PROJECT_ITEM_SET_FIELD_PARAMETER_SCHEMA,
        response_schema=PROJECT_ITEM_SET_FIELD_RESPONSE_SCHEMA,
        group_key="board",
        tags=["composite", "write", "board", "projectv2", "graphql"],
        safety_level="caution",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="gh.composite.sub_issue_add",
        handler=sub_issue_add_composite,
        summary="Link a sub-issue to a parent issue (POST .../sub_issues).",
        description=(
            "Links a task to its parent as a GitHub sub-issue, wrapping "
            "POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues. "
            "sub_issue_id is the child issue's DATABASE id (the ``id`` field on "
            "the issue payload), NOT its issue number -- the REST create-issue "
            "response carries that id. The sub-issue must share the parent's "
            "repository owner. Set replace_parent=true to move an already-"
            "parented sub-issue. A reversible board-hygiene write."
        ),
        parameter_schema=SUB_ISSUE_ADD_PARAMETER_SCHEMA,
        response_schema=SUB_ISSUE_ADD_RESPONSE_SCHEMA,
        group_key="issues",
        tags=["composite", "write", "issues", "sub_issue"],
        safety_level="caution",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="gh.composite.repo_view",
        handler=repo_view_composite,
        summary=(
            "Read one repository: owner, visibility, default branch, topics, "
            "open issues, permissions."
        ),
        description=(
            "Reads one repository with GET /repos/{owner}/{repo} and returns a "
            "short summary: name, owner, visibility, default branch, archived "
            "flag, topics, open issue count, last push time, and what the "
            "connector's identity may do there (read, write, admin). Read-only."
        ),
        parameter_schema=REPO_VIEW_PARAMETER_SCHEMA,
        response_schema=REPO_VIEW_RESPONSE_SCHEMA,
        group_key="repos",
        tags=["composite", "read-only", "repos"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need the basic facts of one repository: whether it "
                "exists and is reachable, its owner and visibility, its default "
                "branch, whether it is archived, how many issues are open, when it "
                "was last pushed to, and whether the connector may read, write or "
                "administer it."
            ),
            "output_shape": (
                "{name, full_name, owner, owner_type, visibility, default_branch, "
                "archived, topics: [string], open_issues_count, pushed_at, "
                "permissions: {read, write, admin} | null}. open_issues_count "
                "counts open issues and open pull requests together."
            ),
            "next_step": (
                "A 404 means the repository does not exist or the connector's "
                "identity cannot see it. Use default_branch as the branch for "
                "later reads of files or commits. If permissions.write is false, "
                "do not plan a write to this repository."
            ),
        },
    ),
    _CompositeSpec(
        op_id="gh.composite.repo_create",
        handler=repo_create_composite,
        summary=(
            "Create a repository in an organisation: name, description, visibility, "
            "optional README."
        ),
        description=(
            "Creates one repository in an organisation with POST /orgs/{org}/repos. "
            "Private unless visibility is 'public'. With with_readme, GitHub makes a "
            "first commit. When a repository with that name already exists, nothing is "
            "written and status is 'unchanged'. A change that waits for approval."
        ),
        parameter_schema=REPO_CREATE_PARAMETER_SCHEMA,
        response_schema=REPO_CREATE_RESPONSE_SCHEMA,
        group_key="repos",
        tags=["composite", "write", "repos"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call when a person asks for a new repository in a GitHub organisation. "
                "Do not call to change an existing repository; use repo_view first if "
                "you are not sure the name is free."
            ),
            "preconditions": (
                "The connector's identity must be allowed to create repositories in "
                "the organisation. Pass visibility 'public' only when the person asked "
                "for a public repository by name; the default is private. The call "
                "waits until an approver accepts it."
            ),
            "output_shape": (
                "{status: 'created' | 'unchanged', name, full_name, owner, url, "
                "visibility, default_branch, guidance}. 'unchanged' means a repository "
                "with that name already exists and nothing was written."
            ),
            "next_step": (
                "On 'created', tell the person the url and default_branch. On "
                "'unchanged', say the name is taken and ask whether to use the existing "
                "repository or pick another name. A 403 means the identity may not "
                "create repositories there; a 422 means GitHub rejected the name."
            ),
        },
    ),
    _CompositeSpec(
        op_id="gh.composite.git_ref_view",
        handler=git_ref_view_composite,
        summary="Read the commit a branch or tag points to.",
        description=(
            "Reads GET /repos/{owner}/{repo}/git/ref/{ref} for a branch or tag "
            "name. MEHO builds the ref (heads/<name> or tags/<name>) itself. "
            "A missing branch or tag answers found=false, not an error. "
            "Read-only."
        ),
        parameter_schema=GIT_REF_VIEW_PARAMETER_SCHEMA,
        response_schema=GIT_REF_VIEW_RESPONSE_SCHEMA,
        group_key="git",
        tags=["composite", "read-only", "git", "ref", "branch", "tag"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GIT_REF_VIEW_LLM_INSTRUCTIONS,
    ),
    _CompositeSpec(
        op_id="gh.composite.git_tree_view",
        handler=git_tree_view_composite,
        summary="List the files and folders of a branch, tag, commit or tree.",
        description=(
            "Reads GET /repos/{owner}/{repo}/git/trees/{tree_sha}, with "
            "recursive=1 when asked, and returns each entry's path, type, mode, "
            "SHA-1 and size. A long list comes back behind a result handle. "
            "Read-only."
        ),
        parameter_schema=GIT_TREE_VIEW_PARAMETER_SCHEMA,
        response_schema=GIT_TREE_VIEW_RESPONSE_SCHEMA,
        group_key="git",
        tags=["composite", "read-only", "git", "tree", "files"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GIT_TREE_VIEW_LLM_INSTRUCTIONS,
    ),
    _CompositeSpec(
        op_id="gh.composite.branch_commit_files",
        handler=branch_commit_files_composite,
        summary="Write text files to a branch as one new commit (waits for approval).",
        description=(
            "Reads the branch, creates a tree with the files on top of its "
            "commit, creates the commit, and moves the branch in one update that "
            "GitHub applies only while the branch is still at base_sha, never "
            "with force. Otherwise the call stops with precondition_failed. "
            "Every call waits for approval."
        ),
        parameter_schema=BRANCH_COMMIT_FILES_PARAMETER_SCHEMA,
        response_schema=BRANCH_COMMIT_FILES_RESPONSE_SCHEMA,
        group_key="git",
        tags=["composite", "write", "git", "commit", "branch", "files"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=BRANCH_COMMIT_FILES_LLM_INSTRUCTIONS,
    ),
)


async def register_github_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every gh-rest composite into ``endpoint_descriptor``.

    Idempotent: a second invocation against unchanged descriptions is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`_register_in_session`). The runner
    (:func:`run_typed_op_registrars`) calls every registered registrar
    on every lifespan startup; the skip-re-embed branch keeps that
    cheap.

    Scope at T4: one composite -- ``gh.composite.pr_status_summary`` --
    with ``safety_level="safe"`` (the register-time equivalent of the
    operator-visible "read" label per the issue body) and
    ``requires_approval=False``. Future T7+ Tasks add write composites
    that inherit the helper's safe-by-default ``dangerous`` /
    ``requires_approval=True`` posture.

    Test seam: ``embedding_service`` lets test fixtures inject a stub
    so unit tests don't load the ONNX model. Production callers leave
    it ``None`` and each registration resolves the process-wide
    singleton.
    """
    for spec in _COMPOSITES:
        await register_composite_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=spec.op_id,
            handler=spec.handler,
            summary=spec.summary,
            description=spec.description,
            parameter_schema=spec.parameter_schema,
            response_schema=spec.response_schema,
            group_key=spec.group_key,
            when_to_use=_WHEN_TO_USE_BY_GROUP[spec.group_key],
            tags=spec.tags,
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            llm_instructions=spec.llm_instructions,
            embedding_service=embedding_service,
        )
