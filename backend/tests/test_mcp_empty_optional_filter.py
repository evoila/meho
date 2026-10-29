# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An empty optional text filter on a read tool means "no filter" (#4110).

Small models often send unused filters as empty text, for example
``list_targets`` with ``connector_id: ""``. Before this change the tool
filtered on the empty text, matched nothing, and answered with an empty
list and no warning. ``tools/call`` now drops empty or blank optional
text arguments for read tools before the schema check, so the tool sees
them as not given. Empty required fields are still refused, and write
tools are not changed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.handlers as mcp_handlers
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OPERATOR = [TenantRole.OPERATOR]


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one ``tools/call`` and return the JSON-RPC response body."""
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    body: dict[str, Any] = response.json()
    return body


def _answer(body: dict[str, Any]) -> Any:
    """Return the error, or the tool result without its timing field."""
    if "error" in body:
        return body["error"]
    content = json.loads(body["result"]["content"][0]["text"])
    content.pop("query_duration_ms", None)
    return content


@pytest.fixture
def seen_arguments(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the arguments each tool handler receives, then run the real handler."""
    seen: list[dict[str, Any]] = []

    def _spying_get_tool(name: str) -> Any:
        entry = get_tool(name)
        if entry is None:
            return None
        defn, handler = entry

        async def _spy(operator: Operator, arguments: dict[str, Any]) -> Any:
            seen.append(dict(arguments))
            return await handler(operator, arguments)

        return defn, _spy

    monkeypatch.setattr(mcp_handlers, "get_tool", _spying_get_tool)
    return seen


async def _seed_targets() -> list[str]:
    """Seed the operator tenant with three targets; return their names."""
    names = ["alpha", "bravo", "charlie"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-test-tenant", name="Op Test"))
        for name, product in zip(names, ("vmware", "k8s", "vault"), strict=True):
            session.add(
                TargetORM(
                    tenant_id=OPERATOR_TENANT_ID,
                    name=name,
                    product=product,
                    host=f"{name}.example",
                    port=443,
                    secret_ref=f"targets/{name}",
                    auth_model="shared_service_account",
                )
            )
    return names


@pytest.mark.parametrize("client_with_operator", _OPERATOR, indirect=True)
@pytest.mark.parametrize("empty", ["", "   "])
async def test_list_targets_empty_connector_id_returns_all_targets(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    empty: str,
) -> None:
    """``connector_id: ""`` lists the same targets as leaving it out."""
    client, _ = client_with_operator
    names = await _seed_targets()

    unfiltered = _call(client, "list_targets", {})
    with_empty = _call(client, "list_targets", {"connector_id": empty})

    assert "error" not in with_empty, with_empty
    rows = with_empty["result"]["structuredContent"]["targets"]
    assert sorted(row["name"] for row in rows) == names
    assert with_empty["result"] == unfiltered["result"]


_EMPTY_FILTER_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("list_targets", {}, "connector_id"),
    ("search_operations", {"connector_id": "vault-1.x", "query": "read"}, "group"),
    ("meho_runbook_list_runs", {}, "template_slug"),
    ("meho_runbook_list_templates", {}, "target_kind"),
    ("query_topology", {"kind": "dependencies", "target": "alpha"}, "kind_filter"),
]


@pytest.mark.parametrize("client_with_operator", _OPERATOR, indirect=True)
@pytest.mark.parametrize(("tool", "base_args", "filter_key"), _EMPTY_FILTER_CASES)
def test_empty_filter_reaches_handler_as_not_given(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    seen_arguments: list[dict[str, Any]],
    tool: str,
    base_args: dict[str, Any],
    filter_key: str,
) -> None:
    """The handler sees no filter key, and the answer matches a call without it."""
    client, _ = client_with_operator

    unfiltered = _call(client, tool, base_args)
    with_empty = _call(client, tool, {**base_args, filter_key: ""})

    assert seen_arguments == [base_args, base_args]
    assert _answer(with_empty) == _answer(unfiltered)


@pytest.mark.parametrize("client_with_operator", _OPERATOR, indirect=True)
def test_empty_required_field_is_still_refused(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    seen_arguments: list[dict[str, Any]],
) -> None:
    """``search_operations`` with ``query: ""`` still fails the schema check."""
    client, _ = client_with_operator

    body = _call(client, "search_operations", {"connector_id": "vault-1.x", "query": ""})

    assert body["error"]["code"] == INVALID_PARAMS
    assert seen_arguments == []


@pytest.mark.parametrize("client_with_operator", _OPERATOR, indirect=True)
def test_write_tool_keeps_empty_optional_text(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    seen_arguments: list[dict[str, Any]],
) -> None:
    """A write tool still receives an empty optional text value unchanged."""
    client, _ = client_with_operator
    arguments = {"scope": "user", "body": "note", "ttl": ""}

    _call(client, "add_to_memory", arguments)

    assert seen_arguments == [arguments]
