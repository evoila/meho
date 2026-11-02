# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``gh.composite.sub_issue_add`` (direct-session REST).

The composite reads the child issue by number, checks it is an issue of the
target's repository, then posts ``POST /repos/{owner}/{repo}/issues/
{issue_number}/sub_issues`` with the child's database id. These tests drive
the handler with a recording session stub -- no live GitHub.

Coverage:

* Happy path -- the child is read, then the write posts the child's
  database id and returns the parent payload + ``status="linked"``.
* ``replace_parent`` is included in the body only when supplied.
* Refusals before any write: another repository than the target's, a
  target with no repository, a child that answers from another repository
  (a moved issue), a child that is a pull request, a child with no id.
* A failed read or write (``httpx.HTTPStatusError``) propagates for the
  dispatcher's outer branch to classify, and a failed read writes nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._scope import TicketWriteScopeError
from meho_backplane.connectors.github.composites._sub_issues import sub_issue_add_composite

_TARGET = SimpleNamespace(name="gh-acme", host="api.github.com/repos/acme/widgets", extras={})
_CHILD_PATH = "/repos/acme/widgets/issues/200"
_LINK_PATH = "/repos/acme/widgets/issues/100/sub_issues"


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


def _child(**extra: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": 987654,
        "number": 200,
        "repository_url": "https://api.github.com/repos/acme/widgets",
    }
    raw.update(extra)
    return raw


class _RecordingConnector:
    """Session stub matching the subset of ``GitHubRestConnector`` the handler uses."""

    def __init__(self, child: Any, response: Any) -> None:
        self._child = child
        self._response = response
        self.get_calls: list[str] = []
        self.post_calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.get_calls.append(path)
        if isinstance(self._child, Exception):
            raise self._child
        return self._child

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
        self.post_calls.append({"path": path, "verb": verb, "json": json})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _params(**extra: Any) -> dict[str, Any]:
    return {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 100,
        "sub_issue_number": 200,
        **extra,
    }


async def _link(connector: _RecordingConnector, target: Any = _TARGET, **extra: Any) -> Any:
    return await sub_issue_add_composite(
        operator=_make_operator(),
        target=target,
        params=_params(**extra),
        connector=connector,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_sub_issue_add_reads_the_child_and_posts_its_database_id() -> None:
    parent_payload = {"number": 100, "sub_issues_summary": {"total": 1}}
    connector = _RecordingConnector(_child(), parent_payload)
    result = await _link(connector)
    assert connector.get_calls == [_CHILD_PATH]
    call = connector.post_calls[0]
    assert call["path"] == _LINK_PATH
    assert call["verb"] == "POST"
    assert call["json"] == {"sub_issue_id": 987654}
    assert result == {
        "parent": parent_payload,
        "parent_number": 100,
        "sub_issue_number": 200,
        "sub_issue_id": 987654,
        "status": "linked",
    }


@pytest.mark.asyncio
async def test_sub_issue_add_owner_and_repo_match_without_case() -> None:
    connector = _RecordingConnector(_child(), {"number": 100})
    await sub_issue_add_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={**_params(), "owner": "Acme", "repo": "Widgets"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert len(connector.post_calls) == 1


@pytest.mark.asyncio
async def test_sub_issue_add_includes_replace_parent_when_set() -> None:
    connector = _RecordingConnector(_child(), {"number": 100})
    await _link(connector, replace_parent=True)
    assert connector.post_calls[0]["json"] == {"sub_issue_id": 987654, "replace_parent": True}


@pytest.mark.asyncio
async def test_sub_issue_add_omits_replace_parent_by_default() -> None:
    connector = _RecordingConnector(_child(), {"number": 100})
    await _link(connector)
    assert "replace_parent" not in connector.post_calls[0]["json"]


@pytest.mark.asyncio
async def test_sub_issue_add_refuses_another_repository() -> None:
    connector = _RecordingConnector(_child(), {"number": 100})
    with pytest.raises(TicketWriteScopeError, match="not the repository of target"):
        await sub_issue_add_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={**_params(), "repo": "other"},
            connector=connector,  # type: ignore[arg-type]
        )
    assert connector.get_calls == []
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_refuses_a_target_without_a_repository() -> None:
    connector = _RecordingConnector(_child(), {"number": 100})
    bare = SimpleNamespace(name="gh-org", host="api.github.com", extras={})
    with pytest.raises(TicketWriteScopeError, match="names no repository"):
        await _link(connector, target=bare)
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_refuses_a_child_from_another_repository() -> None:
    """A moved issue answers from its new repository; MEHO must not link it."""
    moved = _child(repository_url="https://api.github.com/repos/acme/elsewhere")
    connector = _RecordingConnector(moved, {"number": 100})
    with pytest.raises(TicketWriteScopeError, match="not an issue of acme/widgets"):
        await _link(connector)
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_refuses_a_child_without_a_repository_url() -> None:
    connector = _RecordingConnector(_child(repository_url=None), {"number": 100})
    with pytest.raises(TicketWriteScopeError, match="unknown repository"):
        await _link(connector)
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_refuses_a_pull_request_as_child() -> None:
    connector = _RecordingConnector(_child(pull_request={"url": "x"}), {"number": 100})
    with pytest.raises(TicketWriteScopeError, match="pull request"):
        await _link(connector)
    assert connector.post_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [None, "987654", True])
async def test_sub_issue_add_refuses_a_child_without_a_database_id(bad_id: object) -> None:
    connector = _RecordingConnector(_child(id=bad_id), {"number": 100})
    with pytest.raises(TicketWriteScopeError, match="no database id"):
        await _link(connector)
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_failed_child_read_writes_nothing() -> None:
    connector = _RecordingConnector(_http_error(404), {"number": 100})
    with pytest.raises(httpx.HTTPStatusError):
        await _link(connector)
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_sub_issue_add_propagates_http_error() -> None:
    """A 422 (already has a parent) propagates for the dispatcher to map."""
    connector = _RecordingConnector(_child(), _http_error(422))
    with pytest.raises(httpx.HTTPStatusError):
        await _link(connector)
