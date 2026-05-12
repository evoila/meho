# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://tenant/{tenant_id}/info`` reference MCP resource (G0.5-T4, #249).

Covers acceptance criteria 5-8 on issue #249:

* ``resources/templates/list`` returns the tenant-info template entry.
  (The AC text says ``resources/list``, but per T3's spec-correctness
  decision templated resources surface via ``resources/templates/list``;
  concrete ``resources/list`` is empty in v0.2.)
* ``resources/read`` with the operator's own tenant_id returns
  ``{id, slug, name, operator_role}``.
* ``resources/read`` with a *different* tenant's id returns
  ``INVALID_PARAMS`` (-32602). The AC text says HTTP 403, but the JSON-
  RPC transport carries error codes, not HTTP statuses — every input-
  validation failure including tenant-boundary breach maps to -32602.
* ``resources/read`` with a non-UUID id returns INVALID_PARAMS (-32602).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import clear_registries
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.settings import get_settings

_OPERATOR_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a0")


def _operator(role: TenantRole = TenantRole.READ_ONLY) -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_OPERATOR_TENANT_ID,
        tenant_role=role,
    )


@pytest.fixture(autouse=True)
def _isolated_registry_with_production_resource() -> Iterator[None]:
    """Reset the registry then re-register the production ``tenant_info`` resource.

    Same rationale as :mod:`tests.test_mcp_tool_meho_status` — Python's
    import cache prevents the lifespan's
    :func:`eager_import_mcp_modules` from re-running top-level
    registrations after the first test. :func:`importlib.reload` forces
    the module body to re-execute so each test starts from a known
    state regardless of cross-file ordering.
    """
    from meho_backplane.mcp.resources import tenant_info

    clear_registries()
    importlib.reload(tenant_info)
    yield
    clear_registries()


@pytest.fixture
def client_with_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Operator]]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()

    op = _operator(TenantRole.READ_ONLY)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        get_settings.cache_clear()


@pytest.fixture
async def seeded_operator_tenant() -> None:
    """Insert a :class:`Tenant` row matching the fixture operator's ``tenant_id``.

    The conftest autouse fixture runs ``alembic upgrade head`` to materialise
    the ``tenant`` table; this fixture populates the operator's row so the
    handler's ``session.execute(select(Tenant).where(...))`` returns a real
    record rather than ``None``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            Tenant(
                id=_OPERATOR_TENANT_ID,
                slug="op-test-tenant",
                name="Operator Test Tenant",
            ),
        )


def _post_mcp(client: TestClient, body: Any) -> Any:
    return client.post("/mcp", json=body)


def test_resources_templates_list_exposes_tenant_info(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #5: ``resources/templates/list`` returns the registered template."""
    client, _op = client_with_operator

    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )

    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    tenant_info = [t for t in templates if t["uriTemplate"] == "meho://tenant/{tenant_id}/info"]
    assert len(tenant_info) == 1
    assert tenant_info[0]["mimeType"] == "application/json"
    # MEHO-internal RBAC field stripped from the wire shape.
    assert "required_role" not in tenant_info[0]


@pytest.mark.asyncio
async def test_resources_read_own_tenant_returns_identity_bundle(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC #6: reading the operator's own tenant returns {id, slug, name, role}."""
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/info"

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )

    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "application/json"

    bundle = json.loads(contents[0]["text"])
    assert bundle["id"] == str(op.tenant_id)
    assert bundle["slug"] == "op-test-tenant"
    assert bundle["name"] == "Operator Test Tenant"
    assert bundle["operator_role"] == TenantRole.READ_ONLY.value


def test_resources_read_cross_tenant_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #7: a URI bound to a different tenant rejects with -32602.

    The tenant-boundary check runs *before* the DB query, so the test
    doesn't need to seed the foreign tenant — the rejection happens
    purely from the operator vs. URI mismatch.
    """
    client, _op = client_with_operator
    foreign_tenant = uuid4()
    uri = f"meho://tenant/{foreign_tenant}/info"

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "cross-tenant" in body["error"]["message"].lower()


def test_resources_read_invalid_uuid_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #8: a non-UUID bound to {tenant_id} rejects with -32602."""
    client, _op = client_with_operator
    # The URI must still match the template's path shape for the matcher
    # to bind `tenant_id`; the bound value is then validated as a UUID
    # inside the handler.
    uri = "meho://tenant/not-a-uuid/info"

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not a uuid" in body["error"]["message"].lower()
