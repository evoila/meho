# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``create_doc_collections`` registry-create MCP tool (#1739).

Covers the write half added to the doc-collections MCP surface, mirroring
the ``list_doc_collections`` test harness:

* **Registration gates** — the tool declares ``tenant_admin`` role,
  ``write`` op_class, and the ``meho-docs`` capability (parity with the
  REST create route + the ``meho.connector.*`` write-tool precedent).
* **Capability gate** — absent from ``tools/list`` for an unprovisioned
  tenant; a direct ``tools/call`` 403s before the handler runs.
* **Role gate** — a provisioned plain OPERATOR (not tenant_admin) is 403'd.
* **Happy path** — a valid create returns the full collection with
  ``status="provisioning"`` and a server-generated id; the row lands on the
  operator's tenant.
* **Unknown backend type → INVALID_PARAMS** with the registered set in
  ``error.data`` (the MCP analogue of the REST 422).
* **Duplicate key → INVALID_PARAMS** (the MCP analogue of the REST 409).
* **Audit** — one ``audit_log`` row with
  ``op_id="meho.docs.collections.create"`` / ``op_class="write"``.
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
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
_CREATE_TOOL = "create_doc_collections"


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
    """``TestClient`` whose operator's role + capability set are parametrised.

    Default is a tenant_admin with no capability (so the capability gate is
    exercisable); provision with
    ``@pytest.mark.parametrize("admin_client", [(role, frozenset({...}))], indirect=True)``.
    """
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


def _valid_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "collection_key": "vmware",
        "vendor": "VMware by Broadcom",
        "products": ["vsphere", "nsx"],
        "backend": {"type": "corpus-http", "ref": {"endpoint": "https://corpus.test/v1/search"}},
    }
    args.update(overrides)
    return args


def _call_create(client: TestClient, arguments: dict[str, Any]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _CREATE_TOOL, "arguments": arguments},
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
    entry = get_tool(_CREATE_TOOL)
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
    assert _CREATE_TOOL in tools_by_name
    schema = tools_by_name[_CREATE_TOOL]["inputSchema"]
    assert schema["additionalProperties"] is False
    # tenant_id is NOT an argument — a cross-tenant create is impossible.
    assert "tenant_id" not in schema["properties"]
    assert set(schema["required"]) == {"collection_key", "vendor", "backend"}


def test_absent_from_tools_list_for_unprovisioned_admin(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client  # default: admin, no capability
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _CREATE_TOOL not in names


# ---------------------------------------------------------------------------
# Gates on tools/call
# ---------------------------------------------------------------------------


def test_tools_call_403_when_unprovisioned(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client
    body = _call_create(client, _valid_args())
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
    """The capability does not relax the role gate: OPERATOR cannot create."""
    client, _op = admin_client
    body = _call_create(client, _valid_args())
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
async def test_create_returns_full_collection_on_operators_tenant(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    body = _call_create(client, _valid_args())
    result = _payload(body)
    assert result["collection_key"] == "vmware"
    assert result["status"] == "provisioning"
    assert result["backend"]["type"] == "corpus-http"

    row = await _fetch_row("vmware")
    assert row.tenant_id == OPERATOR_TENANT_ID


# ---------------------------------------------------------------------------
# Validation + conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_unknown_backend_type_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
) -> None:
    client, _op = admin_client
    body = _call_create(client, _valid_args(backend={"type": "no-such-backend", "ref": {}}))
    assert body["error"]["code"] == INVALID_PARAMS
    data = body["error"]["data"]
    assert data["kind"] == "unknown_backend_type"
    assert "corpus-http" in data["valid_backend_types"]


@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
def test_duplicate_key_is_invalid_params(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    first = _call_create(client, _valid_args())
    assert first["result"]["isError"] is False, first
    second = _call_create(client, _valid_args())
    assert second["error"]["code"] == INVALID_PARAMS
    assert second["error"]["data"]["kind"] == "collection_conflict"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_client",
    [(TenantRole.TENANT_ADMIN, frozenset({_DOCS_CAPABILITY}))],
    indirect=True,
)
async def test_create_writes_audit_row_with_canonical_op_id(
    admin_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = admin_client
    body = _call_create(client, _valid_args())
    assert body["result"]["isError"] is False, body

    rows = await _mcp_audit_rows()
    create_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.create"]
    assert len(create_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert create_rows[0].payload["op_class"] == "write"
