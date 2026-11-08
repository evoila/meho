# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``server_time`` on ``meho_status`` and ``GET /api/v1/health`` (#5504).

An agent with no shell has no clock. ``meho_status`` gives it the server's
time now, in UTC, as ISO 8601 to the second with a ``Z`` suffix.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from meho_backplane.api.v1.health import HealthResponse, build_health_response
from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _assert_server_time_now(value: object, before: datetime, after: datetime) -> None:
    """*value* is a UTC ISO 8601 time, to the second, taken between the two times."""
    assert isinstance(value, str)
    assert _UTC_SECONDS.match(value), value
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert before.replace(microsecond=0) <= parsed <= after


@pytest.fixture
def status_client() -> Iterator[TestClient]:
    operator = build_operator()

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def test_meho_status_returns_server_time_in_utc(status_client: TestClient) -> None:
    client = status_client
    before = datetime.now(UTC)
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
    )
    after = datetime.now(UTC)

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    _assert_server_time_now(payload["server_time"], before, after)


async def test_server_time_is_set_for_a_login_that_chose_no_tenant() -> None:
    """The short answer for a login with several tenants carries the time too."""
    tenant_a = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    tenant_b = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    operator = Operator(
        sub="person-1",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_a,
        tenant_role=TenantRole.READ_ONLY,
        tenants=(
            TenantMembership(tenant_id=tenant_a, name="tenant-a", group=RoleGroup.READERS),
            TenantMembership(tenant_id=tenant_b, name="tenant-b", group=RoleGroup.OPERATORS),
        ),
        tenant_chosen=False,
    )
    before = datetime.now(UTC)
    response = await build_health_response(operator)
    after = datetime.now(UTC)

    assert response.vault.detail == "tenant_not_chosen"
    _assert_server_time_now(response.model_dump(mode="json")["server_time"], before, after)


def test_an_answer_without_server_time_still_decodes() -> None:
    """An older server sends no ``server_time``; the field reads as missing."""
    response = HealthResponse.model_validate(
        {
            "operator": {
                "sub": "op-1",
                "name": None,
                "email": None,
                "tenant_id": None,
                "tenant_role": None,
            },
            "vault": {"reachable": False, "read_ok": False},
            "db": {"migrated": None},
            "mcp_session_id_capture": "when_negotiated",
        }
    )
    assert response.server_time is None
