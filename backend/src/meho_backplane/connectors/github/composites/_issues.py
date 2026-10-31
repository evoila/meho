# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Issue composites of the gh-rest connector: search, read, file and comment.

Four hand-written operations for daily ticket work, so an agent or an
add-on needs no raw generated operation and no knowledge of GitHub's API:

* ``gh.composite.issue_search`` -- find issues in one repository. Two modes:

  * ``exact`` (default): filters only (state, labels, assignee, author) and
    an optional exact phrase, newest first. Without a phrase MEHO reads the
    plain issue list (``GET /repos/{owner}/{repo}/issues``) page by page to
    the end, so the list is complete. With a phrase MEHO uses GitHub's
    search (``GET /search/issues``) with the phrase in quotes.
  * ``ranked``: free text through GitHub's search, in GitHub's relevance
    order. Each hit is marked ``suggestion``.

  Every search query starts with ``repo:<owner>/<repo>``. The free text may
  not hold a scope qualifier (``repo:``, ``org:``, ``user:``, ``owner:``) or
  a boolean operator, so a search never leaves the named repository. Every
  answer carries ``mode``, ``complete`` and ``total_count``.
* ``gh.composite.issue_view`` -- one issue with its parent, its direct
  sub-issues, its number of comments and, on request, the newest comments.
* ``gh.composite.issue_create`` -- file one issue. The answer carries the
  number, the node id (for ``project_item_add``) and the database id (for
  ``sub_issue_add``).
* ``gh.composite.issue_comment`` -- add one comment to one issue.

All four use the direct-session substrate (``connector._get_json`` /
``_post_json`` through :class:`._git.GitRepo`), so they work on a fresh
install with no spec import. Owner and repository names are checked and
percent-encoded by ``GitRepo``.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.github.composites._git import GitRepo

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = [
    "MAX_LIST_PAGES",
    "SEARCH_RESULT_LIMIT",
    "build_search_query",
    "issue_comment_composite",
    "issue_create_composite",
    "issue_search_composite",
    "issue_view_composite",
]

_ISSUES_PATH: Final = "/repos/{owner}/{repo}/issues"
_ISSUE_PATH: Final = "/repos/{owner}/{repo}/issues/{number}"
_PARENT_PATH: Final = "/repos/{owner}/{repo}/issues/{number}/parent"
_SUB_ISSUES_PATH: Final = "/repos/{owner}/{repo}/issues/{number}/sub_issues"
_COMMENTS_PATH: Final = "/repos/{owner}/{repo}/issues/{number}/comments"
_SEARCH_PATH: Final = "/search/issues"

#: GitHub's largest page size for the issue list, search and sub-issues.
_PAGE_SIZE: Final[int] = 100
#: GitHub's search returns at most 1,000 hits for one query.
SEARCH_RESULT_LIMIT: Final[int] = 1000
#: Most pages of the plain issue list one exact search reads (10,000 rows).
MAX_LIST_PAGES: Final[int] = 100
#: Most pages of sub-issues one ``issue_view`` reads.
_MAX_SUB_ISSUE_PAGES: Final[int] = 10

# A GitHub login: letters, digits and '-', at most 39 characters; app
# accounts end in '[bot]'.
_LOGIN: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
# Qualifiers that would widen a search past the named repository.
_SCOPE_QUALIFIER: Final = re.compile(r"(?i)(?:^|[\s(\-])(?:repo|org|user|owner)\s*:")
# Boolean operators and brackets would let free text regroup the query.
_BOOLEAN: Final = re.compile(r"(?:^|\s)(?:OR|AND|NOT)(?:\s|$)|[()]")
_REPO_FROM_URL: Final = re.compile(r"/repos/([^/]+/[^/]+)$")


