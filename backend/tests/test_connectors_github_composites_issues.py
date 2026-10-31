# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the gh-rest issue composites (search, view, create, comment).

The handlers run against a recording session stub with saved responses; no
live GitHub. All names are made up. Coverage:

* ``issue_search`` exact mode without a phrase reads the plain issue list
  page by page to the end, leaves pull requests out unless asked, and says
  ``complete=true``.
* ``issue_search`` with a phrase, and in ranked mode, uses GitHub's search;
  every query starts with ``repo:<owner>/<repo>``; GitHub's 1,000-hit limit
  and ``incomplete_results`` make ``complete=false``; ranked hits are
  suggestions.
* Free text that would widen the search, bad logins and labels, and a
  phrase or query in the wrong mode are refused before any call.
* ``issue_view`` reads the issue, its parent (404 means none), its
  sub-issues and the newest comments.
* ``issue_create`` returns the number, node id and database id, and the
  database id works as ``sub_issue_id`` for ``sub_issue_add``.
* ``issue_comment`` posts the body and returns the comment's id and link.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._issues import (
    SEARCH_RESULT_LIMIT,
    issue_comment_composite,
    issue_create_composite,
    issue_search_composite,
    issue_view_composite,
)
from meho_backplane.connectors.github.composites._sub_issues import sub_issue_add_composite

_OWNER = "acme"
_REPO = "widgets"
_ISSUES_PATH = "/repos/acme/widgets/issues"
_SEARCH_PATH = "/search/issues"


def _make_operator() -> Operator:
    return Operator(
        sub="op-gh-issues",
        name="GH Issues Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c1c1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status_code, request=request, json={"message": "Not Found"})
    return httpx.HTTPStatusError(f"http_{status_code}", request=request, response=response)


def _issue(number: int, *, pull: bool = False, **extra: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": 9000 + number,
        "node_id": f"I_node{number}",
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "dev-one"}],
        "user": {"login": "reporter"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "comments": 0,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "repository_url": "https://api.github.com/repos/acme/widgets",
    }
    if pull:
        raw["pull_request"] = {"url": "https://api.github.com/repos/acme/widgets/pulls/1"}
    raw.update(extra)
    return raw


class _RecordingConnector:
    """Session stub for the subset of ``GitHubRestConnector`` the handlers use.

    ``get`` maps a path to a payload, an exception, or a function of the
    query params (for paged lists). ``post`` maps a path to a payload.
    """

    def __init__(
        self,
        get: dict[str, Any] | None = None,
        post: dict[str, Any] | None = None,
    ) -> None:
        self._get = dict(get or {})
        self._post = dict(post or {})
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.get_calls.append((path, params))
        if path not in self._get:
            raise AssertionError(f"unexpected GET {path}")
        value = self._get[path]
        if callable(value):
            value = value(params or {})
        if isinstance(value, Exception):
            raise value
        return value

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        self.post_calls.append((verb, path, json))
        if path not in self._post:
            raise AssertionError(f"unexpected POST {path}")
        return self._post[path]


async def _search(connector: _RecordingConnector, **params: Any) -> dict[str, Any]:
    return await issue_search_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": _OWNER, "repo": _REPO, **params},
        connector=connector,  # type: ignore[arg-type]
    )


def _paged_list(total: int, *, pull_every: int = 0):  # type: ignore[no-untyped-def]
    """Return a page function for the plain issue list with ``total`` rows."""

    def page(params: dict[str, Any]) -> list[dict[str, Any]]:
        start = (params["page"] - 1) * params["per_page"]
        numbers = range(total - start, max(total - start - params["per_page"], 0), -1)
        return [_issue(n, pull=bool(pull_every) and n % pull_every == 0) for n in numbers if n > 0]

    return page


def _search_pages(total: int, *, incomplete: bool = False):  # type: ignore[no-untyped-def]
    """Return a page function for ``/search/issues`` reporting ``total`` hits."""

    def page(params: dict[str, Any]) -> dict[str, Any]:
        available = min(total, SEARCH_RESULT_LIMIT)
        start = (params["page"] - 1) * params["per_page"]
        count = max(0, min(params["per_page"], available - start))
        return {
            "total_count": total,
            "incomplete_results": incomplete,
            "items": [_issue(start + i + 1) for i in range(count)],
        }

    return page


