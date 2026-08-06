# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end 'file a board-complete ticket' flow (#2081, AC #4).

Proves an agent can file a board-complete ticket through MEHO's own
dispatch surface -- **without a ``gh`` CLI fallback** -- by composing the
four #2081 composites in the documented order:

    create issue -> add to Projects-v2 board -> set Status field
                 -> link a sub-issue

Every step runs against **one shared connector session stub** (the
#2255 direct-session substrate): the create is the connector's live REST
POST, the board reads/writes are the GraphQL composites, and the
sub-issue link is the REST composite. Nothing shells out to ``gh`` -- the
whole flow is connector-session calls, which is the point of the Task.

Deterministic (mocked upstream) so it runs in the default unit lane
(``tests/integration`` is ignored there); the live-GitHub smoke variant
of the pr-status composite lives under ``tests/integration``.

The load-bearing assertions are the *threading* ones: the board node id
from ``project_view`` flows into the add + set-field mutations, and the
sub-issue link uses the child's **database id** (from the create
response) rather than its issue number -- the sub_issue_id gotcha.
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
from meho_backplane.connectors.github.composites._sub_issues import sub_issue_add_composite

_OWNER = "evoila"
_REPO = "meho"
_PROJECT_NUMBER = 19


def _make_operator() -> Operator:
    """Human operator -- auto-executes ``caution`` board writes (board-complete filing)."""
    return Operator(
        sub="op-gh-board-flow",
        name="GH Board Flow Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000d0d0"),
        tenant_role=TenantRole.OPERATOR,
    )


_PROJECT_VIEW_RESPONSE: dict[str, Any] = {
    "data": {
        "organization": {
            "projectV2": {
                "id": "PVT_board",
                "title": "MEHO Board",
                "fields": {
                    "nodes": [
                        {
                            "__typename": "ProjectV2SingleSelectField",
                            "id": "F_status",
                            "name": "Status",
                            "dataType": "SINGLE_SELECT",
                            "options": [
                                {"id": "opt_todo", "name": "Todo"},
                                {"id": "opt_in_progress", "name": "In Progress"},
                            ],
                        }
                    ]
                },
                "items": {"totalCount": 0, "nodes": []},
            }
        }
    }
}


class _BoardFlowConnector:
    """One connector session stub routing every step of the board-complete flow.

    Routes ``_post_json`` by wire path (and, for ``/graphql``, by the
    mutation named in the document). Records every call in order so the
    test can assert the four steps fired against the connector session --
    never a ``gh`` subprocess.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # Sequential create-issue responses: the main ticket, then the child.
        self._create_queue: list[dict[str, Any]] = [
            {"id": 500, "node_id": "I_kwMAIN", "number": 100},
            {"id": 555, "node_id": "I_kwCHILD", "number": 200},
        ]

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
        self.calls.append({"path": path, "verb": verb, "json": json})
        return self._route(path, json or {})

    def _route(self, path: str, body: dict[str, Any]) -> Any:
        if path == "/graphql":
            return self._route_graphql(body.get("query", ""))
        if path.endswith("/sub_issues"):
            return {"number": 100, "sub_issues_summary": {"total": 1, "completed": 0}}
        if path.endswith("/issues"):
            return self._create_queue.pop(0)
        raise AssertionError(f"unexpected REST path in board flow: {path!r}")

    @staticmethod
    def _route_graphql(query: str) -> Any:
        if "projectV2(number:" in query:
            return _PROJECT_VIEW_RESPONSE
        if "addProjectV2ItemById" in query:
            return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_main"}}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {
                "data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_main"}}}
            }
        raise AssertionError(f"unexpected graphql document in board flow: {query!r}")


@pytest.mark.asyncio
async def test_file_board_complete_ticket_without_gh_cli() -> None:
    """create -> add-to-board -> set-Status -> link-sub-issue, all via the connector session."""
    operator = _make_operator()
    target = object()
    connector = _BoardFlowConnector()

    # ----- Step 1: create the main ticket + a child issue (live REST op) -----
    main = await connector._post_json(
        target, f"/repos/{_OWNER}/{_REPO}/issues", operator=operator, json={"title": "Main ticket"}
    )
    child = await connector._post_json(
        target, f"/repos/{_OWNER}/{_REPO}/issues", operator=operator, json={"title": "Child task"}
    )
    assert main["node_id"] == "I_kwMAIN"
    assert child["id"] == 555  # the DATABASE id -- distinct from its number (200)

    # ----- Step 2: read the board (project id + Status field + its options) -----
    board = await project_view_composite(
        operator=operator,
        target=target,
        params={"owner": _OWNER, "project_number": _PROJECT_NUMBER},
        connector=connector,  # type: ignore[arg-type]
    )
    project_id = board["project_id"]
    status_field = next(f for f in board["fields"] if f["name"] == "Status")
    todo_option = next(o for o in status_field["options"] if o["name"] == "Todo")
    assert project_id == "PVT_board"

    # ----- Step 3: add the main ticket to the board (by content node id) -----
    added = await project_item_add_composite(
        operator=operator,
        target=target,
        params={"project_id": project_id, "content_id": main["node_id"]},
        connector=connector,  # type: ignore[arg-type]
    )
    assert added["status"] == "added"
    item_id = added["item_id"]

    # ----- Step 4: set the Status field on the new board item -----
    field_set = await project_item_set_field_composite(
        operator=operator,
        target=target,
        params={
            "project_id": project_id,
            "item_id": item_id,
            "field_id": status_field["id"],
            "option_id": todo_option["id"],
        },
        connector=connector,  # type: ignore[arg-type]
    )
    assert field_set["status"] == "updated"

    # ----- Step 5: link the child as a sub-issue of the main ticket -----
    linked = await sub_issue_add_composite(
        operator=operator,
        target=target,
        params={
            "owner": _OWNER,
            "repo": _REPO,
            "issue_number": main["number"],
            "sub_issue_id": child["id"],
        },
        connector=connector,  # type: ignore[arg-type]
    )
    assert linked["status"] == "linked"

    # ----- The flow used only the connector session, in the documented order -----
    wire = [(c["path"], c["verb"]) for c in connector.calls]
    assert wire == [
        (f"/repos/{_OWNER}/{_REPO}/issues", "POST"),
        (f"/repos/{_OWNER}/{_REPO}/issues", "POST"),
        ("/graphql", "POST"),
        ("/graphql", "POST"),
        ("/graphql", "POST"),
        (f"/repos/{_OWNER}/{_REPO}/issues/100/sub_issues", "POST"),
    ]

    # ----- Node-id / database-id threading is correct across the steps -----
    add_call = connector.calls[3]
    assert add_call["json"]["variables"] == {"projectId": "PVT_board", "contentId": "I_kwMAIN"}
    set_field_call = connector.calls[4]
    assert set_field_call["json"]["variables"]["fieldId"] == "F_status"
    assert set_field_call["json"]["variables"]["optionId"] == "opt_todo"
    sub_issue_call = connector.calls[5]
    # The link uses the child's DATABASE id (555), never its issue number (200).
    assert sub_issue_call["json"] == {"sub_issue_id": 555}
