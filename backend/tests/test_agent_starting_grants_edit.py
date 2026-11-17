# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The starting grant pick on an agent definition edit, and the missing-pick text (#6389).

* ``PATCH /api/v1/agents/{name}`` and ``meho_agents_edit`` ask for the
  pick when ``identity_ref`` changes to an agent with no live grant, the
  same rule as a create.
* The ``starting_grants_required`` text is true for the switch value:
  with ``observe`` it does not say that the agent runs nothing.

The helpers and the autouse fixtures come from
``test_agent_starting_grants.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.agents.grant_sets import STARTING_GRANTS_JSON_SCHEMA, require_pick
from meho_backplane.agents.schemas import AgentDefinitionUpdate
from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition
from meho_backplane.main import app
from meho_backplane.mcp.handlers import handle_tools_call
from meho_backplane.mcp.registry import tool_for_settings
from meho_backplane.mcp.server import McpInvalidParamsError, McpPermissionDeniedError
from meho_backplane.mcp.tools import agents as mcp_agents
from meho_backplane.mcp.tools.agents import _edit_handler
from tests.test_agent_starting_grants import (  # noqa: F401 -- the autouse fixtures apply by import
    _ADMIN,
    _TENANT_A,
    _audit_rows,
    _definition,
    _grants,
    _mock_kc,
    _operator,
    _registered,
    _seed_grant,
    _seed_principal,
    _seed_target,
    _seed_tenants,
    _settings_env,
    _stub_vault_write,
    _switch,
)

from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_KC_PATH = "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _send(
    client: TestClient, method: str, path: str, body: dict[str, Any], *, kind: str | None = None
) -> Any:
    key = make_rsa_keypair("kid-starting-edit")
    token = mint_token(
        key,
        sub=_ADMIN,
        tenant_role="tenant_admin",
        tenant_id=str(_TENANT_A),
        principal_kind=kind,
    )
    factory, _kc = _mock_kc()
    with patch(_KC_PATH, factory), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


async def _seed_definition(*, old_has_grant: bool = True) -> tuple[str, str]:
    """Seed a definition on one agent, and a second agent with no grant."""
    await _seed_tenants()
    old = await _seed_principal("old-bot")
    new = await _seed_principal("new-bot")
    if old_has_grant:
        await _seed_grant(old)
    async with get_sessionmaker()() as session:
        body = _definition(old)
        session.add(
            AgentDefinition(
                tenant_id=_TENANT_A,
                name=body["name"],
                identity_ref=old,
                model_tier=body["model_tier"],
                system_prompt=body["system_prompt"],
                toolset={},
                turn_budget=body["turn_budget"],
                enabled=True,
                created_by_sub=_ADMIN,
            )
        )
        await session.commit()
    return old, new


async def _identity_ref() -> str:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AgentDefinition.identity_ref).where(AgentDefinition.name == "triage")
        )
        return str(result.scalar_one())


# ---------------------------------------------------------------------------
# REST PATCH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["on", "observe"])
@pytest.mark.asyncio
async def test_patch_to_an_agent_without_grant_needs_the_pick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    old, new = await _seed_definition()
    _switch(monkeypatch, mode)
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": new})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("starting_grants_required:")
    for choice in ("'none'", "'read-everything'", "'read-targets'"):
        assert choice in detail
    assert await _identity_ref() == old
    assert await _grants(new) == []
    assert (await _audit_rows())[-1].payload["starting_grants"] == "not-chosen"


