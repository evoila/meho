# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The five GitHub ticket writes: ``safe``, no approval, only the target's repo and board.

``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md`` makes five
writes ``safe`` with ``requires_approval=False``: file an issue, comment on
an issue, link a sub-issue, add an item to a board, set a board field. This
file pins that posture and the guard that makes it true: each write is
refused before any call to GitHub when it would leave the repository or
board its target names, or when the target names none.

All names are made up; no live GitHub.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites import register_github_composite_operations
from meho_backplane.connectors.github.composites._board import (
    project_item_add_composite,
    project_item_set_field_composite,
)
from meho_backplane.connectors.github.composites._issues import (
    issue_comment_composite,
    issue_create_composite,
)
from meho_backplane.connectors.github.composites._scope import (
    TicketWriteScopeError,
    target_repo,
)
from meho_backplane.connectors.github.composites._sub_issues import sub_issue_add_composite
from meho_backplane.connectors.github.op_classes import TICKET_WRITE_OP_IDS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.op_classes import declared_op_class
from meho_backplane.settings import get_settings

_BOARD = "PVT_board"
_TARGET = SimpleNamespace(
    name="gh-acme", host="api.github.com/repos/acme/widgets", extras={"project_id": _BOARD}
)

Handler = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-gh-ticket-writes",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000e0e0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _NoCallsConnector:
    """Fails the test on any call to GitHub; records the GraphQL lookups it allows."""

    def __init__(self, graphql: list[Any] | None = None) -> None:
        self._graphql = list(graphql or [])
        self.writes: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(self, target: Any, path: str, **_: Any) -> Any:
        raise AssertionError(f"unexpected GET {path}")

    async def _post_json(self, target: Any, path: str, *, json: Any = None, **_: Any) -> Any:
        query = (json or {}).get("query", "")
        if path == "/graphql" and "node(id: $id)" in query and self._graphql:
            return self._graphql.pop(0)
        self.writes.append(path)
        raise AssertionError(f"unexpected write {path}")


_REPO_WRITES: list[tuple[Handler, dict[str, Any]]] = [
    (issue_create_composite, {"title": "t"}),
    (issue_comment_composite, {"issue_number": 7, "body": "b"}),
    (sub_issue_add_composite, {"issue_number": 7, "sub_issue_number": 8}),
]
_BOARD_WRITES: list[tuple[Handler, dict[str, Any]]] = [
    (project_item_add_composite, {"content_id": "I_x"}),
    (project_item_set_field_composite, {"item_id": "PVTI", "field_id": "F", "option_id": "o"}),
]


async def _run(handler: Handler, target: Any, params: dict[str, Any], connector: Any) -> Any:
    return await handler(operator=_operator(), target=target, params=params, connector=connector)


# ---------------------------------------------------------------------------
# Posture: safe, no approval, declared as a write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_five_ticket_writes_are_safe_with_no_approval() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_github_composite_operations(embedding_service=service)
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(TICKET_WRITE_OP_IDS)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == dict.fromkeys(
        TICKET_WRITE_OP_IDS, ("safe", False)
    )


@pytest.mark.parametrize("op_id", TICKET_WRITE_OP_IDS)
def test_each_ticket_write_is_declared_a_write(op_id: str) -> None:
    assert declared_op_class(op_id) == "write"


# ---------------------------------------------------------------------------
# Only the target's repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "params"), _REPO_WRITES)
async def test_a_repository_write_to_another_repository_is_refused(
    handler: Handler, params: dict[str, Any]
) -> None:
    connector = _NoCallsConnector()
    for owner, repo in (("acme", "other"), ("other", "widgets")):
        with pytest.raises(TicketWriteScopeError, match="not the repository of target"):
            await _run(handler, _TARGET, {"owner": owner, "repo": repo, **params}, connector)
    assert connector.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["api.github.com", "", "api.github.com/acme", "github.com/"])
@pytest.mark.parametrize(("handler", "params"), _REPO_WRITES)
async def test_a_repository_write_on_a_target_without_a_repository_is_refused(
    handler: Handler, params: dict[str, Any], host: str
) -> None:
    target = SimpleNamespace(name="gh-org", host=host, extras={})
    with pytest.raises(TicketWriteScopeError, match=r"names no repository.*api\.github\.com/repos"):
        await _run(
            handler, target, {"owner": "acme", "repo": "widgets", **params}, _NoCallsConnector()
        )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.github.com/repos/acme/widgets", ("acme", "widgets")),
        ("api.github.com/acme/widgets", ("acme", "widgets")),
        ("acme/widgets", ("acme", "widgets")),
        ("acme/widgets.js", ("acme", "widgets.js")),
        ("api.github.com/repos/acme/widgets/", ("acme", "widgets")),
        ("api.github.com", None),
        ("api.github.com/acme", None),
        ("", None),
    ],
)
def test_target_repo_reads_the_repository_from_the_host(
    host: str, expected: tuple[str, str] | None
) -> None:
    assert target_repo(SimpleNamespace(host=host)) == expected


def test_target_repo_without_a_host_is_none() -> None:
    assert target_repo(object()) is None


# ---------------------------------------------------------------------------
# Only the target's board, and only content of the target's repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "params"), _BOARD_WRITES)
async def test_a_board_write_to_another_board_is_refused(
    handler: Handler, params: dict[str, Any]
) -> None:
    connector = _NoCallsConnector()
    with pytest.raises(TicketWriteScopeError, match="not the board of target"):
        await _run(handler, _TARGET, {"project_id": "PVT_other", **params}, connector)
    assert connector.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", [{}, {"project_id": ""}, {"project_id": 7}, None])
@pytest.mark.parametrize(("handler", "params"), _BOARD_WRITES)
async def test_a_board_write_on_a_target_without_a_board_is_refused(
    handler: Handler, params: dict[str, Any], extras: Any
) -> None:
    target = SimpleNamespace(name="gh-acme", host="acme/widgets", extras=extras)
    with pytest.raises(TicketWriteScopeError, match=r"names no board.*extras\.project_id"):
        await _run(handler, target, {"project_id": _BOARD, **params}, _NoCallsConnector())


def _content(owner: str, repo: str, kind: str = "Issue") -> dict[str, Any]:
    repository = {"owner": {"login": owner}, "name": repo}
    return {"data": {"node": {"__typename": kind, "repository": repository}}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "match"),
    [
        (_content("acme", "other"), "not the repository of target"),
        (_content("other", "widgets", kind="PullRequest"), "not the repository of target"),
        ({"data": {"node": {"__typename": "DraftIssue"}}}, "not an issue or pull request"),
        ({"data": {"node": None}}, "not an issue or pull request"),
    ],
)
async def test_adding_content_from_another_repository_to_the_board_is_refused(
    answer: dict[str, Any], match: str
) -> None:
    connector = _NoCallsConnector(graphql=[answer])
    with pytest.raises(TicketWriteScopeError, match=match):
        await _run(
            project_item_add_composite,
            _TARGET,
            {"project_id": _BOARD, "content_id": "I_x"},
            connector,
        )
    assert connector.writes == []