def _check_login(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not _LOGIN.fullmatch(value):
        raise ValueError(f"{what}: {value!r} is not a GitHub login")
    return value


def _check_labels(values: object) -> list[str]:
    labels = list(values) if isinstance(values, list) else []
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("labels: a label name is empty")
        if '"' in label or "," in label or any(ord(c) < 32 for c in label):
            raise ValueError(f"labels: {label!r} may not hold a double quote, comma or control")
    return labels


def _check_free_text(value: str, *, what: str) -> str:
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"{what}: control characters are not allowed")
    if _SCOPE_QUALIFIER.search(value):
        raise ValueError(f"{what}: may not name a repository, user, owner or organisation")
    return value


def build_search_query(
    *,
    owner: str,
    repo: str,
    state: str,
    labels: list[str],
    assignee: str | None,
    author: str | None,
    include_pull_requests: bool,
    phrase: str | None = None,
    text: str | None = None,
) -> str:
    """Return the ``q`` value for ``GET /search/issues``.

    The query always starts with ``repo:<owner>/<repo>``. ``phrase`` goes in
    double quotes (exact match); ``text`` goes last, as free words. Both are
    checked first: no scope qualifier, no control character; the phrase has
    no double quote; the text has no boolean operator or bracket.
    """
    parts = [f"repo:{owner}/{repo}"]
    if not include_pull_requests:
        parts.append("is:issue")
    if state != "all":
        parts.append(f"is:{state}")
    parts.extend(f'label:"{label}"' for label in labels)
    if assignee is not None:
        parts.append(f"assignee:{assignee}")
    if author is not None:
        parts.append(f"author:{author}")
    if phrase is not None:
        _check_free_text(phrase, what="phrase")
        if '"' in phrase:
            raise ValueError("phrase: may not hold a double quote")
        parts.append(f'"{phrase}"')
    if text is not None:
        _check_free_text(text, what="query")
        if _BOOLEAN.search(text):
            raise ValueError("query: may not use OR, AND, NOT or brackets")
        parts.append(text)
    return " ".join(parts)


def _names(values: object, key: str) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v[key]) for v in values if isinstance(v, dict) and v.get(key) is not None]


def _login(user: object) -> str | None:
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _hit(raw: dict[str, Any], *, suggestion: bool) -> dict[str, Any]:
    return {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "labels": _names(raw.get("labels"), "name"),
        "assignees": _names(raw.get("assignees"), "login"),
        "author": _login(raw.get("user")),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "comments": raw.get("comments"),
        "html_url": raw.get("html_url"),
        "is_pull_request": "pull_request" in raw,
        "suggestion": suggestion,
    }


