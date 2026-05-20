# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.3-T5 admin MCP override-CRUD tools.

Coverage matrix (Task #382 acceptance criteria):

* All three tools (``meho.broadcast.overrides.list/set/remove``) are
  registered with ``required_role=TENANT_ADMIN`` -- visible in
  ``tools/list`` only for tenant_admin operators.
* ``operator``-role JWTs see ``tools/list`` without the three tools
  (registry filter), AND a direct ``tools/call`` against
  ``meho.broadcast.overrides.set`` is rejected by the dispatcher's
  call-time re-check.
* Tenant-admin happy paths: list / set / remove round-trip in-process
  through the T4 ``*_impl`` functions, producing the same DB rows the
  REST surface would.
* ``set`` Pydantic validation (regex chars, half-set scope pair, invalid
  detail) surfaces as ``-32602`` Invalid Params.
* ``set`` duplicate maps to Invalid Params with the
  ``broadcast_override_already_exists`` detail string.
* ``remove`` 404 (cross-tenant or unknown id) maps to Invalid Params
  with the ``broadcast_override_not_found`` detail.
* Mutation MCP calls produce audit rows identical in shape to the REST
  surface (modulo ``method="MCP"`` + the synthetic
  ``/mcp/tools/call/...`` path).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.overrides import (
    _TENANT_CACHE,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, BroadcastOverride
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


@pytest.fixture(autouse=True)
def _reset_resolver_cache() -> Iterator[None]:
    """T2's per-tenant cache is module-level; wipe between cases."""
    reset_overrides_cache_for_testing()
    yield
    reset_overrides_cache_for_testing()


def _result_dict(response: Any) -> dict[str, Any]:
    """Extract the JSON-decoded tool result from a JSON-RPC response."""
    body = response.json()
    assert "error" not in body, body
    content = body["result"]["content"]
    # The dispatcher wraps the handler's return value in a single
    # text content block carrying the JSON-serialised dict.
    return json.loads(content[0]["text"])


async def _audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


async def _override_rows() -> list[BroadcastOverride]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).order_by(BroadcastOverride.created_at),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_tools_list_exposes_three_override_tools_to_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Tenant-admin sees all three ``meho.broadcast.overrides.*`` tools."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.overrides.list" in names
    assert "meho.broadcast.overrides.set" in names
    assert "meho.broadcast.overrides.remove" in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY, TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_hides_override_tools_from_non_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Non-admin roles (``read_only`` + ``operator``) do NOT see the three tools.

    AC 6 explicitly names ``operator`` -- parametrising both
    non-admin roles closes the literal AC text and covers the
    boundary where the registry filter draws the line.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.overrides.list" not in names
    assert "meho.broadcast.overrides.set" not in names
    assert "meho.broadcast.overrides.remove" not in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY, TenantRole.OPERATOR],
    indirect=True,
)
def test_non_admin_tools_call_set_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from a non-admin (knows the name) is rejected.

    The registry filter hides the tool from ``tools/list`` (verified
    above), but a malicious / curious client could still POST a
    ``tools/call`` with the literal tool name. The dispatcher's
    call-time RBAC re-check is the load-bearing second gate.
    AC 6 explicitly names ``operator``; the ``read_only`` parametrize
    covers the lower role for defence in depth.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {
                    "op_id_pattern": "vault.kv.*",
                    "detail": "aggregate",
                },
            },
        },
    )
    body = resp.json()
    assert "error" in body
    # The dispatcher emits ``-32602`` Invalid Params for role denials
    # on a known tool name (the same shape as an unknown-tool 404).
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Tenant-admin happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_set_creates_row_returns_override_dict(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {
                    "op_id_pattern": "k8s.configmap.info",
                    "scope_field": "namespace",
                    "scope_value": "kube-system",
                    "detail": "aggregate",
                },
            },
        },
    )
    assert resp.status_code == 200
    result = _result_dict(resp)
    override = result["override"]
    assert override["op_id_pattern"] == "k8s.configmap.info"
    assert override["detail"] == "aggregate"
    assert uuid.UUID(override["id"])
    rows = await _override_rows()
    assert len(rows) == 1


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_list_returns_own_tenant_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    # Seed two rows via set.
    for pattern in ("vault.kv.*", "audit.*"):
        post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "meho.broadcast.overrides.set",
                    "arguments": {"op_id_pattern": pattern, "detail": "aggregate"},
                },
            },
        )
    # List them back.
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.list",
                "arguments": {},
            },
        },
    )
    result = _result_dict(resp)
    patterns = {row["op_id_pattern"] for row in result["overrides"]}
    assert patterns == {"vault.kv.*", "audit.*"}


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_remove_deletes_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    create_resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            },
        },
    )
    override_id = _result_dict(create_resp)["override"]["id"]

    remove_resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.remove",
                "arguments": {"override_id": override_id},
            },
        },
    )
    result = _result_dict(remove_resp)
    assert result == {"removed": True}
    rows = await _override_rows()
    assert rows == []


