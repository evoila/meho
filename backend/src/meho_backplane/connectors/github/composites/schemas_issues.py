# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contracts of the four issue composites of the gh-rest connector.

Parameter schemas, response schemas and agent guidance (``llm_instructions``)
for ``gh.composite.issue_search``, ``gh.composite.issue_view``,
``gh.composite.issue_create`` and ``gh.composite.issue_comment``. The
handlers live in :mod:`._issues`.

Same conventions as :mod:`.schemas_git`: ``additionalProperties=False`` on
every parameter schema, a description on every field, and one example call
with neutral placeholders.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "ISSUE_COMMENT_LLM_INSTRUCTIONS",
    "ISSUE_COMMENT_PARAMETER_SCHEMA",
    "ISSUE_COMMENT_RESPONSE_SCHEMA",
    "ISSUE_CREATE_LLM_INSTRUCTIONS",
    "ISSUE_CREATE_PARAMETER_SCHEMA",
    "ISSUE_CREATE_RESPONSE_SCHEMA",
    "ISSUE_SEARCH_LLM_INSTRUCTIONS",
    "ISSUE_SEARCH_PARAMETER_SCHEMA",
    "ISSUE_SEARCH_RESPONSE_SCHEMA",
    "ISSUE_VIEW_LLM_INSTRUCTIONS",
    "ISSUE_VIEW_PARAMETER_SCHEMA",
    "ISSUE_VIEW_RESPONSE_SCHEMA",
    "MAX_BODY_CHARS",
    "MAX_COMMENTS",
    "MAX_RANKED_LIMIT",
]

#: GitHub's limit for an issue or comment body, in characters.
MAX_BODY_CHARS: Final[int] = 65536
#: Most newest comments one ``issue_view`` call returns.
MAX_COMMENTS: Final[int] = 50
#: Most hits one ranked search returns (one GitHub page).
MAX_RANKED_LIMIT: Final[int] = 100

_OWNER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "GitHub repository owner (user or organisation login).",
}
_REPO: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "GitHub repository name (without the owner prefix).",
}
_ISSUE_NUMBER: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "The issue number, the N in #N.",
}
_LOGIN: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 50,
    "description": "A GitHub user login.",
}
_LABEL: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 100,
    "description": "A label name. It may not hold a double quote or a comma.",
}
_STRING_LIST: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "description": "One name."},
}

_ISSUE_HIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One issue (or pull request) found.",
    "properties": {
        "number": {"type": "integer", "description": "The issue number."},
        "title": {"type": "string", "description": "The title."},
        "state": {"type": "string", "description": "'open' or 'closed'."},
        "labels": {**_STRING_LIST, "description": "The label names."},
        "assignees": {**_STRING_LIST, "description": "The logins of the assignees."},
        "author": {"type": ["string", "null"], "description": "Login of the author."},
        "created_at": {"type": ["string", "null"], "description": "When it was filed."},
        "updated_at": {"type": ["string", "null"], "description": "When it last changed."},
        "comments": {"type": ["integer", "null"], "description": "Number of comments."},
        "html_url": {"type": ["string", "null"], "description": "Web link to the issue."},
        "is_pull_request": {
            "type": "boolean",
            "description": "True when the hit is a pull request, not an issue.",
        },
        "suggestion": {
            "type": "boolean",
            "description": (
                "True in ranked mode: GitHub thinks the hit fits, but it may not. "
                "Read it before you rely on it. False in exact mode."
            ),
        },
    },
}


