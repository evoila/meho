# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The create and edit answers list the agent's live grants (#6584).

When the agent already has a live grant, MEHO asks for no starting grant
pick. The answers of ``POST`` / ``PATCH /api/v1/agents`` and of the
``meho_agents_create`` / ``meho_agents_edit`` tools then show those grants,
so the person sees what the agent may do. A grant that has run out is not
live: it is not listed, and MEHO asks for the pick.

The helpers and the autouse fixtures come from
``test_agent_starting_grants.py`` and ``test_agent_starting_grants_edit.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from meho_backplane.agents import live_grants
from meho_backplane.agents.live_grants import load_live_grants
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission
from meho_backplane.main import app
from meho_backplane.mcp.registry import tool_for_settings
from meho_backplane.mcp.tools import agents as mcp_agents
from meho_backplane.mcp.tools.agents import _create_handler, _edit_handler
from tests.test_agent_starting_grants import (  # noqa: F401 -- the autouse fixtures apply by import
    _ADMIN,
    _TENANT_A,
    _TENANT_B,
    _definition,
    _operator,
    _registered,
    _seed_principal,
    _seed_target,
    _seed_tenants,
    _settings_env,
    _stub_vault_write,
    _switch,
)
from tests.test_agent_starting_grants_edit import _seed_definition, _send