# ---------------------------------------------------------------------------
# Error mapping -- HTTPException → McpInvalidParamsError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_set_duplicate_maps_to_invalid_params_409(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    # The composite unique constraint includes NULL-able ``scope_field``
    # / ``scope_value`` columns; under SQL's default NULL-distinct
    # semantics, two rows with both NULL collide-by-design but the
    # constraint declares them distinct. Use a fully-scoped pair so
    # the second insert deterministically triggers the unique
    # violation across PG + SQLite. Mirrors T4's REST duplicate test
    # body in ``tests/test_api_v1_broadcast_overrides.py``.
    client, _op = client_with_operator
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "meho.broadcast.overrides.set",
            "arguments": {
                "op_id_pattern": "k8s.configmap.info",
                "scope_field": "namespace",
                "scope_value": "kube-system",
                "detail": "aggregate",
            },
        },
    }
    post_mcp(client, body)  # first call: success
    resp = post_mcp(client, body)  # second call: duplicate
    err = resp.json()["error"]
    assert err["code"] == INVALID_PARAMS
    assert "broadcast_override_already_exists" in err["message"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_remove_unknown_id_maps_to_invalid_params_404(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.remove",
                "arguments": {"override_id": str(uuid.uuid4())},
            },
        },
    )
    err = resp.json()["error"]
    assert err["code"] == INVALID_PARAMS
    assert "broadcast_override_not_found" in err["message"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_set_regex_chars_rejected_by_pydantic(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """``op_id_pattern`` with regex syntax is rejected by Pydantic."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {
                    "op_id_pattern": "vault\\.kv\\..+",
                    "detail": "aggregate",
                },
            },
        },
    )
    err = resp.json()["error"]
    assert err["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_remove_with_malformed_uuid_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.remove",
                "arguments": {"override_id": "not-a-uuid"},
            },
        },
    )
    err = resp.json()["error"]
    assert err["code"] == INVALID_PARAMS
    assert "uuid" in err["message"].lower()


# ---------------------------------------------------------------------------
# Audit-row equivalence with REST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_set_via_mcp_writes_audit_row_with_override_diff(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """The MCP set tool produces the same audit-row diff as the REST POST.

    Specifically: ``audit_log.payload`` carries ``op_id=meho.broadcast.overrides.set``,
    ``op_class=write``, and the override-diff fragment
    (``override_op="set"`` / ``override_id`` / ``override_pattern`` /
    ``override_detail``) -- the contextvar binding in
    :func:`~meho_backplane.api.v1.broadcast_overrides._bind_set_audit`
    fires inside the impl function regardless of REST vs MCP caller.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {"op_id_pattern": "vault.kv.*", "detail": "full"},
            },
        },
    )
    assert resp.status_code == 200
    override_id = _result_dict(resp)["override"]["id"]
    rows = await _audit_rows()
    # The MCP envelope writes one row with ``method="MCP"`` and path
    # ``/mcp/tools/call/meho.broadcast.overrides.set``.
    mcp_rows = [
        r
        for r in rows
        if r.method == "MCP" and r.path == "/mcp/tools/call/meho.broadcast.overrides.set"
    ]
    assert len(mcp_rows) == 1
    payload = mcp_rows[0].payload
    assert payload["op_id"] == "meho.broadcast.overrides.set"
    assert payload["op_class"] == "write"
    assert payload["override_op"] == "set"
    assert payload["override_id"] == override_id
    assert payload["override_pattern"] == "vault.kv.*"
    assert payload["override_detail"] == "full"


# ---------------------------------------------------------------------------
# Tenant-scoping invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_list_returns_only_own_tenant_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """A row owned by a different tenant must not appear in this operator's list."""
    # Seed a row directly into the DB for a foreign tenant.
    foreign_tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        from meho_backplane.db.models import Tenant

        session.add(
            Tenant(id=foreign_tenant_id, slug="foreign", name="Foreign Tenant"),
        )
        await session.flush()
        session.add(
            BroadcastOverride(
                tenant_id=foreign_tenant_id,
                op_id_pattern="vault.kv.*",
                detail="aggregate",
                created_by_sub="foreign-op",
            ),
        )

    # The operator's MCP list call should return nothing.
    assert foreign_tenant_id != OPERATOR_TENANT_ID  # sanity
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.list",
                "arguments": {},
            },
        },
    )
    result = _result_dict(resp)
    assert result == {"overrides": []}


