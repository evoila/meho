# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter + response schemas for gh-rest composites.

Same conventions as the vmware-rest composite schemas module
(G3.1-T5 #508 / T6 #509):

* ``additionalProperties=False`` on every parameter schema so an
  operator typo on an optional key surfaces as a clear validation error
  rather than disappearing through a permissive shape.
* Schemas declare only what the handler reads. Per-composite
  documentation lives on ``description`` keys; ``describe_operation``
  surfaces the schema verbatim to LLM clients.
* T4 ships exactly one composite -- ``gh.composite.pr_status_summary``
  -- which is read-only (registered ``safety_level="safe"`` --
  the register-time enum value for the issue body's "read" label --
  / ``requires_approval=False``). #2081 adds the four board + sub-issue
  composites (``project_view`` read; ``project_item_add`` /
  ``project_item_set_field`` / ``sub_issue_add`` writes) and reuses this
  module's pattern.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PROJECT_ITEM_ADD_PARAMETER_SCHEMA",
    "PROJECT_ITEM_ADD_RESPONSE_SCHEMA",
    "PROJECT_ITEM_SET_FIELD_PARAMETER_SCHEMA",
    "PROJECT_ITEM_SET_FIELD_RESPONSE_SCHEMA",
    "PROJECT_VIEW_PARAMETER_SCHEMA",
    "PROJECT_VIEW_RESPONSE_SCHEMA",
    "PR_STATUS_SUMMARY_PARAMETER_SCHEMA",
    "PR_STATUS_SUMMARY_RESPONSE_SCHEMA",
    "REPO_CREATE_PARAMETER_SCHEMA",
    "REPO_CREATE_RESPONSE_SCHEMA",
    "REPO_VIEW_PARAMETER_SCHEMA",
    "REPO_VIEW_RESPONSE_SCHEMA",
    "SUB_ISSUE_ADD_PARAMETER_SCHEMA",
    "SUB_ISSUE_ADD_RESPONSE_SCHEMA",
]


PR_STATUS_SUMMARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "pull_number"],
    "properties": {
        "owner": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository owner (user or organisation login).",
        },
        "repo": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository name (without the owner prefix).",
        },
        "pull_number": {
            "type": "integer",
            "minimum": 1,
            "description": "The pull-request number (the integer suffix in /pull/<N> URLs).",
        },
    },
    "description": (
        "Parameters for gh.composite.pr_status_summary. Returns the PR "
        "metadata, the head-commit check runs, the reviews, and the "
        "mergeable state in a single envelope -- answers 'is this PR "
        "ready to merge?' without three separate L2 calls."
    ),
}


PR_STATUS_SUMMARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "pr",
        "checks",
        "reviews",
        "mergeable",
        "mergeable_state",
        "checks_status",
        "review_status",
    ],
    "properties": {
        "pr": {
            "description": (
                "Pull-request payload from GET /repos/{owner}/{repo}/pulls/"
                "{pull_number}. The composite surfaces it verbatim so an "
                "operator can read any field the upstream returns. Required "
                "(the composite errors out when the PR itself cannot be "
                "fetched -- partial-failure tolerance applies only to the "
                "checks and reviews sub-calls)."
            ),
        },
        "checks": {
            "description": (
                "Response from GET /repos/{owner}/{repo}/commits/{ref}/"
                "check-runs against the PR's head SHA. ``null`` when the "
                "sub-call failed (e.g. 404 because the repo has no checks "
                "configured); the composite degrades gracefully on this "
                "particular sub-call rather than aborting the whole "
                "envelope. ``checks_status`` summarises whether checks "
                "are all-passing / any-failed / pending / unknown."
            ),
        },
        "reviews": {
            "description": (
                "List of review records from GET /repos/{owner}/{repo}/"
                "pulls/{pull_number}/reviews. ``null`` when the sub-call "
                "failed; the composite degrades gracefully here too. "
                "``review_status`` summarises the latest disposition per "
                "reviewer (approved / changes_requested / commented / "
                "pending / unknown)."
            ),
        },
        "mergeable": {
            "type": ["boolean", "null"],
            "description": (
                "PR's ``mergeable`` field. GitHub computes this "
                "asynchronously after a push and returns ``null`` until "
                "the background job runs; the composite passes that "
                "tri-state through verbatim."
            ),
        },
        "mergeable_state": {
            "type": ["string", "null"],
            "description": (
                "PR's ``mergeable_state`` (``clean``, ``dirty``, "
                "``blocked``, ``behind``, ``unknown``, etc.). "
                "Pass-through from the upstream payload."
            ),
        },
        "checks_status": {
            "type": "string",
            "enum": [
                "all_passed",
                "any_failed",
                "pending",
                "no_checks",
                "unknown",
            ],
            "description": (
                "Composite-computed summary of the check-runs payload. "
                "``unknown`` when the checks sub-call failed; "
                "``no_checks`` when the call succeeded but returned an "
                "empty array; otherwise derived from the per-run "
                "``conclusion`` / ``status`` fields."
            ),
        },
        "review_status": {
            "type": "string",
            "enum": [
                "approved",
                "changes_requested",
                "commented",
                "pending",
                "no_reviews",
                "unknown",
            ],
            "description": (
                "Composite-computed summary of the reviews payload. "
                "``unknown`` when the reviews sub-call failed; "
                "``no_reviews`` when it returned an empty array; "
                "otherwise derived from the latest-per-reviewer "
                "disposition with ``changes_requested`` as the dominant "
                "veto."
            ),
        },
    },
    "description": (
        "Aggregated PR-status envelope. Designed for the 'is this PR "
        "ready to merge?' agent question -- one composite call yields "
        "the four signals the LLM client needs (metadata, checks, "
        "reviews, mergeable) plus two pre-computed summaries that "
        "collapse the per-record arrays into agent-actionable states."
    ),
}


