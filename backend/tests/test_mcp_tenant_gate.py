# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for an MCP session without a current tenant (#5239).

A login with several tenants that sends no ``Meho-Tenant`` header may only
start the connection and call ``meho_status``, which lists the person's
tenants and roles. Everything else is refused with ``tenant_not_chosen``.
The ``Meho-Tenant`` header on the MCP transport is tested end to end.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import mcp_resource_uri, verify_mcp_jwt, verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_REQUEST
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
