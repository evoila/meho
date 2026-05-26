# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.1-T2 admin MCP agent-definition tools.

Coverage matrix (Task #809 acceptance criteria):

* Registration: the two read tools (``meho.agents.list`` /
  ``meho.agents.show``) are ``operator``-visible; the three write tools
  (``create`` / ``edit`` / ``delete``) are ``tenant_admin``-only.
* RBAC re-check: an ``operator`` direct ``tools/call`` against
  ``meho.agents.create`` is rejected by the dispatcher's call-time
  gate (Invalid Params + "forbidden").
* Tenant-admin happy path: create -> list -> show -> edit -> delete
  round-trips in-process through the service, producing the same DB
  rows the REST surface would.
* Error mapping: a duplicate create maps to Invalid Params
  ('agent_already_exists'); show / delete of a missing name maps to
  Invalid Params ('agent_not_found').
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, AgentPrincipal
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


async def _seed_principal(name: str, *, revoked: bool = False) -> None:
    """Seed an ``agent_principal`` for the operator's tenant.

    G11.2-T8 (#1099): the create / edit handlers now reject an
    ``identity_ref`` that doesn't resolve to a registered, non-revoked
    principal in the operator's tenant. Every MCP test that exercises
    create / edit needs the matching principal seeded first.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            AgentPrincipal(
                id=uuid.uuid4(),
                tenant_id=OPERATOR_TENANT_ID,
                name=name,
                keycloak_client_id=f"agent:{name}",
                keycloak_internal_id=f"kc-internal-{name}",
                owner_sub="op-admin",
                revoked=revoked,
                created_by_sub="op-admin",
            )
        )


_CREATE_ARGS: dict[str, Any] = {
    "name": "incident-triage",
    "identity_ref": "agent:incident-triage",
    "model_tier": "deep",
    "system_prompt": "You triage incidents.",
    "turn_budget": 25,
}


def _result_dict(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "error" not in body, body
    content = body["result"]["content"]
    return json.loads(content[0]["text"])


async def _agent_rows() -> list[AgentDefinition]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AgentDefinition).order_by(AgentDefinition.name))
        return list(result.scalars().all())


def _call(client: TestClient, name: str, arguments: dict[str, Any], rpc_id: int = 1) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_admin_sees_all_five_tools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    for tool in (
        "meho.agents.list",
        "meho.agents.show",
        "meho.agents.create",
        "meho.agents.edit",
        "meho.agents.delete",
    ):
        assert tool in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_operator_sees_only_read_tools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An ``operator`` sees the two read tools but not the three write tools."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "meho.agents.list" in names
    assert "meho.agents.show" in names
    assert "meho.agents.create" not in names
    assert "meho.agents.edit" not in names
    assert "meho.agents.delete" not in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_operator_create_call_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` to a write tool from an operator is rejected."""
    client, _op = client_with_operator
    resp = _call(client, "meho.agents.create", _CREATE_ARGS)
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Tenant-admin happy path round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_full_crud_round_trip(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    # G11.2-T8 (#1099) seed the agent_principal so the create's
    # identity_ref validation passes.
    await _seed_principal("incident-triage")

    # Create.
    created = _result_dict(_call(client, "meho.agents.create", _CREATE_ARGS))
    assert created["name"] == "incident-triage"
    assert uuid.UUID(created["agent"]["id"])
    assert created["agent"]["model_tier"] == "deep"

    # List.
    listed = _result_dict(_call(client, "meho.agents.list", {}))
    assert [a["name"] for a in listed["agents"]] == ["incident-triage"]

    # Show.
    shown = _result_dict(_call(client, "meho.agents.show", {"name": "incident-triage"}))
    assert shown["agent"]["turn_budget"] == 25

    # Edit (partial).
    edited = _result_dict(
        _call(client, "meho.agents.edit", {"name": "incident-triage", "enabled": False})
    )
    assert edited["agent"]["enabled"] is False
    assert edited["agent"]["model_tier"] == "deep"  # unchanged

    # Delete.
    removed = _result_dict(_call(client, "meho.agents.delete", {"name": "incident-triage"}))
    assert removed["removed"] is True
    assert await _agent_rows() == []


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_duplicate_create_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_principal("incident-triage")
    first = _call(client, "meho.agents.create", _CREATE_ARGS)
    assert "error" not in first.json()
    dup = _call(client, "meho.agents.create", _CREATE_ARGS, rpc_id=2)
    body = dup.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "agent_already_exists" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_missing_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = _call(client, "meho.agents.show", {"name": "nope"})
    body = resp.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "agent_not_found" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_validation_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """An out-of-range turn budget surfaces as Invalid Params (Pydantic re-validation)."""
    client, _op = client_with_operator
    resp = _call(client, "meho.agents.create", {**_CREATE_ARGS, "turn_budget": 99999})
    body = resp.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# G11.2-T8 (#1099) -- identity_ref validation at the MCP boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_create_unknown_identity_ref_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """create with an unknown identity_ref → INVALID_PARAMS 'identity_ref_unknown'."""
    client, _op = client_with_operator
    # Deliberately no _seed_principal call -- the identity_ref below has
    # no match in the registry.
    resp = _call(
        client,
        "meho.agents.create",
        {**_CREATE_ARGS, "name": "orphan", "identity_ref": "agent:does-not-exist"},
    )
    body = resp.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "identity_ref_unknown" in body["error"]["message"]
    # The reject must not have left a row.
    assert await _agent_rows() == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_edit_unknown_identity_ref_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """edit that swaps identity_ref to an unknown value → INVALID_PARAMS."""
    client, _op = client_with_operator
    await _seed_principal("incident-triage")
    created = _call(client, "meho.agents.create", _CREATE_ARGS)
    assert "error" not in created.json()
    resp = _call(
        client,
        "meho.agents.edit",
        {"name": "incident-triage", "identity_ref": "agent:nonexistent"},
    )
    body = resp.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "identity_ref_unknown" in body["error"]["message"]
    # The persisted row's identity_ref must still be the original.
    rows = await _agent_rows()
    assert len(rows) == 1
    assert rows[0].identity_ref == "agent:incident-triage"