def _linked(raw: dict[str, Any]) -> dict[str, Any]:
    match = _REPO_FROM_URL.search(str(raw.get("repository_url") or ""))
    return {
        "number": raw.get("number"),
        "id": raw.get("id"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "repository": match.group(1) if match else None,
        "html_url": raw.get("html_url"),
    }


def _rows(payload: object) -> list[dict[str, Any]]:
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _repo(
    connector: GitHubRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> GitRepo:
    return GitRepo(connector, target, operator, params["owner"], params["repo"])


async def _list_issues(
    repo: GitRepo,
    *,
    state: str,
    labels: list[str],
    assignee: str | None,
    author: str | None,
    include_pull_requests: bool,
) -> dict[str, Any]:
    """Exact mode without a phrase: read the plain issue list to the end."""
    query: dict[str, Any] = {
        "state": state,
        "sort": "created",
        "direction": "desc",
        "per_page": _PAGE_SIZE,
    }
    if labels:
        query["labels"] = ",".join(labels)
    if assignee is not None:
        query["assignee"] = assignee
    if author is not None:
        query["creator"] = author
    hits: list[dict[str, Any]] = []
    complete = False
    for page in range(1, MAX_LIST_PAGES + 1):
        rows = _rows(await repo.get(_ISSUES_PATH, params={**query, "page": page}))
        hits.extend(
            _hit(row, suggestion=False)
            for row in rows
            if include_pull_requests or "pull_request" not in row
        )
        if len(rows) < _PAGE_SIZE:
            complete = True
            break
    return {
        "mode": "exact",
        "complete": complete,
        "total_count": None,
        "count": len(hits),
        "reason": None
        if complete
        else f"MEHO read {MAX_LIST_PAGES * _PAGE_SIZE} issues and stopped; add filters",
        "query": None,
        "issues": hits,
    }


async def _search(repo: GitRepo, q: str, *, mode: str, limit: int | None) -> dict[str, Any]:
    """Run ``GET /search/issues``: ranked reads one page, exact reads up to 1,000 hits."""
    ranked = mode == "ranked"
    page_size = limit if ranked and limit is not None else _PAGE_SIZE
    query: dict[str, Any] = {"q": q, "per_page": page_size}
    if not ranked:
        query.update(sort="created", order="desc")
    hits: list[dict[str, Any]] = []
    total_count = 0
    incomplete = False
    for page in range(1, SEARCH_RESULT_LIMIT // page_size + 1):
        payload = await repo.get(_SEARCH_PATH, params={**query, "page": page})
        rows = _rows(payload.get("items"))
        total_count = int(payload.get("total_count") or 0)
        incomplete = incomplete or bool(payload.get("incomplete_results"))
        hits.extend(_hit(row, suggestion=ranked) for row in rows)
        if ranked or len(rows) < page_size or len(hits) >= total_count:
            break
    complete = not incomplete and len(hits) >= total_count
    reason = None
    if incomplete:
        reason = "GitHub said the results are incomplete (the search timed out)"
    elif total_count > SEARCH_RESULT_LIMIT:
        reason = f"{total_count} hits; GitHub's search returns at most {SEARCH_RESULT_LIMIT}"
    elif not complete:
        reason = f"{total_count} hits; this answer holds the first {len(hits)}"
    return {
        "mode": mode,
        "complete": complete,
        "total_count": total_count,
        "count": len(hits),
        "reason": reason,
        "query": q,
        "issues": hits,
    }


async def issue_search_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Find issues in one repository (``gh.composite.issue_search``).

    Raises :class:`ValueError` on a bad name, label, login, phrase or query,
    or when ``phrase`` / ``query`` is used in the wrong mode.
    """
    mode = params.get("mode", "exact")
    phrase = params.get("phrase")
    text = params.get("query")
    if mode == "exact" and text is not None:
        raise ValueError("query is for mode 'ranked'; use phrase in mode 'exact'")
    if mode == "ranked" and phrase is not None:
        raise ValueError("phrase is for mode 'exact'; use query in mode 'ranked'")
    if mode == "ranked" and not text:
        raise ValueError("mode 'ranked' needs query")
    repo = _repo(connector, target, operator, params)
    state = params.get("state", "open")
    labels = _check_labels(params.get("labels"))
    assignee = params.get("assignee")
    author = params.get("author")
    if assignee is not None:
        _check_login(assignee, what="assignee")
    if author is not None:
        _check_login(author, what="author")
    include_prs = bool(params.get("include_pull_requests", False))
    if mode == "exact" and phrase is None:
        return await _list_issues(
            repo,
            state=state,
            labels=labels,
            assignee=assignee,
            author=author,
            include_pull_requests=include_prs,
        )
    q = build_search_query(
        owner=repo.owner,
        repo=repo.repo,
        state=state,
        labels=labels,
        assignee=assignee,
        author=author,
        include_pull_requests=include_prs,
        phrase=phrase,
        text=text,
    )
    return await _search(repo, q, mode=mode, limit=params.get("limit", 30))


async def _get_or_none(
    repo: GitRepo, template: str, number: str, params: dict[str, Any] | None = None
) -> Any:
    """GET ``template``; a 404 (no parent, or a pull request) answers ``None``."""
    try:
        return await repo.get(template, params=params, number=number)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


async def _parent(repo: GitRepo, number: str) -> dict[str, Any] | None:
    payload = await _get_or_none(repo, _PARENT_PATH, number)
    return _linked(payload) if isinstance(payload, dict) and payload else None


async def _sub_issues(repo: GitRepo, number: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page in range(1, _MAX_SUB_ISSUE_PAGES + 1):
        rows = _rows(
            await _get_or_none(
                repo, _SUB_ISSUES_PATH, number, {"per_page": _PAGE_SIZE, "page": page}
            )
        )
        found.extend(_linked(row) for row in rows)
        if len(rows) < _PAGE_SIZE:
            break
    return found


async def _newest_comments(repo: GitRepo, number: str, count: int, limit: int) -> list[Any]:
    """Return the newest ``limit`` comments, oldest of them first.

    GitHub lists comments oldest first, so MEHO reads the last page and,
    when that page is short of ``limit``, the page before it.
    """
    if limit <= 0 or count <= 0:
        return []
    last_page = math.ceil(count / _PAGE_SIZE)
    rows: list[dict[str, Any]] = []
    for page in (last_page, last_page - 1):
        if page < 1 or len(rows) >= limit:
            break
        page_rows = _rows(
            await repo.get(
                _COMMENTS_PATH, params={"per_page": _PAGE_SIZE, "page": page}, number=number
            )
        )
        rows = page_rows + rows
    return [
        {
            "id": row.get("id"),
            "author": _login(row.get("user")),
            "created_at": row.get("created_at"),
            "body": row.get("body"),
            "html_url": row.get("html_url"),
        }
        for row in rows[-limit:]
    ]


async def issue_view_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Read one issue with its parent, sub-issues and comments (``gh.composite.issue_view``).

    A missing issue raises the 404 :class:`httpx.HTTPStatusError` for the
    dispatcher to map. An issue without a parent answers ``parent: null``.
    """
    repo = _repo(connector, target, operator, params)
    number = str(int(params["issue_number"]))
    issue = await repo.get(_ISSUE_PATH, number=number)
    comment_count = int(issue.get("comments") or 0)
    return {
        "number": issue.get("number"),
        "id": issue.get("id"),
        "node_id": issue.get("node_id"),
        "title": issue.get("title"),
        "body": issue.get("body"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "labels": _names(issue.get("labels"), "name"),
        "assignees": _names(issue.get("assignees"), "login"),
        "author": _login(issue.get("user")),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "html_url": issue.get("html_url"),
        "is_pull_request": "pull_request" in issue,
        "parent": await _parent(repo, number),
        "sub_issues": await _sub_issues(repo, number),
        "comment_count": comment_count,
        "comments": await _newest_comments(
            repo, number, comment_count, int(params.get("comments_limit", 0))
        ),
    }


async def issue_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """File one issue (``gh.composite.issue_create``) with ``POST .../issues``."""
    repo = _repo(connector, target, operator, params)
    body: dict[str, Any] = {"title": params["title"]}
    if params.get("body") is not None:
        body["body"] = params["body"]
    labels = _check_labels(params.get("labels"))
    if labels:
        body["labels"] = labels
    assignees = [_check_login(a, what="assignees") for a in params.get("assignees") or []]
    if assignees:
        body["assignees"] = assignees
    issue = await repo.send("POST", _ISSUES_PATH, json=body)
    return {
        "status": "created",
        "number": issue.get("number"),
        "id": issue.get("id"),
        "node_id": issue.get("node_id"),
        "html_url": issue.get("html_url"),
        "title": issue.get("title"),
        "labels": _names(issue.get("labels"), "name"),
        "assignees": _names(issue.get("assignees"), "login"),
    }


async def issue_comment_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Add one comment to one issue (``gh.composite.issue_comment``)."""
    repo = _repo(connector, target, operator, params)
    number = int(params["issue_number"])
    comment = await repo.send(
        "POST", _COMMENTS_PATH, json={"body": params["body"]}, number=str(number)
    )
    return {
        "status": "commented",
        "issue_number": number,
        "comment_id": comment.get("id"),
        "html_url": comment.get("html_url"),
        "created_at": comment.get("created_at"),
    }