# ===========================================================================
# gh.composite.project_view (Projects-v2 board read, #2081)
# ===========================================================================


PROJECT_VIEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "project_number"],
    "properties": {
        "owner": {
            "type": "string",
            "minLength": 1,
            "description": "The org or user login that owns the Projects-v2 board.",
        },
        "project_number": {
            "type": "integer",
            "minimum": 1,
            "description": "The board's number (the integer in its /projects/<N> URL).",
        },
        "owner_type": {
            "type": "string",
            "enum": ["organization", "user"],
            "default": "organization",
            "description": (
                "Whether ``owner`` is an organization or a user login -- picks the "
                "GraphQL root selector. Defaults to ``organization``."
            ),
        },
        "first": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
            "description": "How many board items to return (GraphQL page size, max 100).",
        },
    },
    "description": (
        "Parameters for gh.composite.project_view. Reads a Projects-v2 board's "
        "node id, its fields (single-select fields include their option ids), "
        "and its current items in one GraphQL call -- the node ids the "
        "project_item_add / project_item_set_field write composites consume."
    ),
}


PROJECT_VIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["project_id", "title", "fields", "items", "item_count"],
    "properties": {
        "project_id": {
            "type": ["string", "null"],
            "description": "The board's GraphQL node id -- input to the write composites.",
        },
        "title": {"type": ["string", "null"], "description": "The board's title."},
        "fields": {
            "type": "array",
            "description": (
                "Board fields. Each is ``{id, name, data_type, options}``; "
                "``options`` is a list of ``{id, name}`` for single-select fields "
                "(the option ids project_item_set_field needs) and ``null`` otherwise."
            ),
            "items": {"type": "object"},
        },
        "items": {
            "type": "array",
            "description": (
                "Board items. Each is ``{item_id, content_type, number, title, url, "
                "field_values}``; ``number`` / ``url`` are ``null`` for draft issues."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": ["string", "null"],
                        "description": "Board item's node id (input to project_item_set_field).",
                    },
                    "content_type": {
                        "type": ["string", "null"],
                        "description": "Content type (Issue / PullRequest / DraftIssue).",
                    },
                    "number": {
                        "type": ["integer", "null"],
                        "description": "The issue / PR number; ``null`` for draft issues.",
                    },
                    "title": {"type": ["string", "null"], "description": "The item's title."},
                    "url": {
                        "type": ["string", "null"],
                        "description": "The issue / PR URL; ``null`` for draft issues.",
                    },
                    "field_values": {
                        "type": "object",
                        "description": (
                            "The item's current single-select (Status / Priority / Size) and "
                            'text field values, keyed by field name -- e.g. ``{"Status": "In '
                            'Progress", "Priority": "high"}``. Only single-select and text '
                            "field types are projected; other value types (iteration / number "
                            "/ date / repository / labels / ...) are omitted. Empty when the "
                            "item has no such field set."
                        ),
                    },
                },
            },
        },
        "item_count": {
            "type": ["integer", "null"],
            "description": "Total board item count (items.totalCount), not just this page.",
        },
    },
    "description": "A Projects-v2 board snapshot: node id, fields (with option ids), and items.",
}


# ===========================================================================
# gh.composite.project_item_add (Projects-v2 board write, #2081)
# ===========================================================================


