# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for #93: ``call_operation`` keeps agent secrets out of audit.

Bug (#93): ``handle_tools_call`` classified the wrapper tool by its own
name (``"call_operation"``), which falls through to ``"other"`` and so
treated secret-bearing arguments (``vault.kv.put``'s ``params.data``,
``vault.auth.userpass.write``'s ``params.password``) as routine detail.

Fix (#93): when ``audit_name == "call_operation"`` and ``arguments``
carries a non-empty string ``op_id``, the inner op id drives the
sensitivity classification, so credential classes are clamped.

A tool call publishes no Broadcast event (#3758): the wrapper audit row
is the record. These tests drive the real MCP transport with the inner
dispatch stubbed, then read the stored ``audit_log`` rows back:

* AC#1 / AC#2: for every op in the credential-bearing classes
  (``credential_write``, ``credential_mint``, ``credential_read``) the
  secret in the ``call_operation`` params reaches **no** audit row, and
  no row carries a raw ``params`` key.
* AC#5: the wrapper row keeps ``op_id == "call_operation"`` (and the
  ``/mcp/tools/call/call_operation`` path) so ``meho audit query``
  cardinality and path-based correlations are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

# ---------------------------------------------------------------------------
# Canary secret value — asserted to NEVER appear in a stored audit row
# ---------------------------------------------------------------------------

_SECRET_TOKEN = "s3cr3t-canary-must-not-leak-into-audit"

_WRAPPER_PATH = "/mcp/tools/call/call_operation"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools_call_envelope(
    inner_op_id: str,
    params: dict[str, Any] | None = None,
    call_id: int = 1,
) -> dict[str, Any]:
    """Build a ``tools/call call_operation`` JSON-RPC request body."""
    arguments: dict[str, Any] = {
        "connector_id": "vault-1.x",
        "op_id": inner_op_id,
    }
    if params is not None:
        arguments["params"] = params
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": "call_operation", "arguments": arguments},
    }


def _dummy_dispatch_result(op_id: str) -> dict[str, Any]:
    """Minimal ``call_operation`` handler return value."""
    return {"status": "ok", "op_id": op_id, "duration_ms": 1.0, "result": {}}


async def _audit_rows() -> list[AuditLog]:
    """Every ``audit_log`` row the request wrote, oldest first.

    Excludes the boot-profile stamp row the app lifespan writes on
    startup (#2288); it is unrelated to the request under test.
    """
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
            .order_by(AuditLog.occurred_at)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_call_operation() -> Iterator[AsyncMock]:
    """Stub the ``call_operation`` meta-tool function to avoid real dispatch.

    Patches the name as bound in ``meho_backplane.mcp.tools.operations``
    (the module that registers the ``call_operation`` MCP tool and calls
    this function from ``_call_operation_handler``). With the inner
    dispatch stubbed, the only audit row a call can write is the
    wrapper row ``handle_tools_call`` commits.
    """
    with patch(
        "meho_backplane.mcp.tools.operations.call_operation",
        new_callable=AsyncMock,
    ) as mock_fn:

        async def _echo_result(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
            return _dummy_dispatch_result(arguments.get("op_id", "unknown"))

        mock_fn.side_effect = _echo_result
        yield mock_fn


# ---------------------------------------------------------------------------
# AC#1 + AC#2: credential-bearing ops → the secret reaches no audit row
# ---------------------------------------------------------------------------

_CREDENTIAL_WRITE_OPS = [
    # AC#1 canonical example (issue body)
    ("vault.kv.put", {"path": "secret/app", "data": {"token": _SECRET_TOKEN}}),
    # AC#2 parametrized coverage
    ("vault.kv.patch", {"path": "secret/app", "data": {"token": _SECRET_TOKEN}}),
    ("vault.auth.userpass.write", {"username": "svc", "password": _SECRET_TOKEN}),
    (
        "vault.auth.userpass.update_password",
        {"username": "svc", "password": _SECRET_TOKEN},
    ),
]

_CREDENTIAL_MINT_OPS = [
    ("vault.token.create", {"policies": ["default"], "note": _SECRET_TOKEN}),
    ("vault.auth.approle.generate_secret_id", {"role_name": "my-role", "note": _SECRET_TOKEN}),
    ("harbor.robot.create", {"name": "ci-bot", "permissions": [], "note": _SECRET_TOKEN}),
]

_CREDENTIAL_READ_OPS = [
    ("vault.kv.read", {"path": "secret/app", "note": _SECRET_TOKEN}),
    ("vault.kv.list", {"path": "secret/", "note": _SECRET_TOKEN}),
]

_ALL_CREDENTIAL_OPS = _CREDENTIAL_WRITE_OPS + _CREDENTIAL_MINT_OPS + _CREDENTIAL_READ_OPS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.parametrize("inner_op_id,op_params", _ALL_CREDENTIAL_OPS)
@pytest.mark.asyncio
async def test_call_operation_credential_inner_op_secret_reaches_no_audit_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
    stub_call_operation: AsyncMock,
    inner_op_id: str,
    op_params: dict[str, Any],
) -> None:
    """AC#1 + AC#2: a secret in the ``call_operation`` params is stored nowhere.

    Asserts both the structural absence of a raw ``params`` key on every
    stored row AND the absence of the canary secret string from every
    serialised row payload, so a future shape change that embeds the
    params under a different key doesn't quietly regress.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        _tools_call_envelope(inner_op_id=inner_op_id, params=op_params),
    )
    # The stub handler succeeds; the MCP envelope returns isError=False.
    body = response.json()
    assert "error" not in body, f"unexpected MCP error for {inner_op_id}: {body}"
    assert body["result"]["isError"] is False

    rows = await _audit_rows()
    assert len(rows) == 1, f"expected one wrapper audit row for {inner_op_id}, got {len(rows)}"
    for row in rows:
        assert "params" not in row.payload, (
            f"credential inner op {inner_op_id!r} stored raw params: {row.payload}"
        )
        serialised = json.dumps(row.payload, default=str)
        assert _SECRET_TOKEN not in serialised, (
            f"canary secret found in audit row for {inner_op_id!r}: {serialised}"
        )


# ---------------------------------------------------------------------------
# AC#5: the wrapper row keeps op_id + path of the wrapper tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_call_operation_wrapper_row_keeps_wrapper_op_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
    stub_call_operation: AsyncMock,
) -> None:
    """AC#5: the wrapper audit row's ``op_id`` stays ``"call_operation"``.

    Only the sensitivity classification follows the inner op; the row's
    identifying ``op_id`` and ``path`` must remain the wrapper tool name
    so ``meho audit query`` cardinality and path-based correlations are
    unchanged.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        _tools_call_envelope(
            inner_op_id="vault.kv.put",
            params={"path": "secret/x", "data": {"password": _SECRET_TOKEN}},
        ),
    )
    assert "error" not in response.json()

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.path == _WRAPPER_PATH
    assert row.payload["op_id"] == "call_operation"
    assert row.payload["op_class"] == "tool_call"
    assert _SECRET_TOKEN not in json.dumps(row.payload, default=str)
