# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``update_doc_collections`` registry-update MCP tool (#3601).

Covers the in-place repoint half of the doc-collections MCP surface,
mirroring the ``create_doc_collections`` / ``delete_doc_collections`` test
harness:

* **Registration gates** — ``tenant_admin`` role, ``write`` op_class, the
  ``meho-docs`` capability, and ``collection_key`` the only required arg.
* **Capability gate** — absent from ``tools/list`` for an unprovisioned
  tenant.
* **Role gate** — a provisioned plain OPERATOR is 403'd.
* **Happy path** — a backend repoint returns the full collection with
  ``status="provisioning"`` and the new backend; the row is updated.
* **Unknown backend type → INVALID_PARAMS** (the MCP analogue of REST 422).
* **Global-row platform seat** — updating a global (platform-owned) row
  without ``platform_admin`` → INVALID_PARAMS
  (``global_collection_update_forbidden``), the MCP analogue of REST 403.
* **Audit** — one ``audit_log`` row with
  ``op_id="meho.docs.collections.update"`` / ``op_class="write"``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
_UPDATE_TOOL = "update_doc_collections"
_CORPUS_URL = "https://corpus.test/v1/search"
_NEW_CORPUS_URL = "https://corpus-new.test/v1/search"


def _operator(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    capabilities: frozenset[str] = frozenset(),
    scopes: frozenset[str] = frozenset({"mcp:admin"}),
) -> Operator:
    return Operator(
        sub="admin-test",
        name="Admin",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
        scopes=scopes,
    )


@pytest.fixture
def admin_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
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


async def _seed_collection(**overrides: Any) -> DocCollectionORM:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": OPERATOR_TENANT_ID,
        "collection_key": "vmware",
        "vendor": "VMware",
        "products": ["vsphere"],
        "description": None,
        "when_to_use": None,
        "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
        "status": "ready",
        "last_ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
        "doc_count": 7,
        "readiness": {"reachable": True, "index_built": True},
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    row = DocCollectionORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(row)
        await session.commit()
    return row


def _call_update(client: TestClient, arguments: dict[str, Any]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _UPDATE_TOOL, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


async def _fetch_row(collection_key: str) -> DocCollectionORM:
    sm = get_sessionmaker()
    async with sm() as session:
        return (
            await session.execute(
                select(DocCollectionORM).where(DocCollectionORM.collection_key == collection_key)
            )
        ).scalar_one()


async def _mcp_audit_rows() -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in result.scalars().all() if row.method == "MCP"]


# ---------------------------------------------------------------------------
# Registration gates
# ---------------------------------------------------------------------------


def test_registered_definition_is_admin_write_capability_gated() -> None:
    entry = get_tool(_UPDATE_TOOL)
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
    assert _UPDATE_TOOL in tools_by_name
    schema = tools_by_name[_UPDATE_TOOL]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "tenant_id" not in schema["properties"]
    assert schema["required"] == ["collection_key"]


def test_absent_from_tools_list_for_unprovisioned_admin(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client  # default: admin, no capability
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _UPDATE_TOOL not in names


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.OPERATOR, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_tools_call_403_for_plain_operator(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client
    body = _call_update(client, {"collection_key": "vmware", "description": "x"})
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Happy path + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_repoint_returns_full_collection_and_updates_row(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await _seed_collection(status="ready")
    body = _call_update(
        client,
        {
            "collection_key": "vmware",
            "backend": {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}},
        },
    )
    result = _payload(body)
    assert result["backend"]["ref"] == {"endpoint": _NEW_CORPUS_URL}
    assert result["status"] == "provisioning"

    row = await _fetch_row("vmware")
    assert row.backend["ref"] == {"endpoint": _NEW_CORPUS_URL}
    assert row.status == "provisioning"
    assert row.readiness is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_unknown_backend_type_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await _seed_collection()
    body = _call_update(
        client,
        {"collection_key": "vmware", "backend": {"type": "no-such-backend", "ref": {}}},
    )
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["kind"] == "unknown_backend_type"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_global_row_without_platform_admin_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """A global (platform-owned) row is refused without platform_admin (MCP analogue of 403)."""
    client, _op = admin_client
    await _seed_collection(tenant_id=None, status="ready")
    body = _call_update(
        client,
        {
            "collection_key": "vmware",
            "backend": {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}},
        },
    )
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["error"] == "global_collection_update_forbidden"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_update_writes_audit_row_with_canonical_op_id(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    await _seed_collection()
    body = _call_update(client, {"collection_key": "vmware", "description": "updated"})
    assert body["result"]["isError"] is False, body

    rows = await _mcp_audit_rows()
    update_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.update"]
    assert len(update_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert update_rows[0].payload["op_class"] == "write"
