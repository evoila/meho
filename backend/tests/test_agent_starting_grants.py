# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Starting grant sets, picked when an agent is registered or defined (#6191).

Covers the service, the REST routes and the admin MCP tools:

* each set writes the right ``agent_permission`` rows;
* ``read-targets`` with an unknown, ambiguous or other-tenant name writes
  nothing and creates no login;
* the pick is required while ``AGENT_GRANT_REQUIRED`` is not ``off``;
* for a definition, only when its agent has no live grant;
* the audit row of the creation names the set.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.agents.grant_sets import (
    STARTING_GRANTS_JSON_SCHEMA,
    StartingGrants,
    StartingGrantsError,
)
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.schemas import AgentDefinitionCreate
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.agent_principals import AgentPrincipalCreate, AgentPrincipalService
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentPermission,
    AgentPrincipal,
    AuditLog,
    Target,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.handlers import handle_tools_call
from meho_backplane.mcp.registry import (
    MCP_ADMIN_SCOPE,
    ToolDefinition,
    get_tool,
    tool_for_settings,
)
from meho_backplane.mcp.server import McpInvalidParamsError, McpPermissionDeniedError
from meho_backplane.mcp.tools import agent_principals as mcp_agent_principals
from meho_backplane.mcp.tools import agents as mcp_agents
from meho_backplane.mcp.tools.agent_principals import _register_handler
from meho_backplane.mcp.tools.agents import _create_handler
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_ADMIN = "op-admin"
_KC_PATH = "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _stub_vault_write(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_write(identity_ref: str, client_secret: str, *, tenant_id: uuid.UUID) -> str:
        return f"secret/data/agents/{identity_ref}/credentials"

    monkeypatch.setattr("meho_backplane.auth.agent_principals.write_agent_secret", _noop_write)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _switch(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", mode)
    get_settings.cache_clear()


def _mock_kc() -> tuple[MagicMock, AsyncMock]:
    kc = AsyncMock()
    kc.create_client = AsyncMock(return_value="kc-internal-1")
    kc.get_client_secret = AsyncMock(return_value="made-up-client-secret")
    kc.delete_client = AsyncMock(return_value=None)
    kc.__aenter__ = AsyncMock(return_value=kc)
    kc.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=kc), kc


def _operator(
    tenant_id: uuid.UUID = _TENANT_A,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=_ADMIN,
        name="Admin",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


async def _seed_tenants() -> None:
    async with get_sessionmaker()() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            if (await session.execute(select(Tenant).where(Tenant.id == tid))).first() is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _seed_target(name: str, *, tenant_id: uuid.UUID = _TENANT_A, aliases: list[str]) -> str:
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=tenant_id,
            name=name,
            aliases=aliases,
            product="vault",
            host=f"{name}.example.test",
            port=8200,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
        )
        session.add(target)
        await session.commit()
        return str(target.id)


async def _seed_principal(name: str) -> str:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                tenant_id=_TENANT_A,
                name=name,
                keycloak_client_id=f"agent:{name}",
                keycloak_internal_id=f"kc-{name}",
                owner_sub=_ADMIN,
                revoked=False,
                created_by_sub=_ADMIN,
            )
        )
        await session.commit()
    return f"agent:{name}"


async def _seed_grant(
    sub: str,
    *,
    expires_at: datetime | None = None,
    op_pattern: str = "vault.*",
    verdict: str = "auto-execute",
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_A,
                principal_sub=sub,
                op_pattern=op_pattern,
                target_scope="*",
                verdict=verdict,
                created_by_sub=_ADMIN,
                expires_at=expires_at,
            )
        )
        await session.commit()


async def _grants(sub: str) -> list[AgentPermission]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AgentPermission).where(AgentPermission.principal_sub == sub)
        )
        return list(rows.scalars().all())


async def _principals() -> list[AgentPrincipal]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(AgentPrincipal))).scalars().all())


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(rows.scalars().all())