# ---------------------------------------------------------------------------
# issue_search -- exact mode, plain list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_search_reads_every_page_of_the_list() -> None:
    connector = _RecordingConnector(get={_ISSUES_PATH: _paged_list(250)})
    result = await _search(connector)
    assert result["mode"] == "exact"
    assert result["complete"] is True
    assert result["total_count"] is None
    assert result["query"] is None
    assert result["reason"] is None
    assert result["count"] == 250
    assert [p["page"] for _, p in connector.get_calls if p] == [1, 2, 3]
    numbers = [hit["number"] for hit in result["issues"]]
    assert numbers == sorted(numbers, reverse=True), "newest first"
    assert result["issues"][0]["suggestion"] is False


@pytest.mark.asyncio
async def test_exact_search_passes_the_filters_to_the_list() -> None:
    connector = _RecordingConnector(get={_ISSUES_PATH: _paged_list(3)})
    await _search(
        connector,
        state="closed",
        labels=["bug", "needs triage"],
        assignee="dev-one",
        author="reporter",
    )
    _, params = connector.get_calls[0]
    assert params == {
        "state": "closed",
        "sort": "created",
        "direction": "desc",
        "per_page": 100,
        "labels": "bug,needs triage",
        "assignee": "dev-one",
        "creator": "reporter",
        "page": 1,
    }


@pytest.mark.asyncio
async def test_exact_search_leaves_pull_requests_out_unless_asked() -> None:
    connector = _RecordingConnector(get={_ISSUES_PATH: _paged_list(10, pull_every=2)})
    result = await _search(connector)
    assert result["count"] == 5
    assert not any(hit["is_pull_request"] for hit in result["issues"])

    result = await _search(connector, include_pull_requests=True)
    assert result["count"] == 10
    assert sum(hit["is_pull_request"] for hit in result["issues"]) == 5


@pytest.mark.asyncio
async def test_exact_search_reads_past_100_pages_and_past_pull_requests() -> None:
    # 10,005 rows, every second one a pull request: 101 pages, no cap.
    connector = _RecordingConnector(get={_ISSUES_PATH: _paged_list(10_005, pull_every=2)})
    result = await _search(connector)
    assert result["complete"] is True
    assert result["reason"] is None
    assert result["count"] == 5_003
    assert len(connector.get_calls) == 101
    assert [p["page"] for _, p in connector.get_calls if p] == list(range(1, 102))
    assert result["issues"][-1]["number"] == 1, "the oldest issue is in"

    connector = _RecordingConnector(get={_ISSUES_PATH: _paged_list(10_005, pull_every=2)})
    result = await _search(connector, include_pull_requests=True)
    assert result["complete"] is True
    assert result["count"] == 10_005
    assert len(connector.get_calls) == 101


# ---------------------------------------------------------------------------
# issue_search -- GitHub's search (phrase or ranked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_search_with_a_phrase_uses_search_with_the_phrase_in_quotes() -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(3)})
    result = await _search(connector, phrase="backup fails", labels=["bug"], author="reporter")
    _, params = connector.get_calls[0]
    assert params is not None
    assert params["q"] == (
        'repo:acme/widgets is:issue is:open label:"bug" author:reporter "backup fails"'
    )
    assert params["sort"] == "created" and params["order"] == "desc"
    assert result["mode"] == "exact"
    assert result["complete"] is True
    assert result["total_count"] == 3
    assert result["query"] == params["q"]
    assert result["issues"][0]["suggestion"] is False


@pytest.mark.asyncio
async def test_exact_phrase_search_reads_pages_until_all_hits_are_in() -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(230)})
    result = await _search(connector, phrase="disk full")
    assert result["complete"] is True
    assert result["count"] == 230
    assert [p["page"] for _, p in connector.get_calls if p] == [1, 2, 3]


@pytest.mark.asyncio
async def test_search_over_github_limit_of_1000_hits_is_not_complete() -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(2500)})
    result = await _search(connector, phrase="timeout")
    assert result["complete"] is False
    assert result["total_count"] == 2500
    assert result["count"] == SEARCH_RESULT_LIMIT
    assert "1000" in result["reason"]
    assert len(connector.get_calls) == SEARCH_RESULT_LIMIT // 100


@pytest.mark.asyncio
async def test_search_marks_incomplete_results_from_github() -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(4, incomplete=True)})
    result = await _search(connector, phrase="timeout")
    assert result["complete"] is False
    assert "incomplete" in result["reason"]