PROJECT_ITEM_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "content_id"],
    "properties": {
        "project_id": {
            "type": "string",
            "minLength": 1,
            "description": "The board's GraphQL node id (from gh.composite.project_view).",
        },
        "content_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The issue or PR GraphQL node id (the ``node_id`` field on the "
                "REST issue/PR payload) to add to the board."
            ),
        },
    },
    "description": (
        "Parameters for gh.composite.project_item_add. Wraps the "
        "addProjectV2ItemById GraphQL mutation."
    ),
}


PROJECT_ITEM_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["item_id", "project_id", "content_id", "status"],
    "properties": {
        "item_id": {"type": "string", "description": "The new board item's GraphQL node id."},
        "project_id": {"type": "string", "description": "Echo of the board node id."},
        "content_id": {"type": "string", "description": "Echo of the added content node id."},
        "status": {
            "type": "string",
            "enum": ["added"],
            "description": "Always ``added`` on success.",
        },
    },
    "description": "The board item created by addProjectV2ItemById.",
}


# ===========================================================================
# gh.composite.project_item_set_field (Projects-v2 board write, #2081)
# ===========================================================================


PROJECT_ITEM_SET_FIELD_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "item_id", "field_id", "option_id"],
    "properties": {
        "project_id": {
            "type": "string",
            "minLength": 1,
            "description": "The board's GraphQL node id (from gh.composite.project_view).",
        },
        "item_id": {
            "type": "string",
            "minLength": 1,
            "description": "The board item's node id (from project_view or project_item_add).",
        },
        "field_id": {
            "type": "string",
            "minLength": 1,
            "description": "The single-select field's node id (from project_view fields).",
        },
        "option_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The single-select option's id (from the field's ``options`` in "
                "gh.composite.project_view) to set the field to."
            ),
        },
    },
    "description": (
        "Parameters for gh.composite.project_item_set_field. Wraps the "
        "updateProjectV2ItemFieldValue GraphQL mutation with a single-select value."
    ),
}


PROJECT_ITEM_SET_FIELD_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["item_id", "field_id", "option_id", "status"],
    "properties": {
        "item_id": {"type": "string", "description": "The updated board item's node id."},
        "field_id": {"type": "string", "description": "Echo of the field node id set."},
        "option_id": {"type": "string", "description": "Echo of the option id applied."},
        "status": {
            "type": "string",
            "enum": ["updated"],
            "description": "Always ``updated`` on success.",
        },
    },
    "description": "The board item updated by updateProjectV2ItemFieldValue.",
}


# ===========================================================================
# gh.composite.sub_issue_add (sub-issue linkage REST write, #2081)
# ===========================================================================


SUB_ISSUE_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "issue_number", "sub_issue_id"],
    "properties": {
        "owner": {
            "type": "string",
            "minLength": 1,
            "description": "The parent issue's repository owner (user or org login).",
        },
        "repo": {
            "type": "string",
            "minLength": 1,
            "description": "The parent issue's repository name (without the owner prefix).",
        },
        "issue_number": {
            "type": "integer",
            "minimum": 1,
            "description": "The parent issue's number (the ``#N`` the sub-issue is linked under).",
        },
        "sub_issue_id": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "The sub-issue's DATABASE id (the ``id`` field on the issue payload), "
                "NOT its issue number. The REST create-issue response carries this id. "
                "The sub-issue must belong to the same repository owner as the parent."
            ),
        },
        "replace_parent": {
            "type": "boolean",
            "description": (
                "When ``true``, move a sub-issue that already has a different parent "
                "under this one. Omit to use GitHub's default (reject if already parented)."
            ),
        },
    },
    "description": (
        "Parameters for gh.composite.sub_issue_add. Wraps "
        "POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues."
    ),
}


SUB_ISSUE_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["parent", "parent_number", "sub_issue_id", "status"],
    "properties": {
        "parent": {
            "description": (
                "The parent issue payload GitHub returns (201). Surfaced verbatim so "
                "the caller can read the updated ``sub_issues_summary`` count."
            ),
        },
        "parent_number": {"type": "integer", "description": "Echo of the parent issue number."},
        "sub_issue_id": {
            "type": "integer",
            "description": "Echo of the linked sub-issue database id.",
        },
        "status": {
            "type": "string",
            "enum": ["linked"],
            "description": "Always ``linked`` on success.",
        },
    },
    "description": "The parent issue after the sub-issue was linked.",
}


# ===========================================================================
# gh.composite.repo_view (one repository, read)
# ===========================================================================


REPO_VIEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo"],
    "properties": {
        "owner": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository owner (user or organisation login).",
        },
        "repo": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository name (without the owner prefix).",
        },
    },
    "examples": [{"owner": "<owner>", "repo": "<repo>"}],
    "description": (
        "Parameters for gh.composite.repo_view. Reads one repository "
        "(GET /repos/{owner}/{repo}) and returns a short summary of it."
    ),
}


