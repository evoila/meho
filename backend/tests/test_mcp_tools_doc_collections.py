# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``list_doc_collections`` catalogue MCP tool (G4.6-T4 #1553).

Covers the catalogue-discovery contract:

* ``list_doc_collections`` is registered with
  ``required_capability="meho-docs"``: **absent** from ``tools/list`` for a
  tenant without the base capability, **present** for one with it.
* ``inputSchema`` is strict (``additionalProperties: false``; optional
  ``vendor`` / ``cursor`` / ``limit``).
* ``tools/call`` from a tenant lacking the base ``meho-docs`` capability →
  403-class error (the handler never runs).
* **Per-collection entitlement filter:** a provisioned tenant sees only
  the collections it holds ``meho-docs:<key>`` for — a visible-but-not-
  entitled collection is dropped from the catalogue, so every listed key is
  one ``search_docs`` will accept.
* **Tenant scope + dedupe:** global rows + the tenant's own rows; a
  tenant-curated row shadowing a global key appears once (the tenant row
  wins).
* **Pagination:** keyset by ``collection_key``; a full page carries a
  ``next_cursor``, a short page carries ``null``.
* The audit row carries the canonical ``op_id="meho.docs.collections.list"``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

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
_OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000c0")


async def _seed(
    *,
    collection_key: str,
    vendor: str = "VMware by Broadcom",
    products: list[str] | None = None,
    when_to_use: str | None = "Use for product questions.",
    status: str = "ready",
    tenant_id: UUID | None = None,
) -> None:
    """Insert a :class:`DocCollection` row with explicit identity fields."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollectionORM(
                tenant_id=tenant_id,
                collection_key=collection_key,
                vendor=vendor,
                products=products if products is not None else ["vsphere", "nsx"],
                description=f"{vendor} docs.",
                when_to_use=when_to_use,
                backend={"type": "corpus-http"},
                status=status,
            ),
        )


def _seed_sync(**kwargs: Any) -> None:
    """Run :func:`_seed` to completion from a sync test (fresh loop)."""
    asyncio.run(_seed(**kwargs))


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role + capability set."""
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
    )