async def _register(starting: dict[str, Any] | None, name: str = "bot") -> None:
    factory, _kc = _mock_kc()
    payload = AgentPrincipalCreate(
        name=name,
        starting_grants=None if starting is None else StartingGrants.model_validate(starting),
    )
    with patch(_KC_PATH, factory):
        await AgentPrincipalService().register(_TENANT_A, _ADMIN, payload)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"set": "read-targets"},
        {"set": "read-targets", "targets": []},
        {"set": "read-targets", "targets": ["  "]},
        {"set": "none", "targets": ["vault-a"]},
        {"set": "read-everything", "targets": ["vault-a"]},
        {"set": "write-everything"},
        {"set": "none", "extra": 1},
        {"targets": ["vault-a"]},
        {"set": "read-targets", "targets": [f"t{i}" for i in range(101)]},
    ],
)
def test_bad_pick_is_refused_by_the_model(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StartingGrants.model_validate(raw)


# ---------------------------------------------------------------------------
# Register: the rows each set writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_writes_no_row() -> None:
    await _seed_tenants()
    await _register({"set": "none"})
    assert len(await _principals()) == 1
    assert await _grants("agent:bot") == []


@pytest.mark.asyncio
async def test_read_everything_writes_one_row() -> None:
    await _seed_tenants()
    await _register({"set": "read-everything"})
    rows = await _grants("agent:bot")
    assert [(r.op_pattern, r.target_scope, r.verdict, r.expires_at) for r in rows] == [
        ("*", "*", "auto-execute", None)
    ]
    assert rows[0].created_by_sub == _ADMIN
    assert rows[0].tenant_id == _TENANT_A


@pytest.mark.asyncio
async def test_read_targets_writes_one_row_per_target() -> None:
    await _seed_tenants()
    id_a = await _seed_target("vault-a", aliases=["va"])
    id_b = await _seed_target("vault-b", aliases=[])
    # "va" is an alias of vault-a: the same target twice gives one row.
    await _register({"set": "read-targets", "targets": ["vault-a", "vault-b", "va"]})
    rows = await _grants("agent:bot")
    assert sorted(r.target_scope for r in rows) == sorted([id_a, id_b])
    assert {(r.op_pattern, r.verdict, r.created_by_sub) for r in rows} == {
        ("*", "auto-execute", _ADMIN)
    }


@pytest.mark.parametrize(
    ("targets", "code"),
    [
        (["vault-a", "no-such-target"], "starting_grants_target_unknown"),
        (["vault-other-tenant"], "starting_grants_target_unknown"),
        (["shared-alias"], "starting_grants_target_ambiguous"),
    ],
)
@pytest.mark.asyncio
async def test_read_targets_with_a_bad_name_writes_nothing(targets: list[str], code: str) -> None:
    await _seed_tenants()
    await _seed_target("vault-a", aliases=["shared-alias"])
    await _seed_target("vault-b", aliases=["shared-alias"])
    await _seed_target("vault-other-tenant", tenant_id=_TENANT_B, aliases=[])
    factory, kc = _mock_kc()
    payload = AgentPrincipalCreate(
        name="bot",
        starting_grants=StartingGrants.model_validate({"set": "read-targets", "targets": targets}),
    )
    with patch(_KC_PATH, factory), pytest.raises(StartingGrantsError) as excinfo:
        await AgentPrincipalService().register(_TENANT_A, _ADMIN, payload)
    assert excinfo.value.code == code
    # No login was made, no row was written.
    kc.create_client.assert_not_awaited()
    assert await _principals() == []
    assert await _grants("agent:bot") == []


@pytest.mark.asyncio
async def test_failed_grant_write_rolls_back_the_principal_and_the_login() -> None:
    await _seed_tenants()
    factory, kc = _mock_kc()
    payload = AgentPrincipalCreate(
        name="bot",
        starting_grants=StartingGrants.model_validate({"set": "read-everything"}),
    )
    failing = AsyncMock(side_effect=RuntimeError("grant write failed"))
    with (
        patch(_KC_PATH, factory),
        patch("meho_backplane.auth.agent_principals.write_starting_grants", failing),
        pytest.raises(RuntimeError),
    ):
        await AgentPrincipalService().register(_TENANT_A, _ADMIN, payload)
    kc.delete_client.assert_awaited_once_with("kc-internal-1")
    assert await _principals() == []


@pytest.mark.asyncio
async def test_starting_grant_is_an_ordinary_grant() -> None:
    """It shows in the grant list and revokes like any other grant."""
    await _seed_tenants()
    await _register({"set": "read-everything"})
    service = AgentGrantService()
    listed = await service.list_(_TENANT_A, principal_sub="agent:bot")
    assert [(g.principal_name, g.created_by_sub) for g in listed] == [("bot", _ADMIN)]
    assert await service.revoke(_TENANT_A, listed[0].id) is True
    assert await _grants("agent:bot") == []


# ---------------------------------------------------------------------------
# Register: the switch, over REST, with the audit row
# ---------------------------------------------------------------------------


def _post(
    client: TestClient,
    path: str,
    body: dict[str, Any],
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    kind: str | None = None,
) -> Any:
    key = make_rsa_keypair("kid-starting")
    token = mint_token(
        key, sub=_ADMIN, tenant_role=role.value, tenant_id=str(_TENANT_A), principal_kind=kind
    )
    factory, _kc = _mock_kc()
    with patch(_KC_PATH, factory), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("mode", ["on", "observe"])
@pytest.mark.asyncio
async def test_register_without_pick_is_refused_when_switch_is_not_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    await _seed_tenants()
    _switch(monkeypatch, mode)
    resp = _post(client, "/api/v1/agent-principals", {"name": "bot"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("starting_grants_required:")
    for choice in ("'none'", "'read-everything'", "'read-targets'"):
        assert choice in detail
    assert await _principals() == []
    audit = (await _audit_rows())[-1]
    assert audit.payload["starting_grants"] == "not-chosen"


@pytest.mark.asyncio
async def test_register_without_pick_works_as_before_when_switch_is_off(
    client: TestClient,
) -> None:
    await _seed_tenants()
    resp = _post(client, "/api/v1/agent-principals", {"name": "bot"})
    assert resp.status_code == 201, resp.text
    assert await _grants("agent:bot") == []
    audit = (await _audit_rows())[-1]
    assert audit.payload["op_id"] == "agent_principal.register"
    assert audit.payload["starting_grants"] == "not-chosen"


@pytest.mark.asyncio
async def test_register_audit_row_names_the_set_and_targets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    target_id = await _seed_target("vault-a", aliases=[])
    _switch(monkeypatch, "on")
    resp = _post(
        client,
        "/api/v1/agent-principals",
        {"name": "bot", "starting_grants": {"set": "read-targets", "targets": ["vault-a"]}},
    )
    assert resp.status_code == 201, resp.text
    assert [g.target_scope for g in await _grants("agent:bot")] == [target_id]
    audit = (await _audit_rows())[-1]
    assert audit.payload["starting_grants"] == "read-targets"
    assert audit.payload["starting_grant_targets"] == ["vault-a"]
    # The audit row is about the agent, not about the target.
    assert audit.target_id is None


@pytest.mark.asyncio
async def test_register_unknown_target_over_rest_is_422(
    client: TestClient,
) -> None:
    await _seed_tenants()
    resp = _post(
        client,
        "/api/v1/agent-principals",
        {"name": "bot", "starting_grants": {"set": "read-targets", "targets": ["nope"]}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("starting_grants_target_unknown:")
    assert await _principals() == []


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------


def _definition(identity_ref: str, starting: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "triage",
        "identity_ref": identity_ref,
        "model_tier": "standard",
        "system_prompt": "You triage incidents.",
        "turn_budget": 5,
    }
    if starting is not None:
        body["starting_grants"] = starting
    return body


async def _definitions() -> list[AgentDefinition]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(AgentDefinition))).scalars().all())


@pytest.mark.asyncio
async def test_definition_for_agent_without_grant_needs_the_pick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    # An expired grant is not a live grant.
    await _seed_grant(sub, expires_at=datetime.now(UTC) - timedelta(hours=1))
    _switch(monkeypatch, "on")
    resp = _post(client, "/api/v1/agents", _definition(sub))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("starting_grants_required:")
    assert await _definitions() == []


@pytest.mark.asyncio
async def test_definition_pick_writes_grants_to_its_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    resp = _post(client, "/api/v1/agents", _definition(sub, {"set": "read-everything"}))
    assert resp.status_code == 201, resp.text
    rows = await _grants(sub)
    assert [(r.op_pattern, r.target_scope, r.created_by_sub) for r in rows] == [("*", "*", _ADMIN)]
    audit = (await _audit_rows())[-1]
    assert audit.payload["op_id"] == "agent.create"
    assert audit.payload["starting_grants"] == "read-everything"


@pytest.mark.asyncio
async def test_definition_for_agent_with_grant_needs_no_pick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _seed_grant(sub)
    _switch(monkeypatch, "on")
    resp = _post(client, "/api/v1/agents", _definition(sub))
    assert resp.status_code == 201, resp.text
    assert len(await _grants(sub)) == 1
    assert (await _audit_rows())[-1].payload["starting_grants"] == "not-chosen"


@pytest.mark.asyncio
async def test_definition_pick_skips_a_grant_the_agent_already_has() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _register_definition_grant(sub, "first")
    await _register_definition_grant(sub, "second")
    assert len(await _grants(sub)) == 1


async def _register_definition_grant(sub: str, name: str) -> None:
    body = _definition(sub, {"set": "read-everything"}) | {"name": name}
    await AgentDefinitionService().create(
        _TENANT_A, _ADMIN, AgentDefinitionCreate.model_validate(body)
    )


@pytest.mark.asyncio
async def test_definition_pick_renews_an_expired_grant() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _seed_grant(sub, op_pattern="*", expires_at=datetime.now(UTC) - timedelta(hours=1))
    await _register_definition_grant(sub, "first")
    [row] = await _grants(sub)
    assert (row.op_pattern, row.target_scope, row.verdict) == ("*", "*", "auto-execute")
    assert row.expires_at is None


@pytest.mark.parametrize("expires_at", [None, datetime.now(UTC) - timedelta(hours=1)])
@pytest.mark.asyncio
async def test_definition_pick_keeps_a_deny_grant(expires_at: datetime | None) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    await _seed_grant(sub, op_pattern="*", verdict="deny", expires_at=expires_at)
    await _register_definition_grant(sub, "first")
    [row] = await _grants(sub)
    assert row.verdict == "deny"


@pytest.mark.asyncio
async def test_definition_unknown_target_writes_nothing() -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    payload = AgentDefinitionCreate.model_validate(
        _definition(sub, {"set": "read-targets", "targets": ["nope"]})
    )
    with pytest.raises(StartingGrantsError):
        await AgentDefinitionService().create(_TENANT_A, _ADMIN, payload)
    assert await _definitions() == []
    assert await _grants(sub) == []


# ---------------------------------------------------------------------------
# Admin MCP tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_register_takes_the_pick() -> None:
    await _seed_tenants()
    factory, _kc = _mock_kc()
    with patch(_KC_PATH, factory):
        await _register_handler(
            _operator(), {"name": "bot", "starting_grants": {"set": "read-everything"}}
        )
    assert len(await _grants("agent:bot")) == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"name": "bot"}, "starting_grants_required"),
        ({"name": "bot", "starting_grants": {"set": "read-targets"}}, "invalid starting_grants"),
        ({"name": "bot", "starting_grants": "read-everything"}, "invalid starting_grants"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_register_refuses_a_missing_or_bad_pick(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], expected: str
) -> None:
    await _seed_tenants()
    _switch(monkeypatch, "on")
    factory, kc = _mock_kc()
    with patch(_KC_PATH, factory), pytest.raises(McpInvalidParamsError, match=expected):
        await _register_handler(_operator(), arguments)
    kc.create_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_definition_create_needs_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    with pytest.raises(McpInvalidParamsError, match="starting_grants_required"):
        await _create_handler(_operator(), _definition(sub))
    result = await _create_handler(_operator(), _definition(sub, {"set": "none"}))
    assert result["name"] == "triage"
    assert await _grants(sub) == []


