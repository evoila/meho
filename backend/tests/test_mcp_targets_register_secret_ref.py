# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_targets_register`` shows the stored ``secret_ref`` and whether MEHO built it.

A target added without a ``secret_ref`` gets ``tenants/<tenant_id>/<name>``;
the answer then has ``secret_ref_derived: true``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.schemas import TARGETS_REGISTER_RESPONSE_SCHEMA
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


def _register(client: TestClient, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_targets_register", "arguments": arguments},
        },
    ).json()["result"]


def test_register_response_schema_requires_secret_ref_fields() -> None:
    assert {"secret_ref", "secret_ref_derived"} <= set(TARGETS_REGISTER_RESPONSE_SCHEMA["required"])


@pytest.mark.usefixtures("person_token_writes_run_at_once")
@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.parametrize(
    ("extra", "expected_ref", "expected_derived"),
    [
        ({}, f"tenants/{OPERATOR_TENANT_ID}/linux-01", True),
        (
            {"secret_ref": f"tenants/{OPERATOR_TENANT_ID}/linux"},
            f"tenants/{OPERATOR_TENANT_ID}/linux",
            False,
        ),
        ({"secret_ref": None}, None, False),
    ],
    ids=["omitted", "set", "null"],
)
async def test_mcp_register_shows_secret_ref_and_derived(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
    extra: dict[str, Any],
    expected_ref: str | None,
    expected_derived: bool,
) -> None:
    client, _op = client_with_operator
    result = _register(
        client, {"name": "linux-01", "product": "vault", "host": "vault.example", **extra}
    )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["secret_ref"] == expected_ref
    assert payload["secret_ref_derived"] is expected_derived
