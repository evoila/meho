# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``delete_doc_collections`` registry-delete MCP tool (#2487).

The delete half of the doc-collections MCP surface, mirroring the
``create_doc_collections`` test harness:

* **Registration gates** — the tool declares ``tenant_admin`` role,
  ``write`` op_class, and the ``meho-docs`` capability (parity with the
  REST delete route + the create tool).
* **Capability gate** — absent from ``tools/list`` for an unprovisioned
  tenant; a direct ``tools/call`` 403s before the handler runs.
* **Role gate** — a provisioned plain OPERATOR (not tenant_admin) is 403'd.
* **Happy path** — deleting a disabled, tenant-owned collection returns
  ``{collection_key}`` and removes the row.
* **Non-disabled → INVALID_PARAMS** with ``error.data.error =
  'collection_not_disabled'`` (the MCP analogue of the REST 409).
* **Global row → INVALID_PARAMS** with ``error.data.error =
  'global_collection'`` (the MCP analogue of the REST 403).
* **Unknown key → INVALID_PARAMS** with the ``known_keys`` hint (the MCP
  analogue of the REST 404).
* **Audit** — one ``audit_log`` row with
  ``op_id="meho.docs.collections.delete"`` / ``op_class="write"``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seed_doc_collection,
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
_DELETE_TOOL = "delete_doc_collections"


def _operator(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    return Operator(
        sub="admin-test",
        name="Admin",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
    )


@pytest.fixture
def admin_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` whose operator's role + capability set are parametrised."""
    param = getattr(request, "param", None)
    if param is None:
        role, capabilities = TenantRole.TENANT_ADMIN, frozenset()
    else:
        role, capabilities = param
    op = _operator(role=role, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call_delete(client: TestClient, collection_key: str) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _DELETE_TOOL, "arguments": {"collection_key": collection_key}},
        },
    )
    assert response.status_code == 200
    return response.json()


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


async def _rows_for_key(collection_key: str) -> list[DocCollectionORM]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(DocCollectionORM).where(DocCollectionORM.collection_key == collection_key)
        )
        return list(result.scalars().all())


async def _mcp_audit_rows() -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in result.scalars().all() if row.method == "MCP"]


# ---------------------------------------------------------------------------
# Registration gates
# ---------------------------------------------------------------------------


def test_registered_definition_is_admin_write_capability_gated() -> None:
    entry = get_tool(_DELETE_TOOL)
    assert entry is not None
    defn, _handler = entry
    assert defn.required_role == TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"
    assert defn.required_capability == _DOCS_CAPABILITY


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_present_with_strict_schema_for_provisioned_admin(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    assert _DELETE_TOOL in tools_by_name
    schema = tools_by_name[_DELETE_TOOL]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "tenant_id" not in schema["properties"]
    assert set(schema["required"]) == {"collection_key"}


def test_absent_from_tools_list_for_unprovisioned_admin(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client  # default: admin, no capability
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _DELETE_TOOL not in names


# ---------------------------------------------------------------------------
# Gates on tools/call
# ---------------------------------------------------------------------------


def test_tools_call_403_when_unprovisioned(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client
    body = _call_delete(client, "vmware")
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.OPERATOR, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_tools_call_403_for_plain_operator(
    admin_client: tuple[TestClient, Operator],
) -> None:
    """The capability does not relax the role gate: OPERATOR cannot delete."""
    client, _op = admin_client
    body = _call_delete(client, "vmware")
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Happy path + tenant scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_delete_removes_disabled_tenant_row(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await seed_doc_collection(
        collection_key="vmware", status="disabled", tenant_id=OPERATOR_TENANT_ID
    )
    body = _call_delete(client, "vmware")
    assert _payload(body) == {"collection_key": "vmware"}
    assert await _rows_for_key("vmware") == []


# ---------------------------------------------------------------------------
# Guard refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_non_disabled_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await seed_doc_collection(collection_key="vmware", status="ready", tenant_id=OPERATOR_TENANT_ID)
    body = _call_delete(client, "vmware")
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["error"] == "collection_not_disabled"
    assert body["error"]["data"]["status"] == "ready"
    # Untouched.
    assert len(await _rows_for_key("vmware")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_global_row_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    # tenant_id=None → a global (platform-owned) row.
    await seed_doc_collection(collection_key="vmware", status="disabled", tenant_id=None)
    body = _call_delete(client, "vmware")
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["error"] == "global_collection"
    assert len(await _rows_for_key("vmware")) == 1


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_unknown_key_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    body = _call_delete(client, "nope")
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["error"] == "no_doc_collection"
    assert "known_keys" in body["error"]["data"]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_delete_writes_audit_row_with_canonical_op_id(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await seed_doc_collection(
        collection_key="vmware", status="disabled", tenant_id=OPERATOR_TENANT_ID
    )
    body = _call_delete(client, "vmware")
    assert body["result"]["isError"] is False, body

    rows = await _mcp_audit_rows()
    delete_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.delete"]
    assert len(delete_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert delete_rows[0].payload["op_class"] == "write"