@pytest.mark.asyncio
async def test_ranked_search_returns_suggestions_in_github_order() -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(80)})
    result = await _search(connector, mode="ranked", query="login fails after upgrade", limit=20)
    _, params = connector.get_calls[0]
    assert params is not None
    assert params["q"] == "repo:acme/widgets is:issue is:open login fails after upgrade"
    assert params["per_page"] == 20
    assert "sort" not in params and "order" not in params
    assert len(connector.get_calls) == 1
    assert result["mode"] == "ranked"
    assert result["count"] == 20
    assert result["complete"] is False
    assert all(hit["suggestion"] for hit in result["issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"phrase": "x"},
        {"phrase": "x", "state": "all", "include_pull_requests": True},
        {"mode": "ranked", "query": "y"},
        {"mode": "ranked", "query": "y", "labels": ["a b"], "assignee": "dev-one"},
    ],
)
async def test_every_search_query_starts_with_the_repository(params: dict[str, Any]) -> None:
    connector = _RecordingConnector(get={_SEARCH_PATH: _search_pages(1)})
    await _search(connector, **params)
    for _, sent in connector.get_calls:
        assert sent is not None
        assert sent["q"].startswith("repo:acme/widgets ")
        assert sent["q"].count("repo:") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"mode": "ranked", "query": "bug repo:other/thing"}, "repository"),
        ({"mode": "ranked", "query": "bug org:other"}, "repository"),
        ({"mode": "ranked", "query": "bug OR user:someone"}, "repository"),
        ({"mode": "ranked", "query": "bug OR crash"}, "OR"),
        ({"mode": "ranked", "query": "(bug)"}, "OR"),
        ({"mode": "ranked"}, "needs query"),
        ({"mode": "ranked", "query": "x", "phrase": "y"}, "phrase is for"),
        ({"query": "x"}, "query is for"),
        ({"phrase": 'say "hi"'}, "double quote"),
        ({"phrase": "a\nb"}, "control"),
        ({"assignee": "not a login"}, "login"),
        ({"author": "x:y"}, "login"),
        ({"labels": ['bad"label']}, "double quote"),
        ({"labels": ["a,b"]}, "comma"),
        ({"owner": "../x"}, "owner"),
    ],
)
async def test_search_refuses_bad_input_before_any_call(
    params: dict[str, Any], message: str
) -> None:
    connector = _RecordingConnector()
    with pytest.raises(ValueError, match=message):
        await _search(connector, **params)
    assert connector.get_calls == []


# ---------------------------------------------------------------------------
# issue_view
# ---------------------------------------------------------------------------


def _comments_page(total: int):  # type: ignore[no-untyped-def]
    def page(params: dict[str, Any]) -> list[dict[str, Any]]:
        start = (params["page"] - 1) * params["per_page"]
        return [
            {
                "id": n,
                "user": {"login": "dev-one"},
                "created_at": "2026-01-03T00:00:00Z",
                "body": f"comment {n}",
                "html_url": f"https://github.com/acme/widgets/issues/7#issuecomment-{n}",
            }
            for n in range(start + 1, min(start + params["per_page"], total) + 1)
        ]

    return page


@pytest.mark.asyncio
async def test_issue_view_returns_parent_sub_issues_and_newest_comments() -> None:
    parent = _issue(3, repository_url="https://api.github.com/repos/acme/planning")
    connector = _RecordingConnector(
        get={
            f"{_ISSUES_PATH}/7": _issue(7, body="Steps.", comments=105),
            f"{_ISSUES_PATH}/7/parent": parent,
            f"{_ISSUES_PATH}/7/sub_issues": lambda p: (
                [_issue(8), _issue(9)] if p["page"] == 1 else []
            ),
            f"{_ISSUES_PATH}/7/comments": _comments_page(105),
        }
    )
    result = await issue_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": _OWNER, "repo": _REPO, "issue_number": 7, "comments_limit": 10},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["number"] == 7
    assert result["id"] == 9007
    assert result["node_id"] == "I_node7"
    assert result["body"] == "Steps."
    assert result["labels"] == ["bug"]
    assert result["assignees"] == ["dev-one"]
    assert result["author"] == "reporter"
    assert result["parent"] == {
        "number": 3,
        "id": 9003,
        "title": "Issue 3",
        "state": "open",
        "repository": "acme/planning",
        "html_url": "https://github.com/acme/widgets/issues/3",
    }
    assert [s["number"] for s in result["sub_issues"]] == [8, 9]
    assert result["comment_count"] == 105
    # The newest ten, oldest of them first: comments 96..105 over pages 1 and 2.
    assert [c["id"] for c in result["comments"]] == list(range(96, 106))
    comment_pages = [
        p["page"] for path, p in connector.get_calls if path.endswith("comments") and p
    ]
    assert comment_pages == [2, 1]