ISSUE_SEARCH_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "mode": {
            "type": "string",
            "enum": ["exact", "ranked"],
            "default": "exact",
            "description": (
                "'exact' (default): only issues that match every filter, and the phrase "
                "when one is given, newest first. 'ranked': free text in GitHub's "
                "relevance order; each hit is only a suggestion."
            ),
        },
        "state": {
            "type": "string",
            "enum": ["open", "closed", "all"],
            "default": "open",
            "description": "Which issues: 'open' (default), 'closed' or 'all'.",
        },
        "labels": {
            "type": "array",
            "maxItems": 20,
            "items": _LABEL,
            "description": "Only issues that carry every one of these labels.",
        },
        "assignee": {**_LOGIN, "description": "Only issues assigned to this login."},
        "author": {**_LOGIN, "description": "Only issues filed by this login."},
        "phrase": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": (
                "Exact mode only: an exact phrase the issue must hold (in its title, "
                "body or comments). No double quotes."
            ),
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": (
                "Ranked mode only, and needed there: free text, like 'login fails after "
                "upgrade'. It may not name another repository, user or organisation, "
                "and may not use OR, AND, NOT or brackets."
            ),
        },
        "include_pull_requests": {
            "type": "boolean",
            "default": False,
            "description": "False (default): leave pull requests out. True: keep them.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RANKED_LIMIT,
            "default": 30,
            "description": (
                f"Ranked mode only: the most hits to return (1 to {MAX_RANKED_LIMIT}, "
                "default 30). Exact mode always returns every match it can."
            ),
        },
    },
    "examples": [
        {
            "owner": "<owner>",
            "repo": "<repo>",
            "mode": "exact",
            "state": "open",
            "labels": ["bug"],
        }
    ],
    "description": "Parameters for gh.composite.issue_search: find issues in one repository.",
}

ISSUE_SEARCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["mode", "complete", "total_count", "count", "reason", "query", "issues"],
    "properties": {
        "mode": {"type": "string", "description": "The mode that ran: 'exact' or 'ranked'."},
        "complete": {
            "type": "boolean",
            "description": (
                "True when the list holds every match. False when GitHub said the "
                "results are incomplete, when there are more than 1,000 hits (GitHub's "
                "search limit), or when a ranked search returned only its limit."
            ),
        },
        "total_count": {
            "type": ["integer", "null"],
            "description": (
                "The number of matches GitHub reported. Null when MEHO read the plain "
                "issue list (exact mode with no phrase), which has no count."
            ),
        },
        "count": {"type": "integer", "description": "Number of issues in this answer."},
        "reason": {
            "type": ["string", "null"],
            "description": "Why the list is not complete. Null when it is complete.",
        },
        "query": {
            "type": ["string", "null"],
            "description": (
                "The search text MEHO sent to GitHub. It always starts with "
                "'repo:<owner>/<repo>'. Null when MEHO read the plain issue list."
            ),
        },
        "issues": {
            "type": "array",
            "description": "The issues found. Exact mode: newest first.",
            "items": _ISSUE_HIT_SCHEMA,
        },
    },
}

ISSUE_SEARCH_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to find issues in one repository: by state, labels, assignee or author "
        "(mode 'exact'), with an exact phrase, or by free text when you do not know "
        "the exact words (mode 'ranked'). Check here before you file a new issue, so "
        "you do not file one twice."
    ),
    "parameter_hints": {
        "mode": "Use 'exact' to list or count. Use 'ranked' only to look for similar issues.",
        "phrase": "Exact mode only. Words that must appear together, without quotes.",
        "query": "Ranked mode only, and needed there. Plain words, no qualifiers like repo:.",
        "labels": "Every label must match (AND), not any of them.",
    },
    "output_shape": (
        "{mode, complete, total_count, count, reason, query, issues: [{number, title, "
        "state, labels, assignees, author, created_at, updated_at, comments, html_url, "
        "is_pull_request, suggestion}]}. A long list is behind a result handle; read it "
        "with result_query."
    ),
    "next_step": (
        "If complete is false, say so and add filters to narrow the search. Treat "
        "ranked hits (suggestion=true) as candidates: open each with "
        "gh.composite.issue_view before you rely on it. To file a new issue, call "
        "gh.composite.issue_create."
    ),
    "result_scalars": {"keys": ["mode", "complete", "total_count", "count", "reason", "query"]},
}


ISSUE_VIEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "issue_number"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "issue_number": _ISSUE_NUMBER,
        "comments_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_COMMENTS,
            "default": 0,
            "description": (
                f"How many of the newest comments to return (0 to {MAX_COMMENTS}). "
                "0 (default) returns none; the number of comments is always there."
            ),
        },
    },
    "examples": [{"owner": "<owner>", "repo": "<repo>", "issue_number": 42, "comments_limit": 5}],
    "description": "Parameters for gh.composite.issue_view: read one issue.",
}

