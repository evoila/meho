# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for an MCP session without a current tenant (#5239).

A login with several tenants that sends no ``Meho-Tenant`` header may only
start the connection and call ``meho_status``, which lists the person's
tenants and roles. Everything else is refused with ``tenant_not_chosen``.
The allowed requests do no tenant work: no session cap, no tenant
preamble, no audit row, no broadcast setting, nothing bound.
The ``Meho-Tenant`` header on the MCP transport is tested end to end.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import structlog
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership, TenantRole
from meho_backplane.conventions import preamble as preamble_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.mcp import handlers as mcp_handlers
from meho_backplane.mcp import server as mcp_server
from meho_backplane.mcp.auth import mcp_resource_uri, verify_mcp_jwt, verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_REQUEST, PROTOCOL_VERSION
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.settings import get_settings
from tests.conftest import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from tests.mcp_test_fixtures import (
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_ENTRIES = [f"{_TENANT_A}:lab-a:Readers", f"{_TENANT_B}:lab-b:Operators"]


def _unchosen_operator() -> Operator:
    return Operator(
        sub="person-1",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.READ_ONLY,
        tenants=(
            TenantMembership(tenant_id=_TENANT_A, name="lab-a", group=RoleGroup.READERS),
            TenantMembership(tenant_id=_TENANT_B, name="lab-b", group=RoleGroup.OPERATORS),
        ),
        tenant_chosen=False,
    )


@pytest.fixture
def unchosen_client() -> Iterator[TestClient]:
    operator = _unchosen_operator()

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call(client: TestClient, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def test_tools_list_shows_only_meho_status(unchosen_client: TestClient) -> None:
    response = post_mcp(unchosen_client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["meho_status"]


def test_meho_status_lists_the_tenants_and_roles(unchosen_client: TestClient) -> None:
    response = _call(unchosen_client, "meho_status")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["operator"]["tenant_id"] is None
    assert payload["operator"]["tenant_role"] is None
    assert [(t["name"], t["tenant_role"]) for t in payload["operator"]["tenants"]] == [
        ("lab-a", "read_only"),
        ("lab-b", "operator"),
    ]
    assert payload["vault"]["detail"] == "tenant_not_chosen"


@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_targets", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"arguments": {}}},
    ],
)
def test_other_requests_are_refused(unchosen_client: TestClient, body: dict[str, Any]) -> None:
    response = post_mcp(unchosen_client, body)
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == INVALID_REQUEST
    assert error["message"].startswith("tenant_not_chosen:")
    assert "lab-a, lab-b" in error["message"]


def test_connection_start_is_allowed(unchosen_client: TestClient) -> None:
    ping = post_mcp(unchosen_client, {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert ping.status_code == 200
    assert "error" not in ping.json()
    note = post_mcp(unchosen_client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202


@pytest.fixture
def no_tenant_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test when a request without a tenant does tenant work."""

    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("tenant work without a current tenant")

    monkeypatch.setattr(mcp_server, "resolve_mcp_session_cap", _fail)
    monkeypatch.setattr(mcp_server, "check_mcp_session_cap", _fail)
    monkeypatch.setattr(preamble_module, "assemble_preamble_detailed", _fail)
    monkeypatch.setattr(mcp_handlers, "compute_effective_broadcast_detail", _fail)
    monkeypatch.setattr(mcp_handlers, "write_mcp_audit_row", _fail)


async def _audit_rows_without_boot_stamp() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
        )
        return list(rows.scalars().all())


@pytest.mark.usefixtures("no_tenant_work")
def test_initialize_without_a_tenant_skips_the_cap_and_the_preamble(
    unchosen_client: TestClient,
) -> None:
    response = post_mcp(
        unchosen_client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tenant-gate-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["serverInfo"]["name"]
    assert result["instructions"].startswith("tenant_not_chosen:")
    assert "lab-a, lab-b" in result["instructions"]
    assert "meho_status" in result["instructions"]
    assert "Meho-Tenant" in result["instructions"]


@pytest.mark.usefixtures("no_tenant_work")
async def test_meho_status_without_a_tenant_writes_no_audit_row(
    unchosen_client: TestClient,
) -> None:
    """With the default audit settings the wrapper row would name the placeholder tenant."""
    assert get_settings().audit_wrapper_rows_retired is False
    response = _call(unchosen_client, "meho_status")
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert await _audit_rows_without_boot_stamp() == []


async def test_bind_step_binds_nothing_without_a_tenant() -> None:
    structlog.contextvars.clear_contextvars()
    operator = await verify_mcp_jwt_and_bind(_unchosen_operator())
    assert operator.tenant_chosen is False
    assert structlog.contextvars.get_contextvars() == {}


# ---------------------------------------------------------------------------
# The Meho-Tenant header on the MCP transport
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_token() -> Iterator[str]:
    key = make_rsa_keypair("kid-mcp-tenant")
    token = mint_token(
        key,
        sub="person-1",
        audience=mcp_resource_uri(),
        extra_claims={"meho_tenants": _ENTRIES},
    )
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        yield token


async def test_mcp_header_picks_the_tenant(mcp_token: str) -> None:
    operator = await verify_mcp_jwt(f"Bearer {mcp_token}", "lab-b")
    assert operator.tenant_id == _TENANT_B
    assert operator.tenant_role is TenantRole.OPERATOR
    assert operator.tenant_chosen is True


async def test_mcp_without_header_has_no_current_tenant(mcp_token: str) -> None:
    operator = await verify_mcp_jwt(f"Bearer {mcp_token}", None)
    assert operator.tenant_chosen is False


async def test_mcp_header_for_another_tenant_is_403(mcp_token: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await verify_mcp_jwt(f"Bearer {mcp_token}", str(uuid.uuid4()))
    assert exc.value.status_code == 403
    assert str(exc.value.detail).startswith("tenant_not_member:")
    assert "WWW-Authenticate" not in (exc.value.headers or {})
