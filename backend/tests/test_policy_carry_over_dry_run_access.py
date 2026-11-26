# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may run the dry run of the "always wait" carry-over, and what it writes.

* An agent login gets 403, also one with the platform admin mark. Every
  other caller gets the same status as ``GET /api/v1/policy/export`` with
  the same scope. A hosted run has no MCP tool and no operation for it.
* It writes only its audit row: every table is the same before and after,
  with every policy switch off and on, also when the dry run fails half
  way.
* An override with approval on keeps waiting: the override alone makes a
  ``safe`` call wait, before and after a dry run.

Drives the production ``meho_backplane.main:app`` with real tokens. DB is
the autouse per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.policy.apply import register_policy_apply_operation
from meho_backplane.connectors.policy.classify import register_policy_classify_operation
from meho_backplane.connectors.policy.preview import register_policy_classify_preview_operation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, Base, EndpointDescriptor
from meho_backplane.main import app
from meho_backplane.mcp.handlers import handle_tools_list
from meho_backplane.mcp.registry import registered_tool_names
from meho_backplane.policy import carry_over
from meho_backplane.settings import get_settings

from ._carry_over_helpers import (
    AUDIT_OP,
    CONNECTOR,
    EXPORT_ROUTE,
    OTHER_TENANT,
    ROUTE,
    TENANT,
    add_steps,
    audit_rows,
    call,
    dry_run,
    get,
    override,
    policy,
    register,
    setup,
)
from ._oidc_jwt_helpers import make_rsa_keypair
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_SWITCHES = ("POLICY_CLASSIFICATION", "POLICY_ENGINE_SHADOW", "POLICY_APPROVER_ROUTING")
#: Tables a dry run may change, and why.
_MAY_CHANGE = {
    # One row per call: the dry run's own audit row.
    "audit_log",
    # The first call of a tenant's token adds the tenant's row.
    "tenant",
    # The first call of a service login notes the login.
    "machine_login",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _switches(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    for name in _SWITCHES:
        monkeypatch.setenv(name, "true" if on else "false")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Who may run it (whole path: token, gate, audit row)
# ---------------------------------------------------------------------------

_RUNNER = str(uuid.uuid4())
_AGENT = {"principal_kind": "agent"}
_SERVICE = {"principal_kind": "service"}
_OPERATOR = TenantRole.OPERATOR.value
_READER = TenantRole.READ_ONLY.value

#: (caller, token claims or None for no token, status for tenant, status for install)
_CALLERS: list[tuple[str, dict[str, Any] | None, int, int]] = [
    ("person, tenant admin", {}, 200, 403),
    ("person, platform admin, tenant admin", {"platform_admin": True}, 200, 200),
    ("person, platform admin, reader", {"platform_admin": True, "tenant_role": _READER}, 200, 200),
    ("person, operator", {"tenant_role": _OPERATOR}, 403, 403),
    ("person, reader", {"tenant_role": _READER}, 403, 403),
    ("agent, operator with a grant", {**_AGENT, "tenant_role": _OPERATOR}, 403, 403),
    ("agent, reader", {**_AGENT, "tenant_role": _READER}, 403, 403),
    ("agent, tenant admin", dict(_AGENT), 403, 403),
    ("agent, platform admin", {**_AGENT, "platform_admin": True}, 403, 403),
    ("service, tenant admin", dict(_SERVICE), 200, 403),
    ("service, platform admin", {**_SERVICE, "platform_admin": True}, 200, 403),
    (
        "runner",
        {"principal_kind": "runner", "runner_id": _RUNNER, "tenant_role": _READER},
        403,
        403,
    ),
    ("no token", None, 401, 401),
]


def _send(
    client: TestClient, key: Any, route: str, scope: str, claims: dict[str, Any] | None
) -> int:
    if claims is None:
        return client.get(route, params={"scope": scope}).status_code
    sub = "caller-" + uuid.uuid5(uuid.NAMESPACE_OID, repr(sorted(claims.items()))).hex[:8]
    status: int = get(client, key, route, scope, sub=sub, **claims).status_code
    return status


@pytest.mark.asyncio
async def test_an_agent_is_refused_and_every_other_caller_gets_the_export_answer(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup(embedding)
    key = make_rsa_keypair("kid-co-callers")

    seen: list[tuple[str, str, int, int, int]] = []
    for caller, claims, tenant_status, install_status in _CALLERS:
        for scope, expected in (("tenant", tenant_status), ("install", install_status)):
            dry = _send(client, key, ROUTE, scope, claims)
            export = _send(client, key, EXPORT_ROUTE, scope, claims)
            seen.append((caller, scope, dry, export, expected))

    for caller, scope, dry, export, expected in seen:
        assert dry == expected, (caller, scope, dry)
        if claims_is_agent(caller):
            assert dry == 403, (caller, scope)
        else:
            assert dry == export, (caller, scope, dry, export)
    # The refusal of an agent is the dry run's own: the export lets this one read.
    assert ("agent, platform admin", "tenant", 403, 200, 403) in seen

    rows = await audit_rows()
    sent = [(dry, scope) for _, scope, dry, _, _ in seen if dry != 401]
    assert [row.status_code for row in rows] == [dry for dry, _ in sent]
    assert {row.method for row in rows} == {"GET"}
    named = [row for row in rows if (row.payload or {}).get("op_id") == AUDIT_OP]
    assert {row.status_code for row in named} >= {200, 403}
    assert len([dry for dry, _ in sent if dry == 200]) == 7


def claims_is_agent(caller: str) -> bool:
    return caller.startswith("agent")


@pytest.mark.asyncio
async def test_a_hosted_run_has_no_tool_and_no_operation_for_the_dry_run(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    await register_policy_apply_operation(embedding_service=embedding)
    await register_policy_classify_operation(embedding_service=embedding)
    await register_policy_classify_preview_operation(embedding_service=embedding)
    admin = Operator(
        sub="root-a",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
        scopes=frozenset({"mcp:admin"}),
    )

    listed = [tool["name"] for tool in (await handle_tools_list(admin, None))["tools"]]
    assert "meho_connector_list" in listed
    assert not [name for name in listed if "carry" in name]
    assert not [name for name in registered_tool_names() if "carry" in name]
    async with get_sessionmaker()() as session:
        op_ids = list((await session.execute(select(EndpointDescriptor.op_id))).scalars())
    assert "policy.apply" in op_ids
    assert not [op_id for op_id in op_ids if "carry" in op_id]


# ---------------------------------------------------------------------------
# It writes only its audit row
# ---------------------------------------------------------------------------


async def _tables() -> dict[str, list[str]]:
    """Every row of every table of the database, as text, sorted."""
    async with get_sessionmaker()() as session:
        conn = await session.connection()
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        tables: dict[str, list[str]] = {}
        for name in names:
            rows = await session.execute(text(f'SELECT * FROM "{name}"'))
            tables[name] = sorted(repr(tuple(row)) for row in rows)
        return tables


def _answers(client: TestClient, key: Any) -> list[Any]:
    calls = [
        dry_run(client, key),
        dry_run(client, key, sub="svc-a", principal_kind="service"),
        dry_run(client, key, "install", sub="root-a", platform_admin=True),
    ]
    assert [resp.status_code for resp in calls] == [200, 200, 200]
    return [resp.json() for resp in calls]


@pytest.mark.asyncio
async def test_the_dry_run_changes_no_row(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup(embedding)
    add_steps(monkeypatch, "cowapi.composite.rebuild", CONNECTOR, "cowapi.vm.update")
    await override("cowapi.vm.list", TENANT, approval=False)
    await override("cowapi.gone", TENANT, approval=True)
    await policy(
        uuid.UUID("00000000-0000-0000-0000-0000000c0c0c"),
        [{"match": {"op": "*"}, "always_wait": True}],
    )
    key = make_rsa_keypair("kid-co-nowrite")
    before = await _tables()
    audit_before = len(before["audit_log"])

    _switches(monkeypatch, on=False)
    off = _answers(client, key)
    assert _answers(client, key) == off
    _switches(monkeypatch, on=True)
    on = _answers(client, key)
    assert _answers(client, key) == on
    assert on == off

    after = await _tables()
    # Every table of the database: also those the models do not list.
    assert after.keys() == before.keys()
    assert len(before) >= len(Base.metadata.tables)
    changed = sorted(name for name in before if before[name] != after[name])
    assert set(changed) <= _MAY_CHANGE, changed
    added = [row for row in await audit_rows() if row.payload.get("op_id") == AUDIT_OP]
    assert len(after["audit_log"]) - audit_before == len(added) == 12
    assert {(row.payload.get("op_class"), row.status_code) for row in added} == {("read", 200)}
    assert OTHER_TENANT not in {row.tenant_id for row in added}


@pytest.mark.asyncio
async def test_a_dry_run_that_fails_half_way_writes_only_its_audit_row(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_vault(monkeypatch)
    await setup(embedding)
    key = make_rsa_keypair("kid-co-fail")
    assert dry_run(TestClient(app), key).status_code == 200
    before = await _tables()

    async def _broken(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("fixture: the read breaks after the overrides")

    monkeypatch.setattr(carry_over, "_file_settings", _broken)
    resp = dry_run(TestClient(app, raise_server_exceptions=False), key)
    assert resp.status_code == 500

    after = await _tables()
    assert sorted(name for name in before if before[name] != after[name]) == ["audit_log"]
    (failed,) = [row for row in await audit_rows() if row.status_code == 500]
    assert failed.payload.get("op_id") == AUDIT_OP
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1


# ---------------------------------------------------------------------------
# An override with approval on keeps waiting
# ---------------------------------------------------------------------------


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


@pytest.mark.asyncio
async def test_without_the_override_the_same_safe_call_runs(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    key = make_rsa_keypair("kid-co-runs")
    assert call(client, key, "cowapi.vm.list")["status"] == "ok"
    assert await _requests() == []


@pytest.mark.asyncio
async def test_an_override_with_approval_on_keeps_waiting(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await override("cowapi.vm.list", TENANT, approval=True)
    key = make_rsa_keypair("kid-co-wait")

    first = call(client, key, "cowapi.vm.list")
    assert dry_run(client, key).status_code == 200
    second = call(client, key, "cowapi.vm.list")

    assert first["status"] == second["status"] == "awaiting_approval"
    requests = await _requests()
    assert [r.op_id for r in requests] == ["cowapi.vm.list", "cowapi.vm.list"]
    assert {r.status for r in requests} == {"pending"}
    async with get_sessionmaker()() as session:
        calls = [
            row
            for row in (await session.execute(select(AuditLog))).scalars()
            if (row.payload or {}).get("op_id") == "cowapi.vm.list"
        ]
    assert len(calls) == 2
    assert {row.operator_sub for row in calls} == {"op-a"}
    assert {(row.path, row.status_code) for row in calls} == {("approval.request", 202)}
    assert {row.payload["approval_request_id"] for row in calls} == {str(r.id) for r in requests}