_LINKED_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A linked issue, in short.",
    "properties": {
        "number": {"type": "integer", "description": "The issue number."},
        "id": {"type": "integer", "description": "The issue's database id."},
        "title": {"type": "string", "description": "The title."},
        "state": {"type": "string", "description": "'open' or 'closed'."},
        "repository": {
            "type": ["string", "null"],
            "description": "'<owner>/<repo>' of the issue. It may be another repository.",
        },
        "html_url": {"type": ["string", "null"], "description": "Web link to the issue."},
    },
}

_COMMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One comment.",
    "properties": {
        "id": {"type": "integer", "description": "The comment's id."},
        "author": {"type": ["string", "null"], "description": "Login of the author."},
        "created_at": {"type": ["string", "null"], "description": "When it was written."},
        "body": {"type": ["string", "null"], "description": "The text of the comment."},
        "html_url": {"type": ["string", "null"], "description": "Web link to the comment."},
    },
}

ISSUE_VIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "number",
        "id",
        "node_id",
        "title",
        "body",
        "state",
        "labels",
        "assignees",
        "parent",
        "sub_issues",
        "comment_count",
        "comments",
    ],
    "properties": {
        "number": {"type": "integer", "description": "The issue number."},
        "id": {"type": "integer", "description": "Database id, for gh.composite.sub_issue_add."},
        "node_id": {
            "type": "string",
            "description": "Node id, for gh.composite.project_item_add.",
        },
        "title": {"type": "string", "description": "The title."},
        "body": {"type": ["string", "null"], "description": "The text of the issue."},
        "state": {"type": "string", "description": "'open' or 'closed'."},
        "state_reason": {
            "type": ["string", "null"],
            "description": "Why it was closed ('completed', 'not_planned', ...), or null.",
        },
        "labels": {**_STRING_LIST, "description": "The label names."},
        "assignees": {**_STRING_LIST, "description": "The logins of the assignees."},
        "author": {"type": ["string", "null"], "description": "Login of the author."},
        "created_at": {"type": ["string", "null"], "description": "When it was filed."},
        "updated_at": {"type": ["string", "null"], "description": "When it last changed."},
        "closed_at": {"type": ["string", "null"], "description": "When it was closed, or null."},
        "html_url": {"type": ["string", "null"], "description": "Web link to the issue."},
        "is_pull_request": {
            "type": "boolean",
            "description": "True when the number belongs to a pull request.",
        },
        "parent": {
            **_LINKED_ISSUE_SCHEMA,
            "type": ["object", "null"],
            "description": "The parent issue, or null when the issue has none.",
        },
        "sub_issues": {
            "type": "array",
            "description": "The direct sub-issues, in GitHub's order.",
            "items": _LINKED_ISSUE_SCHEMA,
        },
        "comment_count": {"type": "integer", "description": "The number of comments."},
        "comments": {
            "type": "array",
            "description": "The newest comments asked for, oldest of them first.",
            "items": _COMMENT_SCHEMA,
        },
    },
}

ISSUE_VIEW_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to read one issue: its title, text, state, labels, assignees, its parent, "
        "its direct sub-issues and how many comments it has, and optionally the newest "
        "comments."
    ),
    "parameter_hints": {
        "issue_number": "The N in #N, not the database id.",
        "comments_limit": "Set it only when you need the discussion; 0 keeps the answer short.",
    },
    "output_shape": (
        "{number, id, node_id, title, body, state, state_reason, labels, assignees, "
        "author, created_at, updated_at, closed_at, html_url, is_pull_request, parent: "
        "{number, id, title, state, repository, html_url} | null, sub_issues: [...], "
        "comment_count, comments: [{id, author, created_at, body, html_url}]}."
    ),
    "next_step": (
        "A 404 means the issue does not exist or the connector cannot see it. Use id with "
        "gh.composite.sub_issue_add and node_id with gh.composite.project_item_add. To "
        "answer, call gh.composite.issue_comment."
    ),
}