@pytest.fixture
def catalogue_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` whose operator's capability set is parametrised.

    Default is the empty set (unprovisioned); provision with
    ``@pytest.mark.parametrize("catalogue_client", [frozenset({...})], indirect=True)``.
    """
    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.OPERATOR, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call_list(client: TestClient, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Issue ``tools/call list_doc_collections`` and return the JSON-RPC body."""
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_doc_collections",
                "arguments": arguments if arguments is not None else {},
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    """Extract the structured tool result from a successful JSON-RPC body."""
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


async def _mcp_audit_rows() -> list[AuditLog]:
    """Read every MCP-method ``audit_log`` row, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in result.scalars().all() if row.method == "MCP"]


# ---------------------------------------------------------------------------
# Registration + capability gate (visibility)
# ---------------------------------------------------------------------------


def test_registered_definition_is_operator_read_capability_gated() -> None:
    """The tool declares operator role, read op_class, and the meho-docs gate."""
    entry = get_tool("list_doc_collections")
    assert entry is not None
    defn, _handler = entry
    assert defn.required_role == TenantRole.OPERATOR
    assert defn.op_class == "read"
    assert defn.required_capability == _DOCS_CAPABILITY


def test_absent_from_tools_list_for_unprovisioned_tenant(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """An operator without the base ``meho-docs`` never sees the catalogue tool."""
    client, _op = catalogue_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "list_doc_collections" not in names


@pytest.mark.parametrize("catalogue_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_present_with_strict_schema_for_provisioned_tenant(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """Once provisioned, the tool appears with a strict optional-arg schema."""
    client, _op = catalogue_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    assert "list_doc_collections" in tools_by_name
    tool = tools_by_name["list_doc_collections"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"vendor", "cursor", "limit"}
    # No required args — the catalogue lists everything by default.
    assert "required" not in schema or schema["required"] == []
    # The wire shape never leaks the server-side gating fields.
    assert "required_capability" not in tool


@pytest.mark.parametrize("catalogue_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_description_steers_the_agent_to_collection_keys(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """The description names search_docs and the collection-key contract."""
    client, _op = catalogue_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools_by_name["list_doc_collections"]["description"]
    assert "search_docs" in desc
    assert "collection" in desc.lower()


def test_hidden_from_provisioned_read_only_operator() -> None:
    """The capability does not relax the role gate: read_only never sees it."""
    op = _operator(role=TenantRole.READ_ONLY, capabilities=frozenset({_DOCS_CAPABILITY}))

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "list_doc_collections" not in names


# ---------------------------------------------------------------------------
# tools/call — base capability gate
# ---------------------------------------------------------------------------


def test_tools_call_403_when_unprovisioned(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """Naming the tool directly still 403s when the base capability is absent."""
    client, _op = catalogue_client
    body = _call_list(client)
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# tools/call — entitlement filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"})],
    indirect=True,
)
def test_lists_only_entitled_collections(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """A provisioned tenant sees only the collections it holds an entitlement for.

    Two collections are seeded; the operator holds ``meho-docs:vmware`` but
    not ``meho-docs:netapp``. Only ``vmware`` appears — every listed key is
    one ``search_docs`` would accept.
    """
    client, _op = catalogue_client
    _seed_sync(collection_key="vmware", vendor="VMware by Broadcom")
    _seed_sync(collection_key="netapp", vendor="NetApp")

    payload = _payload(_call_list(client))
    keys = [c["collection_key"] for c in payload["collections"]]
    assert keys == ["vmware"]
    assert payload["next_cursor"] is None
    row = payload["collections"][0]
    assert row["vendor"] == "VMware by Broadcom"
    assert row["products"] == ["vsphere", "nsx"]
    assert row["when_to_use"] == "Use for product questions."
    assert row["status"] == "ready"


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY})],
    indirect=True,
)
def test_provisioned_but_no_per_collection_entitlement_returns_empty(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """The base add-on alone lists nothing — entitlement is per collection."""
    client, _op = catalogue_client
    _seed_sync(collection_key="vmware")
    payload = _payload(_call_list(client))
    assert payload["collections"] == []
    assert payload["next_cursor"] is None


# ---------------------------------------------------------------------------
# tools/call — tenant scope + dedupe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"})],
    indirect=True,
)
def test_tenant_row_shadows_global_key_once(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """A tenant-curated row shadowing a global key appears once — tenant wins."""
    client, op = catalogue_client
    _seed_sync(collection_key="vmware", vendor="Global VMware")
    _seed_sync(collection_key="vmware", vendor="Tenant VMware", tenant_id=op.tenant_id)

    payload = _payload(_call_list(client))
    assert len(payload["collections"]) == 1
    assert payload["collections"][0]["vendor"] == "Tenant VMware"


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"})],
    indirect=True,
)
def test_other_tenant_curated_row_is_invisible(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """A row curated by another tenant is out of scope, even when entitled."""
    client, _op = catalogue_client
    _seed_sync(collection_key="vmware", vendor="Other Tenant", tenant_id=_OTHER_TENANT_ID)
    payload = _payload(_call_list(client))
    assert payload["collections"] == []


# ---------------------------------------------------------------------------
# tools/call — vendor filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:vmware", "meho-docs:netapp"})],
    indirect=True,
)
def test_vendor_filter_narrows_the_catalogue(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """An exact-match ``vendor`` narrows to one vendor's collections."""
    client, _op = catalogue_client
    _seed_sync(collection_key="vmware", vendor="VMware by Broadcom")
    _seed_sync(collection_key="netapp", vendor="NetApp")

    payload = _payload(_call_list(client, {"vendor": "NetApp"}))
    keys = [c["collection_key"] for c in payload["collections"]]
    assert keys == ["netapp"]


# ---------------------------------------------------------------------------
# tools/call — keyset pagination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:alpha", "meho-docs:bravo", "meho-docs:charlie"})],
    indirect=True,
)
def test_keyset_pagination_walks_by_collection_key(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """A full page carries next_cursor; the next page resumes after it."""
    client, _op = catalogue_client
    for key in ("alpha", "bravo", "charlie"):
        _seed_sync(collection_key=key, vendor=key.title())

    first = _payload(_call_list(client, {"limit": 2}))
    assert [c["collection_key"] for c in first["collections"]] == ["alpha", "bravo"]
    assert first["next_cursor"] == "bravo"

    second = _payload(_call_list(client, {"limit": 2, "cursor": "bravo"}))
    assert [c["collection_key"] for c in second["collections"]] == ["charlie"]
    assert second["next_cursor"] is None


# ---------------------------------------------------------------------------
# tools/call — strict schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("catalogue_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_rejects_extra_arguments(
    catalogue_client: tuple[TestClient, Operator],
) -> None:
    """``additionalProperties: false`` rejects unknown top-level keys."""
    client, _op = catalogue_client
    body = _call_list(client, {"tenant_id": "smuggled"})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Audit op_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalogue_client",
    [frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"})],
    indirect=True,
)
async def test_audit_row_carries_canonical_op_id(
    catalogue_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """The catalogue read writes one audit row with the canonical op_id."""
    client, _op = catalogue_client
    await _seed(collection_key="vmware")
    body = _call_list(client)
    assert body["result"]["isError"] is False

    rows = await _mcp_audit_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.collections.list"
    assert payload["op_class"] == "read"
