# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Projects-v2 board composites (direct-session, #2081).

The three board composites (``project_view`` read, ``project_item_add``
/ ``project_item_set_field`` writes) issue GraphQL calls through the
resolved ``GitHubRestConnector`` session via
:func:`._graphql.github_graphql`. These tests drive each handler with a
recording session stub whose ``_post_json`` returns canned GraphQL
envelopes -- no live GitHub, no ``endpoint_descriptor`` table.

Coverage:

* ``project_view`` -- org + user roots select the right GraphQL selector
  and forward login / number / first as variables; the envelope carries
  project_id + parsed fields (single-select option ids) + parsed items +
  item_count; unknown login / project number raise GitHubGraphQlError.
* ``project_item_add`` / ``project_item_set_field`` -- the mutation is
  posted with the right variables; the returned node id is surfaced;
  a missing node id raises.
* GraphQL error handling -- a ``200``-with-``errors`` envelope raises
  GitHubGraphQlError (GitHub does not use a 4xx status for query errors).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._board import (
    project_item_add_composite,
    project_item_set_field_composite,
    project_view_composite,
)
from meho_backplane.connectors.github.composites._graphql import (
    GitHubGraphQlError,
    github_graphql,
)


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-gh-board",
        name="GH Board Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingConnector:
    """Session stub matching the subset of ``GitHubRestConnector`` the board handlers use.

    ``_post_json`` pops the next canned response off a queue (an
    ``Exception`` instance is raised instead) and records the ``(path,
    verb, json)`` of every call so tests can assert the GraphQL document
    + variables that went on the wire. ``mount_op_path`` is identity,
    mirroring the github connector's inherited default.
    """

    def __init__(self, post_responses: list[Any]) -> None:
        self._post_responses = list(post_responses)
        self.post_calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.post_calls.append({"path": path, "verb": verb, "json": json})
        response = self._post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _project_view_payload() -> dict[str, Any]:
    """A canned ``projectV2`` GraphQL envelope with one single-select field + two items."""
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_board1",
                    "title": "MEHO Board",
                    "fields": {
                        "nodes": [
                            {
                                "__typename": "ProjectV2Field",
                                "id": "F_title",
                                "name": "Title",
                                "dataType": "TITLE",
                            },
                            {
                                "__typename": "ProjectV2SingleSelectField",
                                "id": "F_status",
                                "name": "Status",
                                "dataType": "SINGLE_SELECT",
                                "options": [
                                    {"id": "opt_todo", "name": "Todo"},
                                    {"id": "opt_done", "name": "Done"},
                                ],
                            },
                        ]
                    },
                    "items": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "content": {
                                    "__typename": "Issue",
                                    "number": 42,
                                    "title": "Fix X",
                                    "url": "https://github.com/o/r/issues/42",
                                },
                            },
                            {
                                "id": "PVTI_2",
                                "content": {"__typename": "DraftIssue", "title": "Draft note"},
                            },
                        ],
                    },
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_project_view_org_root_and_variables() -> None:
    """``project_view`` uses the ``organization`` root and forwards login/number/first."""
    connector = _RecordingConnector([_project_view_payload()])
    envelope = await project_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "project_number": 19, "first": 20},
        connector=connector,  # type: ignore[arg-type]
    )
    # ----- GraphQL document + variables on the wire -----
    assert len(connector.post_calls) == 1
    call = connector.post_calls[0]
    assert call["path"] == "/graphql"
    assert "organization(login: $login)" in call["json"]["query"]
    assert "user(login: $login)" not in call["json"]["query"]
    assert call["json"]["variables"] == {"login": "evoila", "number": 19, "first": 20}
    # ----- Parsed envelope -----
    assert envelope["project_id"] == "PVT_board1"
    assert envelope["title"] == "MEHO Board"
    assert envelope["item_count"] == 2


@pytest.mark.asyncio
async def test_project_view_parses_single_select_options() -> None:
    """The Status single-select field's option ids surface for project_item_set_field."""
    connector = _RecordingConnector([_project_view_payload()])
    envelope = await project_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "project_number": 19},
        connector=connector,  # type: ignore[arg-type]
    )
    status_field = next(f for f in envelope["fields"] if f["name"] == "Status")
    assert status_field["id"] == "F_status"
    assert status_field["data_type"] == "SINGLE_SELECT"
    assert status_field["options"] == [
        {"id": "opt_todo", "name": "Todo"},
        {"id": "opt_done", "name": "Done"},
    ]
    # A non-single-select field carries options=None.
    title_field = next(f for f in envelope["fields"] if f["name"] == "Title")
    assert title_field["options"] is None


@pytest.mark.asyncio
async def test_project_view_parses_items_including_draft() -> None:
    """Items surface item_id + content type/number/title/url; drafts have null number/url."""
    connector = _RecordingConnector([_project_view_payload()])
    envelope = await project_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "project_number": 19},
        connector=connector,  # type: ignore[arg-type]
    )
    issue_item = next(i for i in envelope["items"] if i["content_type"] == "Issue")
    assert issue_item["item_id"] == "PVTI_1"
    assert issue_item["number"] == 42
    assert issue_item["url"] == "https://github.com/o/r/issues/42"
    draft_item = next(i for i in envelope["items"] if i["content_type"] == "DraftIssue")
    assert draft_item["number"] is None
    assert draft_item["url"] is None