from ._vault_fakes import install_fake_vault


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _add_grant(
    sub: str,
    *,
    op_pattern: str = "vault.*",
    target_scope: str = "*",
    verdict: str = "auto-execute",
    expires_at: datetime | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
) -> str:
    async with get_sessionmaker()() as session:
        row = AgentPermission(
            tenant_id=tenant_id,
            principal_sub=sub,
            op_pattern=op_pattern,
            target_scope=target_scope,
            verdict=verdict,
            created_by_sub=_ADMIN,
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()
        return str(row.id)


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=3)


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_without_pick_lists_the_one_live_grant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    grant_id = await _add_grant(sub)
    _switch(monkeypatch, "on")
    resp = _send(client, "POST", "/api/v1/agents", _definition(sub))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "triage"
    assert body["live_grants_truncated"] is False
    assert body["live_grants"] == [
        {
            "id": grant_id,
            "op_pattern": "vault.*",
            "target_scope": "*",
            "target_name": None,
            "verdict": "auto-execute",
            "expires_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_create_lists_an_all_operations_grant_on_one_named_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    target_id = await _seed_target("alpha-vault", aliases=[])
    await _add_grant(sub, op_pattern="*", target_scope=target_id)
    _switch(monkeypatch, "on")
    resp = _send(client, "POST", "/api/v1/agents", _definition(sub))
    assert resp.status_code == 201, resp.text
    (grant,) = resp.json()["live_grants"]
    assert grant["op_pattern"] == "*"
    assert grant["target_scope"] == target_id
    assert grant["target_name"] == "alpha-vault"


@pytest.mark.asyncio
async def test_create_lists_the_end_date_of_a_grant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    ends = _future()
    await _add_grant(sub, verdict="needs-approval", expires_at=ends)
    _switch(monkeypatch, "on")
    resp = _send(client, "POST", "/api/v1/agents", _definition(sub))
    assert resp.status_code == 201, resp.text
    (grant,) = resp.json()["live_grants"]
    assert grant["verdict"] == "needs-approval"
    assert datetime.fromisoformat(grant["expires_at"]) == ends


@pytest.mark.asyncio
async def test_a_grant_that_ran_out_is_not_live_and_the_pick_is_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _add_grant(sub, expires_at=_past())
    _switch(monkeypatch, "on")
    refused = _send(client, "POST", "/api/v1/agents", _definition(sub))
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"].startswith("starting_grants_required:")

    clear_jwks_cache()  # each _send signs with a fresh key
    saved = _send(client, "POST", "/api/v1/agents", _definition(sub, {"set": "none"}))
    assert saved.status_code == 201, saved.text
    assert saved.json()["live_grants"] == []


@pytest.mark.asyncio
async def test_create_with_a_pick_lists_the_grant_it_wrote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    resp = _send(client, "POST", "/api/v1/agents", _definition(sub, {"set": "read-everything"}))
    assert resp.status_code == 201, resp.text
    (grant,) = resp.json()["live_grants"]
    assert (grant["op_pattern"], grant["target_scope"]) == ("*", "*")


@pytest.mark.asyncio
async def test_patch_to_an_agent_with_a_live_grant_lists_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new = await _seed_definition()
    await _add_grant(new, op_pattern="k8s.*")
    _switch(monkeypatch, "on")
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": new})
    assert resp.status_code == 200, resp.text
    assert [g["op_pattern"] for g in resp.json()["live_grants"]] == ["k8s.*"]


@pytest.mark.asyncio
async def test_get_answer_has_no_live_grants_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_definition()
    resp = _send(client, "GET", "/api/v1/agents/triage", {})
    assert resp.status_code == 200, resp.text
    assert "live_grants" not in resp.json()


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_create_answer_names_the_live_grants() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _add_grant(sub, op_pattern="*")
    result = await _create_handler(_operator(), _definition(sub))
    assert result["name"] == "triage"
    assert "live_grants" not in result["agent"]
    assert [g["op_pattern"] for g in result["live_grants"]] == ["*"]
    assert result["live_grants_truncated"] is False


@pytest.mark.asyncio
async def test_mcp_edit_answer_names_the_live_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    _old, new = await _seed_definition()
    await _add_grant(new, expires_at=_future())
    _switch(monkeypatch, "on")
    result = await _edit_handler(_operator(), {"name": "triage", "identity_ref": new})
    (grant,) = result["live_grants"]
    assert grant["expires_at"] is not None


@pytest.mark.parametrize("tool", ["meho_agents_create", "meho_agents_edit"])
def test_mcp_tool_text_is_unchanged_while_the_switch_is_off(tool: str) -> None:
    defn = _registered(tool, mcp_agents)
    assert tool_for_settings(defn, _operator()) is defn
    assert "live_grants" not in defn.description


@pytest.mark.parametrize("tool", ["meho_agents_create", "meho_agents_edit"])
def test_mcp_tool_text_names_live_grants_while_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    defn = _registered(tool, mcp_agents)
    _switch(monkeypatch, "on")
    assert "live_grants" in tool_for_settings(defn, _operator()).description


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reads_only_the_callers_tenant() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _add_grant(sub, op_pattern="mine.*")
    await _add_grant(sub, op_pattern="other.*", tenant_id=_TENANT_B)
    live = await load_live_grants(_TENANT_A, sub)
    assert [g.op_pattern for g in live.grants] == ["mine.*"]


@pytest.mark.asyncio
async def test_list_names_no_target_from_another_tenant() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    other_target = await _seed_target("beta-vault", tenant_id=_TENANT_B, aliases=[])
    await _add_grant(sub, target_scope=other_target)
    (grant,) = (await load_live_grants(_TENANT_A, sub)).grants
    assert grant.target_name is None
    assert grant.target_scope == other_target


@pytest.mark.asyncio
async def test_a_long_list_says_it_was_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    for pattern in ("a.*", "b.*", "c.*"):
        await _add_grant(sub, op_pattern=pattern)
    monkeypatch.setattr(live_grants, "MAX_LIVE_GRANTS", 2)
    live = await load_live_grants(_TENANT_A, sub)
    assert [g.op_pattern for g in live.grants] == ["a.*", "b.*"]
    assert live.truncated is True


@pytest.mark.asyncio
async def test_an_agent_with_no_grant_has_an_empty_complete_list() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    live = await load_live_grants(_TENANT_A, sub)
    assert live.grants == []
    assert live.truncated is False