# ---------------------------------------------------------------------------
# Cache invalidation -- AC 9
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_set_via_mcp_invalidates_tenant_override_cache(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """``set`` over MCP invalidates T2's per-tenant cache (AC 9).

    Mirrors ``test_post_invalidates_resolver_cache`` in the T4 REST
    suite: pre-seed the cache with a long-TTL empty sentinel and a
    matching tenant scope, then issue the set call. The
    ``create_override_impl`` function calls
    :func:`invalidate_tenant_cache` on success, so the post-call
    cache state must NOT carry the pre-seeded sentinel under this
    tenant's slot.
    """
    import time as time_module

    client, _op = client_with_operator
    # Sentinel: empty rule set under a far-future TTL. Any rule
    # mutation that fails to call ``invalidate_tenant_cache`` would
    # leave this sentinel in place; the test catches that regression.
    _TENANT_CACHE[OPERATOR_TENANT_ID] = ([], time_module.monotonic() + 600.0)
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {
                    "op_id_pattern": "vault.kv.*",
                    "detail": "aggregate",
                },
            },
        },
    )
    assert resp.status_code == 200
    # Post-call: the sentinel slot must be evicted. Whether the
    # post-route audit publish path re-hydrates the slot with FRESH
    # rows or leaves it empty for the next lookup is implementation
    # detail; the load-bearing invariant is "the empty sentinel that
    # would have served stale 'no overrides' verdicts is gone".
    entry = _TENANT_CACHE.get(OPERATOR_TENANT_ID)
    if entry is not None:
        cached_rules, _expires_at = entry
        # If the cache was re-hydrated, the new entry must carry the
        # rule we just created (not the empty sentinel).
        patterns = {r.op_id_pattern for r in cached_rules}
        assert "vault.kv.*" in patterns, "post-set cache still holds the pre-seeded empty sentinel"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_remove_via_mcp_invalidates_tenant_override_cache(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """``remove`` over MCP invalidates T2's per-tenant cache (AC 9).

    Same shape as the ``set`` cache-invalidation test. A removed rule
    must NOT linger in the cache under this tenant's slot.
    """
    import time as time_module

    client, _op = client_with_operator
    # Seed a real rule via the MCP set call.
    create_resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {"op_id_pattern": "audit.*", "detail": "aggregate"},
            },
        },
    )
    override_id = _result_dict(create_resp)["override"]["id"]
    # Pin a stale-cache sentinel containing the just-created rule
    # under a long TTL. The remove call below must evict the slot.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).where(
                BroadcastOverride.tenant_id == OPERATOR_TENANT_ID,
            ),
        )
        rows = list(result.scalars().all())
    _TENANT_CACHE[OPERATOR_TENANT_ID] = (rows, time_module.monotonic() + 600.0)
    assert len(_TENANT_CACHE[OPERATOR_TENANT_ID][0]) == 1
    remove_resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.remove",
                "arguments": {"override_id": override_id},
            },
        },
    )
    assert remove_resp.status_code == 200
    entry = _TENANT_CACHE.get(OPERATOR_TENANT_ID)
    if entry is not None:
        cached_rules, _ = entry
        cached_ids = {r.id for r in cached_rules}
        assert uuid.UUID(override_id) not in cached_ids, (
            "post-remove cache still holds the deleted rule"
        )


# ---------------------------------------------------------------------------
# Pydantic cross-field validation -- half-set scope pair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_set_half_set_scope_pair_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """``scope_field`` without ``scope_value`` fails Pydantic's after-validator.

    The dispatcher's jsonschema check permits both fields to be
    optional and the half-set combination passes the schema layer.
    ``BroadcastOverrideCreate._scope_pair_must_be_consistent`` is
    the load-bearing invariant: it raises ``ValueError`` →
    Pydantic ValidationError → ``McpInvalidParamsError`` → -32602.
    Verifies the MCP set handler runs T4's Pydantic re-validation
    rather than skipping straight into the impl.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.overrides.set",
                "arguments": {
                    "op_id_pattern": "k8s.configmap.info",
                    "scope_field": "namespace",
                    # scope_value deliberately omitted -- half-set pair.
                    "detail": "aggregate",
                },
            },
        },
    )
    err = resp.json()["error"]
    assert err["code"] == INVALID_PARAMS
    # The Pydantic ValueError message threads through to the wire
    # error; the substring "scope_field and scope_value" pins the
    # validator-message identity so a future rewording surfaces here.
    assert "scope_field" in err["message"]
    assert "scope_value" in err["message"]


# ---------------------------------------------------------------------------
# inputSchema strictness -- matches connector_admin precedent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_input_schemas_are_strict_draft_2020_12(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Every override tool's inputSchema is JSON-Schema 2020-12 + strict.

    Matches the ``connector_admin`` precedent's schema-strictness
    pattern: schema itself validates as Draft 2020-12, every schema
    sets ``additionalProperties: false`` (so a typo'd kwarg fails at
    the dispatcher's jsonschema gate rather than landing as a silent
    no-op), and MEHO-internal fields (``required_role`` /
    ``op_class``) are stripped from the wire shape per
    :meth:`ToolDefinition.to_wire`.
    """
    import jsonschema

    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    for name in (
        "meho.broadcast.overrides.list",
        "meho.broadcast.overrides.set",
        "meho.broadcast.overrides.remove",
    ):
        schema = tools[name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, (
            f"{name}: missing additionalProperties=false"
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        # MEHO-internal fields must not leak onto the wire.
        assert "required_role" not in tools[name]
        assert "op_class" not in tools[name]