_PICK_TOOLS = [
    ("meho_agent_principals_register", mcp_agent_principals),
    ("meho_agents_create", mcp_agents),
]


def _registered(tool: str, module: Any) -> ToolDefinition:
    # Another test file may have emptied the tool registry.
    if get_tool(tool) is None:
        importlib.reload(module)
    entry = get_tool(tool)
    assert entry is not None
    return entry[0]


@pytest.mark.parametrize(("tool", "module"), _PICK_TOOLS)
def test_mcp_tools_are_unchanged_while_the_switch_is_off(tool: str, module: Any) -> None:
    defn = _registered(tool, module)
    assert "starting_grants" not in defn.inputSchema["properties"]
    assert "starting_grants" not in defn.description
    assert "AGENT_GRANT_REQUIRED" not in defn.description
    assert tool_for_settings(defn) is defn


@pytest.mark.parametrize("mode", ["observe", "on"])
@pytest.mark.parametrize(("tool", "module"), _PICK_TOOLS)
def test_mcp_tools_show_the_pick_while_the_switch_is_not_off(
    monkeypatch: pytest.MonkeyPatch, tool: str, module: Any, mode: str
) -> None:
    defn = _registered(tool, module)
    _switch(monkeypatch, mode)
    shown = tool_for_settings(defn)
    assert shown.inputSchema["properties"]["starting_grants"] == STARTING_GRANTS_JSON_SCHEMA
    assert shown.inputSchema["required"] == defn.inputSchema["required"]
    assert shown.description.startswith(defn.description)
    assert "starting_grants" in shown.description
    assert "starting_grants" not in defn.inputSchema["properties"]


