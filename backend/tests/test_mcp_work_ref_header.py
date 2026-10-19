# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``Meho-Work-Ref`` header on the real ``/mcp`` endpoint (#4256).

The audit middleware reads the header for every HTTP request. It skips
its own audit row for ``/mcp`` (the MCP handlers write one row per tool
call), but it must still read the header there. These tests send a
``call_operation`` through ``POST /mcp`` and check the audit rows:

* the header lands on the MCP row and on the DISPATCH row;
* a ``work_ref`` tool argument wins over the header on the DISPATCH row;
* the next request without the header gets no work ref (no leak).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_HEADER_REF = "gh:evoila/meho#7"
_ARG_REF = "gh:evoila/meho#99"
_OP_ID = "stub.work_ref_header"
_PATH = "/mcp/tools/call/call_operation"


class _StubConnector(Connector):
    """Minimal connector so the dispatcher finds a class for the stub op."""

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: return a small result."""
    return {"ok": True}


@pytest.fixture
async def _stub_op() -> AsyncIterator[None]:
    """Register the stub connector and typed op, then remove the connector."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    embedding.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    embedding.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    reset_dispatcher_caches()
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=_OP_ID,
        handler=_echo_handler,
        summary="Stub op for the work ref header test.",
        description="Return a small result.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )
    try:
        yield
    finally:
        connector_registry._REGISTRY_V2.pop(("stub", "", ""), None)
        reset_dispatcher_caches()


def _call(client: TestClient, request_id: int, headers: dict[str, str], **extra: str) -> None:
    """Send one ``call_operation`` for the stub op through ``POST /mcp``."""
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {"connector_id": "stub-1.x", "op_id": _OP_ID, **extra},
            },
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert "error" not in response.json(), response.json()


async def _rows(method: str) -> list[AuditLog]:
    """Audit rows for the stub op with the given method, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.scalars(
            select(AuditLog).where(AuditLog.method == method).order_by(AuditLog.occurred_at),
        )
        rows = list(result.all())
    if method == "MCP":
        return [r for r in rows if r.path == _PATH]
    return [r for r in rows if (r.payload or {}).get("op_id") == _OP_ID]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.usefixtures("_stub_op")
async def test_mcp_work_ref_header_reaches_audit_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Header only, then header plus argument, then nothing."""
    client, _op = client_with_operator

    # 1. Header only: both rows carry the header value.
    _call(client, 1, {"Meho-Work-Ref": _HEADER_REF})
    # 2. Header and a different ``work_ref`` argument: the argument wins
    #    on the DISPATCH row, as on REST.
    _call(client, 2, {"Meho-Work-Ref": _HEADER_REF}, work_ref=_ARG_REF)
    # 3. No header, no argument: no work ref, nothing left over from 1 or 2.
    _call(client, 3, {})

    mcp_rows = await _rows("MCP")
    dispatch_rows = await _rows("DISPATCH")
    assert [r.work_ref for r in mcp_rows] == [_HEADER_REF, _HEADER_REF, None]
    assert [r.work_ref for r in dispatch_rows] == [_HEADER_REF, _ARG_REF, None]
    assert work_ref_var.get() is None
