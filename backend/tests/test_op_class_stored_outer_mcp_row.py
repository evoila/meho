# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The outer MCP row of ``call_operation`` reads the class stored on the inner operation.

The wrapper row and the broadcast detail of ``call_operation`` come from the
operation inside. When that operation's row stores a secret class (a connector
floor set it), the detail is the aggregate only, as for an op id in a secret
class. Without a stored class the detail stays full.
"""

from __future__ import annotations

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
from meho_backplane.operations.ingest.safety_floors import _FLOORS
from tests._stored_op_class_harness import CONNECTOR_ID, GET_CLIENTS, MADE_UP_VALUE, ingest
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def stub_call_operation() -> Iterator[AsyncMock]:
    with patch(
        "meho_backplane.mcp.tools.operations.call_operation", new_callable=AsyncMock
    ) as mock_fn:

        async def _echo(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok", "op_id": arguments["op_id"], "duration_ms": 1.0, "result": {}}

        mock_fn.side_effect = _echo
        yield mock_fn
    _FLOORS.clear()


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize(("stored", "detail"), [("credential_read", "aggregate"), (None, "full")])
@pytest.mark.asyncio
async def test_outer_row_detail_comes_from_the_class_stored_on_the_inner_operation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
    stub_call_operation: AsyncMock,
    stub_embedding_service: AsyncMock,
    stored: str | None,
    detail: str,
) -> None:
    client, _operator = client_with_operator
    await ingest(stub_embedding_service, {GET_CLIENTS: stored} if stored else {})

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {
                    "connector_id": CONNECTOR_ID,
                    "op_id": GET_CLIENTS,
                    "params": {"q": MADE_UP_VALUE},
                },
            },
        },
    )

    assert response.json()["result"]["isError"] is False, response.text
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
                )
            )
            .scalars()
            .all()
        )
    (row,) = rows
    assert row.payload["op_id"] == "call_operation"
    assert row.payload["broadcast_detail_effective"] == detail
    assert MADE_UP_VALUE not in str(row.payload)
