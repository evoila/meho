# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated pagination hints for ingested GitHub REST operations (#3676).

GitHub list endpoints return one page of a JSON array. The next page is
named only in the ``Link`` response header, and the request side uses
``page`` / ``per_page`` query params. Ingested rows carry no
``llm_instructions``, so without a hint a reduced GitHub list tells the
agent nothing about how to fetch the next page.

An operator applies a hint to a deployment's ingested descriptor through
the operator-review ``edit_op`` path, as
``llm_instructions["pagination_hint"]``. The dispatcher then passes it to
result reduction, which copies it into ``fetch_more.native_pagination``.

These hints only say how to ask for more rows. They do not say how many
rows GitHub holds, so a reduced handle stays "source coverage unknown".

Source: https://docs.github.com/en/rest/issues/issues#list-repository-issues
and https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api
(API version 2022-11-28).
"""

from __future__ import annotations

from typing import Any

#: The ingested op id of "List repository issues" (``<METHOD>:<path>``).
GH_ISSUE_LIST_OP_ID = "GET:/repos/{owner}/{repo}/issues"

#: Pagination hint for ``GET /repos/{owner}/{repo}/issues``. The shape
#: matches :class:`~meho_backplane.connectors.schemas.PaginationHint`.
GH_ISSUE_LIST_PAGINATION_HINT: dict[str, Any] = {
    "params": {
        "per_page": "Rows per page. Default 30, maximum 100.",
        "page": (
            "Page number, starting at 1 (the default). Add 1 to get the next "
            "page. A page with fewer than per_page rows is the last page. One "
            "call returns one page only, so a reduced result may not hold "
            "every issue."
        ),
        "state": "Which issues to list: open (default), closed, or all.",
        "since": (
            "Only issues updated after this time, as an ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)."
        ),
    },
    "example_next_call": {
        "tool": "call_operation",
        "args": {
            "op_id": GH_ISSUE_LIST_OP_ID,
            "params": {
                "owner": "<owner>",
                "repo": "<repo>",
                "state": "open",
                "per_page": 100,
                "page": 2,
            },
        },
    },
}
