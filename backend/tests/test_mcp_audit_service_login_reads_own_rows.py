# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A service login reads only its own audit rows through the MCP audit tools.

``query_audit`` (flat and ``shape="tree"``) and ``meho_audit_replay`` run
against real rows (:mod:`tests._audit_own_rows_seed`) of a service login and
of a person in one tenant. The service login gets only its own rows, also
with the ``tenant_admin`` role. The tree shape holds only the rows of the
caller's own calls, for a person too, whatever session id the
``Mcp-Session-Id`` header names. A session that is not the caller's answers
like a session that does not exist.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

from ._audit_own_rows_seed import PERSON_SUB, SERVICE_SUB, WINDOW, Seeded, seed


def _operator(sub: str, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


_SERVICE = _operator(SERVICE_SUB, PrincipalKind.SERVICE)
_PERSON = _operator(PERSON_SUB, PrincipalKind.USER)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@pytest.fixture
async def seeded() -> Seeded:
    return await seed(OPERATOR_TENANT_ID)


def _tool(
    client: TestClient,
    operator: Operator,
    name: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )
    body = response.json()
    assert "result" in body, body
    assert body["result"]["isError"] is False, body
    payload: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _ids(payload: dict[str, Any]) -> set[str]:
    return {row["id"] for row in payload["rows"]}


def _tree(payload: dict[str, Any]) -> dict[str, Any]:
    """The tree answer without the fields that do not depend on the session.

    ``excluded_null_session_count`` grows with each MCP call of the test, so
    the tests check it once, on the first call.
    """
    return {key: payload[key] for key in ("root", "row_count", "loose_row_count")}


async def test_query_audit_lists_only_own_rows(client: TestClient, seeded: Seeded) -> None:
    rows = _ids(_tool(client, _SERVICE, "query_audit", WINDOW))
    assert seeded.service.all_ids <= rows
    assert not rows & seeded.person.all_ids
    assert str(seeded.person_child_of_service) not in rows

    for args in (
        {**WINDOW, "principal": PERSON_SUB},
        {**WINDOW, "audit_id": str(seeded.person.traced)},
        {**WINDOW, "agent_session_id": str(seeded.person_session)},
    ):
        assert _ids(_tool(client, _SERVICE, "query_audit", args)) == set()


async def test_query_audit_person_keeps_every_row(client: TestClient, seeded: Seeded) -> None:
    rows = _ids(_tool(client, _PERSON, "query_audit", WINDOW))
    assert seeded.service.all_ids | seeded.person.all_ids <= rows


async def test_tree_of_another_session_answers_like_a_missing_session(
    client: TestClient, seeded: Seeded
) -> None:
    """The tree shape holds only the rows of the caller's own calls.

    A header that names another session does not open it, for a person and
    for a service login.
    """

    def tree(operator: Operator, session_id: uuid.UUID, header: uuid.UUID | None = None) -> Any:
        headers = {"Mcp-Session-Id": str(header)} if header else None
        args = {"shape": "tree", "agent_session_id": str(session_id)}
        return _tree(_tool(client, operator, "query_audit", args, headers=headers))

    first = _tool(
        client,
        _SERVICE,
        "query_audit",
        {"shape": "tree", "agent_session_id": str(uuid.uuid4())},
    )
    # Only its own MCP calls with no session are counted.
    assert first["excluded_null_session_count"] == 3
    absent = _tree(first)
    assert absent["root"] == [] and absent["row_count"] == 0

    # A person: their own session replays, another session does not.
    assert tree(_PERSON, seeded.person_session)["row_count"] == 2
    assert tree(_PERSON, seeded.service_session, header=seeded.service_session) == absent
    person_mixed = tree(_PERSON, seeded.mixed_session)
    assert person_mixed["row_count"] == 1
    assert person_mixed["root"][0]["principal_sub"] == PERSON_SUB

    # A service login: the same, and only its own row of the mixed session.
    assert tree(_SERVICE, seeded.person_session, header=seeded.person_session) == absent
    mixed = tree(_SERVICE, seeded.mixed_session)
    assert mixed["row_count"] == 1
    assert mixed["root"][0]["principal_sub"] == SERVICE_SUB
    own = tree(_SERVICE, seeded.service_session)
    assert own["row_count"] == 2
    assert [c["id"] for c in own["root"][0]["children"]] == [str(seeded.service.session_child)]


def _nodes(forest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for node in forest for n in (node, *_nodes(node.get("children", [])))]


async def test_tree_after_a_call_with_the_header_of_another_session_shows_only_own_rows(
    client: TestClient, seeded: Seeded
) -> None:
    """Two requests with the session id of another login in the header.

    The first request makes MEHO store a row of the person in the session of
    the service login. The second request replays that session: the tree
    holds the person's own row and no row of the service login.
    """
    header = {"Mcp-Session-Id": str(seeded.service_session)}
    _tool(client, _PERSON, "query_audit", WINDOW, headers=header)
    args = {"shape": "tree", "agent_session_id": str(seeded.service_session)}
    nodes = _nodes(_tool(client, _PERSON, "query_audit", args, headers=header)["root"])

    assert nodes, "the first request stored a row of the person in the session"
    assert {n["principal_sub"] for n in nodes} == {PERSON_SUB}
    assert not {n["id"] for n in nodes} & seeded.service.all_ids


async def test_meho_audit_replay_service_login_replays_only_own_sessions(
    client: TestClient, seeded: Seeded
) -> None:
    def replay(operator: Operator, session_id: uuid.UUID) -> dict[str, Any]:
        return _tree(_tool(client, operator, "meho_audit_replay", {"session_id": str(session_id)}))

    absent = replay(_SERVICE, uuid.uuid4())
    assert absent["root"] == [] and absent["row_count"] == 0
    assert replay(_SERVICE, seeded.person_session) == absent
    mixed = replay(_SERVICE, seeded.mixed_session)
    assert mixed["row_count"] == 1
    assert mixed["root"][0]["principal_sub"] == SERVICE_SUB
    assert replay(_SERVICE, seeded.service_session)["row_count"] == 2

    # A person with tenant_admin keeps the cross-session replay.
    assert replay(_PERSON, seeded.mixed_session)["row_count"] == 2
    assert replay(_PERSON, seeded.service_session)["row_count"] == 3
