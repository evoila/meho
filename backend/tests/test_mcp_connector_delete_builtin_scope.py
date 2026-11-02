# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""``meho_connector_delete`` on the built-in scope needs ``platform_admin``.

A built-in connector's rows (``tenant_id IS NULL``) are shared by every
tenant. The MCP tool reaches them with ``tenant_id`` omitted, through
the real :class:`~meho_backplane.operations.ingest.ReviewService`. A
tenant_admin without ``platform_admin`` gets the same structured
``builtin_connector_write_forbidden`` refusal as ``meho_connector_enable``,
and no row changes; a platform_admin deletes the rows.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ensure_connector_class_registered
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_PRODUCT = "builtinprobe"
_VERSION = "1.0"
_IMPL_ID = "builtinprobe-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"


async def _seed_builtin_rows() -> None:
    """One built-in group with two operations, used by every tenant."""
    assert ensure_connector_class_registered(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        base_url=None,
    )
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="reads",
                name="Reads",
                when_to_use="Use for reads.",
                review_status="enabled",
            ),
        )
        for index in range(2):
            session.add(
                EndpointDescriptor(
                    tenant_id=None,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"GET:/api/v1/things/{index}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/v1/things/{index}",
                    summary="Summary",
                    description="Description",
                    group_id=group_id,
                    tags=["test"],
                    parameter_schema={"type": "object"},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                ),
            )
        await session.commit()


async def _builtin_row_counts() -> tuple[int, int]:
    async with get_sessionmaker()() as session:
        groups = await session.scalar(
            select(func.count())
            .select_from(OperationGroup)
            .where(OperationGroup.impl_id == _IMPL_ID, OperationGroup.tenant_id.is_(None))
        )
        ops = await session.scalar(
            select(func.count())
            .select_from(EndpointDescriptor)
            .where(EndpointDescriptor.impl_id == _IMPL_ID, EndpointDescriptor.tenant_id.is_(None))
        )
        return int(groups or 0), int(ops or 0)


def _delete_call(client: TestClient) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho_connector_delete",
                "arguments": {"connector_id": _CONNECTOR_ID},
            },
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize(
    "client_with_operator",
    [(TenantRole.TENANT_ADMIN, False)],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tenant_admin_cannot_delete_builtin_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    await _seed_builtin_rows()
    client, _op = client_with_operator

    body = _delete_call(client)

    assert body["error"]["code"] == -32602
    assert body["error"]["data"]["error"] == "builtin_connector_write_forbidden"
    assert body["error"]["data"]["connector_id"] == _CONNECTOR_ID
    assert "BuiltinConnectorWriteForbiddenError" not in json.dumps(body)
    assert await _builtin_row_counts() == (1, 2)


@pytest.mark.parametrize(
    "client_with_operator",
    [(TenantRole.TENANT_ADMIN, True)],
    indirect=True,
)
@pytest.mark.asyncio
async def test_platform_admin_deletes_builtin_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    await _seed_builtin_rows()
    client, _op = client_with_operator

    body = _delete_call(client)

    [content] = body["result"]["content"]
    payload = json.loads(content["text"])
    assert payload["ok"] is True
    assert payload["deleted"]["operations_deleted"] == 2
    assert await _builtin_row_counts() == (0, 0)