@pytest.mark.asyncio
async def test_mcp_call_with_a_pick_is_refused_while_the_switch_is_off() -> None:
    _registered("meho_agent_principals_register", mcp_agent_principals)
    await _seed_tenants()
    factory, kc = _mock_kc()
    with (
        patch(_KC_PATH, factory),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
        pytest.raises(McpInvalidParamsError, match="starting_grants"),
    ):
        await handle_tools_call(
            _operator(),
            {
                "name": "meho_agent_principals_register",
                "arguments": {"name": "bot", "starting_grants": {"set": "read-everything"}},
            },
        )
    kc.create_client.assert_not_awaited()
    assert await _grants("agent:bot") == []


# ---------------------------------------------------------------------------
# Who may send a pick
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("service", "human_principal_required:"),
        # An agent login acts as at most operator, so the role check refuses it first.
        ("agent", "insufficient_role"),
    ],
)
@pytest.mark.asyncio
async def test_definition_pick_from_a_machine_login_is_refused(
    client: TestClient, kind: str, detail: str
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    resp = _post(client, "/api/v1/agents", _definition(sub, {"set": "read-everything"}), kind=kind)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"].startswith(detail)
    assert await _definitions() == []
    assert await _grants(sub) == []


@pytest.mark.asyncio
async def test_definition_with_set_none_from_a_service_login_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    resp = _post(client, "/api/v1/agents", _definition(sub, {"set": "none"}), kind="service")
    assert resp.status_code == 201, resp.text
    assert await _grants(sub) == []


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE, PrincipalKind.AGENT])
@pytest.mark.asyncio
async def test_mcp_definition_pick_from_a_machine_login_is_refused(kind: PrincipalKind) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    with pytest.raises(McpPermissionDeniedError, match="human_principal_required"):
        await _create_handler(_operator(kind=kind), _definition(sub, {"set": "read-everything"}))
    assert await _definitions() == []
    assert await _grants(sub) == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/agent-principals",
            {"name": "bot", "starting_grants": {"set": "read-everything"}},
        ),
        ("/api/v1/agents", _definition("agent:triage-bot", {"set": "read-everything"})),
    ],
)
@pytest.mark.asyncio
async def test_pick_from_a_caller_without_tenant_admin_is_refused(
    client: TestClient, path: str, body: dict[str, Any]
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    resp = _post(client, path, body, role=TenantRole.OPERATOR)
    assert resp.status_code == 403, resp.text
    assert [p.name for p in await _principals()] == ["triage-bot"]
    assert await _definitions() == []
    assert await _grants(sub) == []
    assert await _grants("agent:bot") == []


@pytest.mark.parametrize(
    ("tool", "module", "arguments"),
    [
        (
            "meho_agent_principals_register",
            mcp_agent_principals,
            {"name": "bot", "starting_grants": {"set": "read-everything"}},
        ),
        (
            "meho_agents_create",
            mcp_agents,
            _definition("agent:triage-bot", {"set": "read-everything"}),
        ),
    ],
)
@pytest.mark.asyncio
async def test_mcp_pick_from_a_caller_without_tenant_admin_is_refused(
    tool: str, module: Any, arguments: dict[str, Any]
) -> None:
    # Another test file may have emptied the tool registry.
    if get_tool(tool) is None:
        importlib.reload(module)
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    factory, kc = _mock_kc()
    with (
        patch(_KC_PATH, factory),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
        pytest.raises(McpInvalidParamsError, match="forbidden"),
    ):
        await handle_tools_call(
            _operator(role=TenantRole.OPERATOR), {"name": tool, "arguments": arguments}
        )
    kc.create_client.assert_not_awaited()
    assert await _definitions() == []
    assert await _grants(sub) == []
    assert await _grants("agent:bot") == []
