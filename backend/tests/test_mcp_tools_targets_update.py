# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho_targets_update`` operator-plane MCP tool.

The ``targets.update`` operation itself is covered in
:mod:`tests.test_targets_update_op`. Here:

* The tool registers with ``tenant_admin``, ``op_class='write'``, the
  operation's parameter schema and a parked-union ``outputSchema``.
* An ``operator`` does not see it in ``tools/list`` and a direct
  ``tools/call`` is refused.
* A ``tenant_admin``'s call and an agent's call wait for approval and
  change nothing until a person approves them.
* A field outside the schema, and a call without ``target_id``, give
  ``-32602``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.update import TARGETS_UPDATE_PARAMETER_SCHEMA
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests._targets_helpers import _insert_target
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TOOL = "meho_targets_update"
_OLD_REF = "tenants/old/path"
_NEW_REF = f"tenants/{OPERATOR_TENANT_ID}/new-path"


@pytest_asyncio.fixture
async def _seeded_target() -> AsyncIterator[TargetORM]:
    """The operator's tenant and one target in it."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    row = await _insert_target(
        tenant_id=OPERATOR_TENANT_ID,
        name="web-01",
        product="ssh",
        host="203.0.113.5",
        secret_ref=_OLD_REF,
    )
    yield row


def _update_call(client: TestClient, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _TOOL, "arguments": arguments},
        },
    ).json()


async def _stored_ref(target_id: uuid.UUID) -> str | None:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return row.secret_ref


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(result.scalar_one())


@pytest.fixture
def _agent_client() -> Iterator[TestClient]:
    """``TestClient`` bound to an agent login with the ``mcp:admin`` scope."""
    op = Operator(
        sub="agent:fixer",
        name="Fixer",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.AGENT,
        scopes=frozenset({"mcp:admin"}),
    )

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tool_registers_as_a_tenant_admin_write() -> None:
    entry = get_tool(_TOOL)
    assert entry is not None
    defn, _handler = entry
    assert defn.required_role is TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"
    assert defn.feature == "targets"
    assert defn.inputSchema == TARGETS_UPDATE_PARAMETER_SCHEMA
    assert defn.outputSchema is not None
    parked = next(b for b in defn.outputSchema["oneOf"] if "awaiting_approval" in str(b))
    assert parked["properties"]["status"]["const"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_an_operator_does_not_see_the_tool(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    body = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ).json()
    assert _TOOL not in {t["name"] for t in body["result"]["tools"]}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_an_operator_call_is_refused_and_changes_nothing(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_target: TargetORM,
) -> None:
    client, _op = client_with_operator
    body = _update_call(client, {"target_id": str(_seeded_target.id), "secret_ref": _NEW_REF})
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert await _stored_ref(_seeded_target.id) == _OLD_REF


# ---------------------------------------------------------------------------
# tenant_admin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.usefixtures("person_token_writes_run_at_once")
async def test_a_tenant_admin_call_waits_for_approval(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_target: TargetORM,
) -> None:
    client, _op = client_with_operator
    body = _update_call(client, {"target_id": str(_seeded_target.id), "secret_ref": _NEW_REF})
    payload = body["result"]["structuredContent"]
    assert payload["status"] == "awaiting_approval"
    assert payload["op_id"] == "targets.update"
    assert await _approval_count() == 1
    assert await _stored_ref(_seeded_target.id) == _OLD_REF


async def test_an_agent_call_waits_for_approval(
    _agent_client: TestClient,
    _seeded_target: TargetORM,
) -> None:
    body = _update_call(
        _agent_client, {"target_id": str(_seeded_target.id), "secret_ref": _NEW_REF}
    )
    assert body["result"]["structuredContent"]["status"] == "awaiting_approval"
    assert await _approval_count() == 1
    assert await _stored_ref(_seeded_target.id) == _OLD_REF


# ---------------------------------------------------------------------------
# Refused input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.parametrize(
    "arguments",
    [
        {"secret_ref": "tenants/x/y", "name": "renamed"},
        {"secret_ref": "tenants/x/y", "fingerprint": {"vendor": "x"}},
        {"secret_ref": "tenants/x/y", "tenant_id": str(uuid.uuid4())},
    ],
    ids=["rename", "fingerprint", "tenant_id"],
)
async def test_a_field_outside_the_schema_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_target: TargetORM,
    arguments: dict[str, Any],
) -> None:
    client, _op = client_with_operator
    body = _update_call(client, {"target_id": str(_seeded_target.id), **arguments})
    assert body["error"]["code"] == INVALID_PARAMS, body
    assert await _approval_count() == 0
    assert await _stored_ref(_seeded_target.id) == _OLD_REF


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_a_call_without_target_id_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    body = _update_call(client, {"secret_ref": "tenants/x/y"})
    assert body["error"]["code"] == INVALID_PARAMS, body