@pytest.mark.asyncio
async def test_issue_view_without_parent_and_without_comments() -> None:
    connector = _RecordingConnector(
        get={
            f"{_ISSUES_PATH}/7": _issue(7, comments=4),
            f"{_ISSUES_PATH}/7/parent": _http_error(404),
            f"{_ISSUES_PATH}/7/sub_issues": [],
        }
    )
    result = await issue_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": _OWNER, "repo": _REPO, "issue_number": 7},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["parent"] is None
    assert result["sub_issues"] == []
    assert result["comment_count"] == 4
    assert result["comments"] == []
    assert not any(path.endswith("/comments") for path, _ in connector.get_calls)


@pytest.mark.asyncio
async def test_issue_view_of_a_missing_issue_raises_the_404() -> None:
    connector = _RecordingConnector(get={f"{_ISSUES_PATH}/7": _http_error(404)})
    with pytest.raises(httpx.HTTPStatusError):
        await issue_view_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": _OWNER, "repo": _REPO, "issue_number": 7},
            connector=connector,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# issue_create and issue_comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_create_returns_the_ids_and_sub_issue_add_takes_the_database_id() -> None:
    created = _issue(200, id=555, node_id="I_child", labels=[{"name": "bug"}], assignees=[])
    connector = _RecordingConnector(
        post={
            _ISSUES_PATH: created,
            f"{_ISSUES_PATH}/100/sub_issues": {"number": 100},
        }
    )
    result = await issue_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "owner": _OWNER,
            "repo": _REPO,
            "title": "Child task",
            "body": "Details.",
            "labels": ["bug"],
            "assignees": ["dev-one"],
        },
        connector=connector,  # type: ignore[arg-type]
    )
    assert result == {
        "status": "created",
        "number": 200,
        "id": 555,
        "node_id": "I_child",
        "html_url": "https://github.com/acme/widgets/issues/200",
        "title": "Issue 200",
        "labels": ["bug"],
        "assignees": [],
    }
    assert connector.post_calls[0] == (
        "POST",
        _ISSUES_PATH,
        {"title": "Child task", "body": "Details.", "labels": ["bug"], "assignees": ["dev-one"]},
    )

    linked = await sub_issue_add_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "owner": _OWNER,
            "repo": _REPO,
            "issue_number": 100,
            "sub_issue_id": result["id"],
        },
        connector=connector,  # type: ignore[arg-type]
    )
    assert linked["status"] == "linked"
    assert connector.post_calls[1] == (
        "POST",
        f"{_ISSUES_PATH}/100/sub_issues",
        {"sub_issue_id": 555},
    )


@pytest.mark.asyncio
async def test_issue_create_sends_only_the_title_when_nothing_else_is_given() -> None:
    connector = _RecordingConnector(post={_ISSUES_PATH: _issue(1)})
    await issue_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": _OWNER, "repo": _REPO, "title": "Only a title"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.post_calls == [("POST", _ISSUES_PATH, {"title": "Only a title"})]


@pytest.mark.asyncio
async def test_issue_create_refuses_a_bad_assignee_before_any_call() -> None:
    connector = _RecordingConnector()
    with pytest.raises(ValueError, match="login"):
        await issue_create_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": _OWNER, "repo": _REPO, "title": "t", "assignees": ["@someone"]},
            connector=connector,  # type: ignore[arg-type]
        )
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_issue_comment_posts_the_body() -> None:
    connector = _RecordingConnector(
        post={
            f"{_ISSUES_PATH}/7/comments": {
                "id": 31,
                "html_url": "https://github.com/acme/widgets/issues/7#issuecomment-31",
                "created_at": "2026-01-04T00:00:00Z",
            }
        }
    )
    result = await issue_comment_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": _OWNER, "repo": _REPO, "issue_number": 7, "body": "Done."},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result == {
        "status": "commented",
        "issue_number": 7,
        "comment_id": 31,
        "html_url": "https://github.com/acme/widgets/issues/7#issuecomment-31",
        "created_at": "2026-01-04T00:00:00Z",
    }
    assert connector.post_calls == [("POST", f"{_ISSUES_PATH}/7/comments", {"body": "Done."})]