@pytest.mark.asyncio
async def test_patch_with_a_pick_writes_grants_to_the_new_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new = await _seed_definition()
    target_id = await _seed_target("vault-a", aliases=[])
    _switch(monkeypatch, "on")
    resp = _send(
        client,
        "PATCH",
        "/api/v1/agents/triage",
        {"identity_ref": new, "starting_grants": {"set": "read-targets", "targets": ["vault-a"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["identity_ref"] == new
    rows = await _grants(new)
    assert [(r.op_pattern, r.target_scope, r.created_by_sub) for r in rows] == [
        ("*", target_id, _ADMIN)
    ]
    audit = (await _audit_rows())[-1]
    assert audit.payload["op_id"] == "agent.edit"
    assert audit.payload["starting_grants"] == "read-targets"
    assert audit.payload["starting_grant_targets"] == ["vault-a"]


@pytest.mark.asyncio
async def test_patch_to_an_agent_with_a_grant_needs_no_pick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new = await _seed_definition()
    await _seed_grant(new)
    _switch(monkeypatch, "on")
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": new})
    assert resp.status_code == 200, resp.text
    assert await _identity_ref() == new
    assert len(await _grants(new)) == 1


@pytest.mark.asyncio
async def test_patch_that_keeps_the_agent_needs_no_pick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending the same identity_ref (the console edit form always does) asks nothing."""
    old, _new = await _seed_definition(old_has_grant=False)
    _switch(monkeypatch, "on")
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": old, "turn_budget": 9})
    assert resp.status_code == 200, resp.text
    assert resp.json()["turn_budget"] == 9


@pytest.mark.asyncio
async def test_patch_without_pick_works_as_before_when_switch_is_off(
    client: TestClient,
) -> None:
    _old, new = await _seed_definition()
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": new})
    assert resp.status_code == 200, resp.text
    assert await _grants(new) == []


@pytest.mark.asyncio
async def test_patch_unknown_target_changes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = await _seed_definition()
    _switch(monkeypatch, "on")
    resp = _send(
        client,
        "PATCH",
        "/api/v1/agents/triage",
        {"identity_ref": new, "starting_grants": {"set": "read-targets", "targets": ["nope"]}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("starting_grants_target_unknown:")
    assert await _identity_ref() == old
    assert await _grants(new) == []


@pytest.mark.asyncio
async def test_patch_pick_from_a_service_login_is_refused(
    client: TestClient,
) -> None:
    old, new = await _seed_definition()
    resp = _send(
        client,
        "PATCH",
        "/api/v1/agents/triage",
        {"identity_ref": new, "starting_grants": {"set": "read-everything"}},
        kind="service",
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"].startswith("human_principal_required:")
    assert await _identity_ref() == old
    assert await _grants(new) == []


def test_pick_without_identity_ref_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="only used together with 'identity_ref'"):
        AgentDefinitionUpdate.model_validate({"starting_grants": {"set": "none"}})


@pytest.mark.asyncio
async def test_patch_pick_without_identity_ref_is_422(client: TestClient) -> None:
    await _seed_definition()
    resp = _send(
        client, "PATCH", "/api/v1/agents/triage", {"starting_grants": {"set": "read-everything"}}
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# MCP meho_agents_edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_edit_needs_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = await _seed_definition()
    _switch(monkeypatch, "on")
    with pytest.raises(McpInvalidParamsError, match="starting_grants_required"):
        await _edit_handler(_operator(), {"name": "triage", "identity_ref": new})
    assert await _identity_ref() == old
    result = await _edit_handler(
        _operator(),
        {"name": "triage", "identity_ref": new, "starting_grants": {"set": "read-everything"}},
    )
    assert result["agent"]["identity_ref"] == new
    assert [(r.op_pattern, r.target_scope) for r in await _grants(new)] == [("*", "*")]


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE, PrincipalKind.AGENT])
@pytest.mark.asyncio
async def test_mcp_edit_pick_from_a_machine_login_is_refused(kind: PrincipalKind) -> None:
    old, new = await _seed_definition()
    with pytest.raises(McpPermissionDeniedError, match="human_principal_required"):
        await _edit_handler(
            _operator(kind=kind),
            {"name": "triage", "identity_ref": new, "starting_grants": {"set": "read-everything"}},
        )
    assert await _identity_ref() == old
    assert await _grants(new) == []


def test_mcp_edit_is_unchanged_while_the_switch_is_off() -> None:
    defn = _registered("meho_agents_edit", mcp_agents)
    assert "starting_grants" not in defn.inputSchema["properties"]
    assert tool_for_settings(defn, _operator()) is defn


@pytest.mark.parametrize("mode", ["observe", "on"])
def test_mcp_edit_shows_the_pick_while_the_switch_is_not_off(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    defn = _registered("meho_agents_edit", mcp_agents)
    _switch(monkeypatch, mode)
    shown = tool_for_settings(defn, _operator())
    assert shown.inputSchema["properties"]["starting_grants"] == STARTING_GRANTS_JSON_SCHEMA
    assert shown.inputSchema["required"] == ["name"]
    assert "starting_grants" in shown.description


@pytest.mark.asyncio
async def test_mcp_edit_call_with_a_pick_is_refused_while_the_switch_is_off() -> None:
    _registered("meho_agents_edit", mcp_agents)
    old, new = await _seed_definition()
    with (
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
        pytest.raises(McpInvalidParamsError, match="starting_grants"),
    ):
        await handle_tools_call(
            _operator(),
            {
                "name": "meho_agents_edit",
                "arguments": {
                    "name": "triage",
                    "identity_ref": new,
                    "starting_grants": {"set": "read-everything"},
                },
            },
        )
    assert await _identity_ref() == old
    assert await _grants(new) == []


# ---------------------------------------------------------------------------
# The text of starting_grants_required
# ---------------------------------------------------------------------------


def _required_text(monkeypatch: pytest.MonkeyPatch, mode: str) -> str:
    _switch(monkeypatch, mode)
    with pytest.raises(ValueError, match="starting_grants_required") as info:
        require_pick(None)
    return str(info.value)


def test_required_text_with_the_switch_on_says_the_agent_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _required_text(monkeypatch, "on")
    assert "'none' (the agent runs nothing until an admin adds a grant)" in text
    assert "observe" not in text


def test_required_text_with_the_switch_observe_says_nothing_is_refused_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _required_text(monkeypatch, "observe")
    assert "AGENT_GRANT_REQUIRED is 'observe'" in text
    assert "refuses no call of the agent yet" in text
    assert "the agent can still run safe operations at once" in text
    assert "the agent runs nothing until" not in text
    for choice in ("'none'", "'read-everything'", "'read-targets'"):
        assert choice in text


def test_no_text_while_the_switch_is_off() -> None:
    require_pick(None)
