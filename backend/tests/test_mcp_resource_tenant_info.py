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

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


def test_resources_templates_list_exposes_tenant_info(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #5: ``resources/templates/list`` returns the registered template."""
    client, _op = client_with_operator

    response = post_mcp(
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

    response = post_mcp(
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

    response = post_mcp(
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

    response = post_mcp(
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
