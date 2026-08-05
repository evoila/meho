# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``gh.composite.sub_issue_add`` (direct-session REST, #2081).

The composite issues ``POST /repos/{owner}/{repo}/issues/{issue_number}/
sub_issues`` through the resolved ``GitHubRestConnector`` session
(``connector._post_json`` mounted through ``mount_op_path``). These tests
drive the handler with a recording session stub -- no live GitHub.

Coverage:

* Happy path -- the write posts the mounted path with a
  ``{"sub_issue_id": ...}`` body and returns the parent payload +
  ``status="linked"``.
* ``replace_parent`` is included in the body only when supplied.
* A transport failure (``httpx.HTTPStatusError``) propagates for the
  dispatcher's outer branch to classify.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._sub_issues import sub_issue_add_composite


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-gh-sub-issue",
        name="GH Sub-Issue Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c0c0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` the way ``raise_for_status`` would."""
    request = httpx.Request("POST", "https://api.github.com/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"http_{status_code}", request=request, response=response)


class _RecordingConnector:
    """Session stub matching the subset of ``GitHubRestConnector`` the handler uses."""

    def __init__(self, response: Any) -> None:
        self._response = response
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
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_sub_issue_add_posts_database_id_body() -> None:
    """The write posts the mounted path with a ``{"sub_issue_id": ...}`` body."""
    parent_payload = {"number": 100, "sub_issues_summary": {"total": 1}}
    connector = _RecordingConnector(parent_payload)
    result = await sub_issue_add_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "issue_number": 100, "sub_issue_id": 987654},
        connector=connector,  # type: ignore[arg-type]
    )
    call = connector.post_calls[0]
    assert call["path"] == "/repos/evoila/meho/issues/100/sub_issues"
    assert call["verb"] == "POST"
    assert call["json"] == {"sub_issue_id": 987654}
    assert result == {
        "parent": parent_payload,
        "parent_number": 100,
        "sub_issue_id": 987654,
        "status": "linked",
    }


@pytest.mark.asyncio
async def test_sub_issue_add_includes_replace_parent_when_set() -> None:
    """``replace_parent`` is added to the body only when the caller passes it."""
    connector = _RecordingConnector({"number": 100})
    await sub_issue_add_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "owner": "evoila",
            "repo": "meho",
            "issue_number": 100,
            "sub_issue_id": 5,
            "replace_parent": True,
        },
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.post_calls[0]["json"] == {"sub_issue_id": 5, "replace_parent": True}


@pytest.mark.asyncio
async def test_sub_issue_add_omits_replace_parent_by_default() -> None:
    """Without ``replace_parent`` the body carries only ``sub_issue_id``."""
    connector = _RecordingConnector({"number": 100})
    await sub_issue_add_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "issue_number": 100, "sub_issue_id": 5},
        connector=connector,  # type: ignore[arg-type]
    )
    assert "replace_parent" not in connector.post_calls[0]["json"]


@pytest.mark.asyncio
async def test_sub_issue_add_propagates_http_error() -> None:
    """A 422 (already-parented / cross-owner) propagates for the dispatcher to map."""
    connector = _RecordingConnector(_http_error(422))
    with pytest.raises(httpx.HTTPStatusError):
        await sub_issue_add_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "issue_number": 100, "sub_issue_id": 5},
            connector=connector,  # type: ignore[arg-type]
        )