@pytest.mark.asyncio
async def test_project_view_user_root() -> None:
    """``owner_type='user'`` switches the GraphQL root selector to ``user``."""
    payload = {
        "data": {"user": {"projectV2": {"id": "PVT_u", "title": "T", "fields": {}, "items": {}}}}
    }
    connector = _RecordingConnector([payload])
    envelope = await project_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "octocat", "project_number": 3, "owner_type": "user"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert "user(login: $login)" in connector.post_calls[0]["json"]["query"]
    assert envelope["project_id"] == "PVT_u"
    assert envelope["fields"] == []
    assert envelope["items"] == []
    assert envelope["item_count"] is None


@pytest.mark.asyncio
async def test_project_view_invalid_owner_type_raises() -> None:
    """An owner_type outside the closed enum raises before any wire call."""
    connector = _RecordingConnector([])
    with pytest.raises(GitHubGraphQlError, match="owner_type"):
        await project_view_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "x", "project_number": 1, "owner_type": "team"},
            connector=connector,  # type: ignore[arg-type]
        )
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_project_view_unknown_project_raises() -> None:
    """A ``null`` projectV2 (bad number / no project scope) raises GitHubGraphQlError."""
    connector = _RecordingConnector([{"data": {"organization": {"projectV2": None}}}])
    with pytest.raises(GitHubGraphQlError, match="no projectV2"):
        await project_view_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "project_number": 999},
            connector=connector,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_project_view_unknown_owner_raises() -> None:
    """A ``null`` owner (login typo / no access) raises GitHubGraphQlError."""
    connector = _RecordingConnector([{"data": {"organization": None}}])
    with pytest.raises(GitHubGraphQlError, match="no 'organization'"):
        await project_view_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "nope", "project_number": 1},
            connector=connector,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_project_item_add_posts_mutation_and_returns_item_id() -> None:
    """``project_item_add`` posts addProjectV2ItemById with the right variables."""
    connector = _RecordingConnector(
        [{"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_new"}}}}]
    )
    result = await project_item_add_composite(
        operator=_make_operator(),
        target=object(),
        params={"project_id": "PVT_board1", "content_id": "I_kwissue"},
        connector=connector,  # type: ignore[arg-type]
    )
    call = connector.post_calls[0]
    assert "addProjectV2ItemById" in call["json"]["query"]
    assert call["json"]["variables"] == {"projectId": "PVT_board1", "contentId": "I_kwissue"}
    assert result == {
        "item_id": "PVTI_new",
        "project_id": "PVT_board1",
        "content_id": "I_kwissue",
        "status": "added",
    }


@pytest.mark.asyncio
async def test_project_item_add_missing_item_id_raises() -> None:
    """A mutation payload without an item id raises rather than returning None."""
    connector = _RecordingConnector([{"data": {"addProjectV2ItemById": {"item": {}}}}])
    with pytest.raises(GitHubGraphQlError, match="returned no item"):
        await project_item_add_composite(
            operator=_make_operator(),
            target=object(),
            params={"project_id": "P", "content_id": "C"},
            connector=connector,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_project_item_set_field_posts_single_select_value() -> None:
    """``project_item_set_field`` posts updateProjectV2ItemFieldValue with the option id."""
    connector = _RecordingConnector(
        [{"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_1"}}}}]
    )
    result = await project_item_set_field_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "project_id": "PVT_board1",
            "item_id": "PVTI_1",
            "field_id": "F_status",
            "option_id": "opt_done",
        },
        connector=connector,  # type: ignore[arg-type]
    )
    call = connector.post_calls[0]
    assert "updateProjectV2ItemFieldValue" in call["json"]["query"]
    assert "singleSelectOptionId" in call["json"]["query"]
    assert call["json"]["variables"] == {
        "projectId": "PVT_board1",
        "itemId": "PVTI_1",
        "fieldId": "F_status",
        "optionId": "opt_done",
    }
    assert result == {
        "item_id": "PVTI_1",
        "field_id": "F_status",
        "option_id": "opt_done",
        "status": "updated",
    }


@pytest.mark.asyncio
async def test_graphql_errors_array_raises() -> None:
    """A 200-with-errors GraphQL envelope raises GitHubGraphQlError with the message."""
    connector = _RecordingConnector(
        [
            {
                "data": None,
                "errors": [{"message": "Could not resolve to a node with the global id of 'X'."}],
            }
        ]
    )
    with pytest.raises(GitHubGraphQlError, match="Could not resolve to a node"):
        await github_graphql(
            connector,  # type: ignore[arg-type]
            object(),
            _make_operator(),
            query="query { x }",
            variables={},
        )


@pytest.mark.asyncio
async def test_graphql_missing_data_raises() -> None:
    """A response with neither data nor errors raises GitHubGraphQlError."""
    connector = _RecordingConnector([{"foo": "bar"}])
    with pytest.raises(GitHubGraphQlError, match="no ``data`` object"):
        await github_graphql(
            connector,  # type: ignore[arg-type]
            object(),
            _make_operator(),
            query="query { x }",
            variables={},
        )