REPO_VIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "name",
        "full_name",
        "owner",
        "owner_type",
        "visibility",
        "default_branch",
        "archived",
        "topics",
        "open_issues_count",
        "pushed_at",
        "permissions",
    ],
    "properties": {
        "name": {"type": "string", "description": "Repository name without the owner."},
        "full_name": {"type": "string", "description": "Owner and name, as 'owner/repo'."},
        "owner": {
            "type": "string",
            "description": "Login of the user or organisation that owns it.",
        },
        "owner_type": {
            "type": ["string", "null"],
            "description": "Kind of owner: 'User' or 'Organization'.",
        },
        "visibility": {
            "type": ["string", "null"],
            "description": "'public', 'private' or 'internal'.",
        },
        "default_branch": {"type": "string", "description": "Name of the default branch."},
        "archived": {
            "type": "boolean",
            "description": "True when the repository is archived (read-only).",
        },
        "topics": {
            "type": "array",
            "items": {"type": "string", "description": "One topic label."},
            "description": "Topic labels set on the repository.",
        },
        "open_issues_count": {
            "type": "integer",
            "description": (
                "Number of open issues and pull requests together, as GitHub counts them."
            ),
        },
        "pushed_at": {
            "type": ["string", "null"],
            "description": "Time of the last push, as an ISO 8601 timestamp.",
        },
        "permissions": {
            "type": ["object", "null"],
            "description": (
                "What the connector's own identity may do in this repository. "
                "null when GitHub does not report it."
            ),
            "properties": {
                "read": {"type": "boolean", "description": "May read the code and issues."},
                "write": {"type": "boolean", "description": "May push commits and change issues."},
                "admin": {"type": "boolean", "description": "May change the repository settings."},
            },
        },
    },
    "description": (
        "Short summary of one repository. Only these fields, not the full vendor answer."
    ),
}


# One GitHub owner or repository name. The lookahead after ``$`` matters:
# the validator is Python ``re``, where ``$`` also matches before a final
# newline, so without it ``"name\n"`` would pass.
_GITHUB_NAME_PATTERN = "^[A-Za-z0-9._-]+$(?!\\n)"

REPO_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["org", "name"],
    "properties": {
        "org": {
            "type": "string",
            "minLength": 1,
            "pattern": _GITHUB_NAME_PATTERN,
            "description": (
                "Login of the GitHub organisation that will own the repository. "
                "Letters, digits, '.', '-' and '_' only."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": _GITHUB_NAME_PATTERN,
            "description": (
                "Name of the new repository (without the organisation prefix). "
                "Letters, digits, '.', '-' and '_' only."
            ),
        },
        "description": {
            "type": "string",
            "description": "Short text shown under the repository name. Optional.",
        },
        "visibility": {
            "type": "string",
            "enum": ["private", "public"],
            "description": (
                "Who can see the repository. 'private' when omitted. "
                "Pass 'public' only when the caller asks for a public repository."
            ),
        },
        "with_readme": {
            "type": "boolean",
            "description": (
                "When true, GitHub makes a first commit with an empty README, so the "
                "default branch exists at once. False when omitted (empty repository)."
            ),
        },
    },
    "examples": [{"org": "example-org", "name": "example-repo", "visibility": "private"}],
    "description": (
        "Parameters for gh.composite.repo_create. Creates one repository in an "
        "organisation (POST /orgs/{org}/repos). Private unless visibility is 'public'."
    ),
}
REPO_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "status",
        "name",
        "full_name",
        "owner",
        "url",
        "visibility",
        "default_branch",
        "guidance",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created", "unchanged"],
            "description": (
                "'created' when the repository was made. 'unchanged' when a repository "
                "with that name already exists; then nothing was written."
            ),
        },
        "name": {"type": "string", "description": "Repository name without the owner."},
        "full_name": {"type": "string", "description": "Owner and name, as 'org/repo'."},
        "owner": {"type": "string", "description": "Login of the organisation that owns it."},
        "url": {"type": "string", "description": "Web address of the repository."},
        "visibility": {
            "type": ["string", "null"],
            "description": "'public', 'private' or 'internal'.",
        },
        "default_branch": {
            "type": ["string", "null"],
            "description": "Name of the default branch, for example 'main'.",
        },
        "guidance": {
            "type": "string",
            "description": "One sentence that says what happened.",
        },
    },
    "description": (
        "Result of gh.composite.repo_create: what happened, and the main facts of the "
        "repository (new or already existing)."
    ),
}
