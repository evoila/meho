# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The outer MCP row of ``call_operation`` reads the class stored on the inner operation.

The wrapper row and the broadcast detail of ``call_operation`` come from the
operation inside. When that operation's row stores a secret class (a connector
floor set it), the detail is the aggregate only, as for an op id in a secret
class. Without a stored class the detail stays full.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.operations.ingest.safety_floors import _FLOORS
from meho_backplane.operations.op_class_of import note_ran_op_class
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


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_outer_row_detail_comes_from_the_row_the_dispatcher_ran(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
    stub_call_operation: AsyncMock,
    stub_embedding_service: AsyncMock,
) -> None:
    """The named row stores no class; the row the dispatcher picked stores a secret class."""
    client, _operator = client_with_operator
    await ingest(stub_embedding_service, {})

    async def _ran_another_row(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        note_ran_op_class("credential_read")
        return {"status": "ok", "op_id": arguments["op_id"], "duration_ms": 1.0, "result": {}}

    stub_call_operation.side_effect = _ran_another_row

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
        (row,) = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
                )
            )
            .scalars()
            .all()
        )
    assert row.payload["broadcast_detail_effective"] == "aggregate"
    assert MADE_UP_VALUE not in str(row.payload)


async def _store_class_on_another_version(op_class: str | None) -> None:
    """Copy the ``GET`` row to version 2.0 of the same catalog, with *op_class* stored."""
    async with get_sessionmaker()() as session, session.begin():
        (named,) = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id == GET_CLIENTS)
                )
            )
            .scalars()
            .all()
        )
        values = {
            column.key: getattr(named, column.key)
            for column in inspect(EndpointDescriptor).column_attrs
            if column.key != "id"
        }
        session.add(EndpointDescriptor(**{**values, "version": "2.0", "op_class": op_class}))


def _pending(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """A background call or a check that has no result yet: no row was picked here."""
    return {
        "status": "pending",
        "op_id": GET_CLIENTS,
        "duration_ms": 1.0,
        "result": None,
        "extras": {"run_id": str(uuid.uuid4())},
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("control", [{"async": True}, {"run_id": "check"}])
@pytest.mark.parametrize(("stored", "detail"), [("credential_read", "aggregate"), (None, "full")])
@pytest.mark.asyncio
async def test_background_start_and_check_read_the_class_of_every_version(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
    stub_call_operation: AsyncMock,
    stub_embedding_service: AsyncMock,
    control: dict[str, Any],
    stored: str | None,
    detail: str,
) -> None:
    """Only version 2.0 stores a secret class: the dispatcher may pick it for the target."""
    client, _operator = client_with_operator
    await ingest(stub_embedding_service, {})
    await _store_class_on_another_version(stored)
    if control.get("run_id"):
        control = {"run_id": str(uuid.uuid4())}

    with (
        patch(
            "meho_backplane.mcp.tools.operations.submit_async_call",
            new=AsyncMock(side_effect=_pending),
        ),
        patch("meho_backplane.mcp.tools.operations.poll_call", new=AsyncMock(side_effect=_pending)),
    ):
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
                        **control,
                    },
                },
            },
        )

    assert response.json()["result"]["isError"] is False, response.text
    stub_call_operation.assert_not_called()
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
    (row,) = [row for row in rows if row.payload.get("op_id") == "call_operation"]
    assert row.payload["broadcast_detail_effective"] == detail
    if stored is not None:
        assert MADE_UP_VALUE not in str(row.payload)