ISSUE_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "title"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "The title of the new issue.",
        },
        "body": {
            "type": "string",
            "maxLength": MAX_BODY_CHARS,
            "description": "The text of the new issue, in Markdown.",
        },
        "labels": {
            "type": "array",
            "maxItems": 20,
            "items": _LABEL,
            "description": "Label names to put on the issue. They should exist in the repository.",
        },
        "assignees": {
            "type": "array",
            "maxItems": 10,
            "items": _LOGIN,
            "description": "Logins to assign the issue to.",
        },
    },
    "examples": [
        {
            "owner": "<owner>",
            "repo": "<repo>",
            "title": "Backup job fails on the second node",
            "body": "What happens, and how to see it.",
            "labels": ["bug"],
        }
    ],
    "description": "Parameters for gh.composite.issue_create: file one issue.",
}

ISSUE_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status", "number", "id", "node_id", "html_url", "title", "labels", "assignees"],
    "properties": {
        "status": {"type": "string", "enum": ["created"], "description": "Always 'created'."},
        "number": {"type": "integer", "description": "The number of the new issue."},
        "id": {
            "type": "integer",
            "description": "Database id; pass it as sub_issue_id to gh.composite.sub_issue_add.",
        },
        "node_id": {
            "type": "string",
            "description": "Node id; pass it as content_id to gh.composite.project_item_add.",
        },
        "html_url": {"type": ["string", "null"], "description": "Web link to the new issue."},
        "title": {"type": "string", "description": "The title as GitHub stored it."},
        "labels": {
            **_STRING_LIST,
            "description": "The labels GitHub set. GitHub drops labels it does not allow.",
        },
        "assignees": {
            **_STRING_LIST,
            "description": "The assignees GitHub set. GitHub drops logins it does not allow.",
        },
    },
}

ISSUE_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to file one new issue in a repository, when a person asks for a ticket or "
        "a runbook says to file one."
    ),
    "preconditions": (
        "Search first with gh.composite.issue_search, so you do not file the same issue "
        "twice. The connector's identity needs write access to issues in the repository. "
        "The call waits until an approver accepts it. Labels and assignees need push "
        "access; without it GitHub drops them without an error."
    ),
    "parameter_hints": {
        "labels": "Use labels that already exist in the repository.",
        "assignees": "Logins only, without '@'.",
    },
    "output_shape": (
        "{status: 'created', number, id, node_id, html_url, title, labels, assignees}."
    ),
    "next_step": (
        "Report number and html_url. To put the issue under a parent, call "
        "gh.composite.sub_issue_add with sub_issue_id=id. To add it to a board, call "
        "gh.composite.project_item_add with content_id=node_id. Compare labels and "
        "assignees with what you asked for; GitHub may have dropped some."
    ),
}


ISSUE_COMMENT_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "issue_number", "body"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "issue_number": _ISSUE_NUMBER,
        "body": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_BODY_CHARS,
            "description": "The text of the comment, in Markdown.",
        },
    },
    "examples": [
        {
            "owner": "<owner>",
            "repo": "<repo>",
            "issue_number": 42,
            "body": "The backup ran again and passed.",
        }
    ],
    "description": "Parameters for gh.composite.issue_comment: add a comment to one issue.",
}

ISSUE_COMMENT_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status", "issue_number", "comment_id", "html_url", "created_at"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["commented"],
            "description": "Always 'commented'.",
        },
        "issue_number": {"type": "integer", "description": "Echo of the issue number."},
        "comment_id": {"type": "integer", "description": "The id of the new comment."},
        "html_url": {"type": ["string", "null"], "description": "Web link to the new comment."},
        "created_at": {"type": ["string", "null"], "description": "When it was written."},
    },
}

ISSUE_COMMENT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to add a comment to one issue (or pull request), for example to report "
        "progress or a result."
    ),
    "preconditions": (
        "Read the issue first with gh.composite.issue_view, so the comment fits the "
        "discussion. The connector's identity needs write access to issues in the "
        "repository. The call waits until an approver accepts it."
    ),
    "output_shape": "{status: 'commented', issue_number, comment_id, html_url, created_at}.",
    "next_step": (
        "Report html_url. A 404 means the issue does not exist; a 403 means the identity "
        "may not comment there; a 410 means the issue was deleted."
    ),
}
