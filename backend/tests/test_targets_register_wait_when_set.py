# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``targets.register`` with a secret field map or a capture setting waits for a person.

A service login whose standing grant covers ``targets.register`` runs a
plain call at once. A call that sets ``secret_field_map`` or
``flight_recorder_capture`` waits for a person's approval on every call:
the standing grant does not cover it. Nothing is written until then.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ServicePrincipalGrant, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.op_classes import waits_when_set
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_SERVICE_SUB = "svc:onboarding"


def _service_operator() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        name="Onboarding",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.SERVICE,
        scopes=frozenset({"mcp:admin"}),
    )


@pytest_asyncio.fixture
async def _granted() -> AsyncIterator[None]:
    """One tenant and a standing grant on ``targets.register`` for the service login."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
        await session.flush()
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=OPERATOR_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                op_id=TARGETS_REGISTER_OP_ID,
                connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
                target_id=None,
                reason="fixture: onboarding service registers targets",
                created_by_sub="fixture-admin",
            )
        )
    yield


@pytest.fixture
def _service_client() -> Iterator[TestClient]:
    async def _fake_verify() -> Operator:
        return _service_operator()

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _register(client: TestClient, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_targets_register", "arguments": arguments},
        },
    ).json()


async def _target_names() -> list[str]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(TargetORM.name)))


async def _waiting_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ApprovalRequest.op_id)))


_NEW = {"name": "new-vc", "product": "vault", "host": "vault-c.example"}


async def test_a_granted_call_without_the_fields_runs_at_once(
    _service_client: TestClient, _granted: None
) -> None:
    body = _register(_service_client, _NEW)

    result = body["result"]
    assert result["isError"] is False, body
    assert result["structuredContent"].get("status") != "awaiting_approval"
    assert await _target_names() == ["new-vc"]
    assert await _waiting_ops() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"secret_field_map": {"username": "user"}},
        {"flight_recorder_capture": True},
        {"flight_recorder_capture": False},
    ],
    ids=["secret-field-map", "capture-on", "capture-off"],
)
async def test_a_granted_call_with_the_fields_waits_for_a_person(
    _service_client: TestClient, _granted: None, extra: dict[str, Any]
) -> None:
    body = _register(_service_client, {**_NEW, **extra})

    result = body["result"]
    assert result["structuredContent"]["status"] == "awaiting_approval", body
    assert await _target_names() == []
    assert await _waiting_ops() == [TARGETS_REGISTER_OP_ID]


@pytest.mark.parametrize(
    ("params", "waits"),
    [
        ({}, False),
        ({"secret_field_map": None, "flight_recorder_capture": None}, False),
        ({"secret_field_map": {}}, False),
        ({"secret_field_map": {"username": "user"}}, True),
        ({"flight_recorder_capture": False}, True),
    ],
)
def test_only_a_set_field_makes_the_call_wait(params: dict[str, Any], waits: bool) -> None:
    assert waits_when_set(TARGETS_REGISTER_OP_ID, {**_NEW, **params}) is waits
