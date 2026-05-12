# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the MCP-specific audit integration (G0.5-T5, #250).

Covers every acceptance criterion on issue #250:

* ``tools/call meho.status`` writes exactly one ``audit_log`` row with
  method=MCP, path=/mcp/tools/call/meho.status, status_code=200,
  payload carries the empty-args params_hash + op_class=read.
* ``resources/read meho://tenant/<id>/info`` writes exactly one
  ``audit_log`` row with path=/mcp/resources/read/<uri>.
* ``tools/call`` against an unknown tool writes a row with status_code=404
  and op_class=unknown.
* ``compute_params_hash`` is deterministic across dict insertion order
  and equivalent representations.
* The chassis :class:`~meho_backplane.audit.AuditMiddleware` skips
  ``/mcp`` requests entirely (no duplicate row per JSON-RPC envelope).
* Audit-write failure converts an otherwise-successful MCP call into a
  JSON-RPC ``-32603`` Internal Error (fail-closed contract).
* Every row carries the correct ``tenant_id`` populated from the
  validated :class:`Operator`.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.mcp.audit import compute_params_hash, write_mcp_audit_row
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import clear_registries
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
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
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin Keycloak / Vault / backplane env vars every test in this file needs.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``; the helper-level
    ``write_mcp_audit_row`` test (which doesn't use ``client_with_operator``)
    still calls ``get_sessionmaker()`` → ``get_settings()`` and would
    explode on a missing Keycloak knob otherwise. Pinning here makes
    every test in the file independently runnable.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_registry_with_production_modules() -> Iterator[None]:
    """Clear registries then re-execute production tool/resource registrations.

    Same pattern documented in :mod:`tests.test_mcp_tool_meho_status`:
    Python's import cache makes ``eager_import_mcp_modules`` a no-op on
    the 2nd+ test in the same process, so the production registrations
    have to be reloaded explicitly to land after the registry clear.
    """
    from meho_backplane.mcp.resources import tenant_info
    from meho_backplane.mcp.tools import meho_status

    clear_registries()
    importlib.reload(meho_status)
    importlib.reload(tenant_info)
    yield
    clear_registries()


@pytest.fixture
def client_with_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` with the MCP auth dependency overridden to a fixture operator."""
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
    """Insert the operator's :class:`Tenant` row so the tenant_info resource resolves."""
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


async def _audit_rows() -> list[AuditLog]:
    """Read every ``audit_log`` row, ordered by ``occurred_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# compute_params_hash — pure-function determinism
# ---------------------------------------------------------------------------


def test_params_hash_empty_dict_is_stable() -> None:
    """``compute_params_hash({})`` returns the canonical empty-object hash.

    Regression-locks the hash so a future change to the canonicalisation
    rule (separators, sort_keys) would surface here loudly instead of
    silently invalidating every existing payload row's params_hash.
    """
    expected = (
        # SHA-256("{}") — the canonical JSON encoding of an empty dict
        # under ``sort_keys=True, separators=(",", ":")``.
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert compute_params_hash({}) == expected


def test_params_hash_is_order_independent() -> None:
    """Same dict-keys in different insertion orders → same hash."""
    a = {"foo": 1, "bar": 2}
    b = {"bar": 2, "foo": 1}
    assert compute_params_hash(a) == compute_params_hash(b)


def test_params_hash_differs_for_distinct_inputs() -> None:
    """``{x: 1}`` and ``{x: 2}`` produce different digests."""
    assert compute_params_hash({"x": 1}) != compute_params_hash({"x": 2})


# ---------------------------------------------------------------------------
# tools/call audit — happy path + status_code derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_meho_status_writes_one_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: tools/call meho.status writes one row, method=MCP, status_code=200."""
    client, op = client_with_operator

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/meho.status"
    assert row.status_code == 200
    assert row.payload["op_id"] == "meho.status"
    assert row.payload["op_class"] == "read"
    assert row.payload["params_hash"] == compute_params_hash({})


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_writes_audit_row_with_404(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #3: unknown.tool → row with status_code=404, op_class=unknown."""
    client, _op = client_with_operator

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no.such.tool", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "/mcp/tools/call/no.such.tool"
    assert row.status_code == 404
    assert row.payload["op_class"] == "unknown"


# ---------------------------------------------------------------------------
# resources/read audit — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resources_read_tenant_info_writes_one_audit_row(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC #2: resources/read meho://tenant/<id>/info → one row, path matches."""
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/info"

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
    assert "result" in response.json()

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == f"/mcp/resources/read/{uri}"
    assert row.status_code == 200
    assert row.payload["uri"] == uri
    assert row.payload["op_class"] == "read"


# ---------------------------------------------------------------------------
# AC #7: chassis AuditMiddleware does NOT double-audit /mcp requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_envelope_does_not_produce_chassis_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """The chassis ``AuditMiddleware`` skips ``/mcp`` so one POST = one row.

    Without the path-prefix exclusion in :class:`AuditMiddleware`, the
    middleware would write a row per JSON-RPC POST (wrong granularity)
    AND each MCP handler would write its own row (correct granularity).
    The test pins the single-row outcome.
    """
    client, _op = client_with_operator

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    # Exactly one row from the MCP handler; the chassis AuditMiddleware
    # path-prefix exclusion (audit.py:_AUDIT_SKIP_PATH_PREFIXES) keeps
    # the JSON-RPC envelope from adding a second.
    assert len(rows) == 1
    assert rows[0].method == "MCP"


# ---------------------------------------------------------------------------
# AC #5: audit-write failure → fail-closed -32603
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_converts_call_to_internal_error(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """When :func:`write_mcp_audit_row` raises, the call fails with -32603.

    Fail-closed contract: an audit-write failure invalidates the
    operation. The client sees -32603 INTERNAL_ERROR; no
    ``result`` envelope is returned even if the handler itself
    succeeded.
    """
    client, _op = client_with_operator

    async def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("simulated audit-write failure")

    with patch(
        "meho_backplane.mcp.handlers.write_mcp_audit_row",
        side_effect=_explode,
    ):
        response = _post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "meho.status", "arguments": {}},
            },
        )

    body = response.json()
    assert body["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# write_mcp_audit_row helper-level test (independent of MCP dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_mcp_audit_row_persists_every_field() -> None:
    """The helper round-trips every field including the JSON ``payload``."""
    op = _operator()
    request_id = uuid4()
    payload = {"op_id": "test.tool", "params_hash": "abc123", "op_class": "read"}

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/test.tool",
        status_code=200,
        duration_ms=12.34,
        payload=payload,
        request_id=request_id,
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/test.tool"
    assert row.status_code == 200
    assert row.request_id == request_id
    # SQLite stores Numeric via _PORTABLE_JSON if applicable; check value
    assert float(row.duration_ms or 0) == pytest.approx(12.34)
    assert json.loads(json.dumps(row.payload)) == payload
