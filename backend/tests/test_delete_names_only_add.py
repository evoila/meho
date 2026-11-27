# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the install's delete names only add to the built-in ones.

* The setting ``SERVICE_GRANT_DELETE_SHAPED_PATTERNS`` never removes a
  built-in delete name with the switch on, also when it is empty.
* A service login calls an operation with a built-in delete name over MCP,
  under a standing grant, with a setting that lists other names: with the
  switch on the call waits for a person, and the audit row says so.
* With the switch off, a set value is the whole list.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, ServicePrincipalGrant, Tenant
from meho_backplane.delete_names import DELETE_SHAPED_OP_ID_PATTERNS
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS, get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OTHER_NAMES = "acme.custom.nuke, POST:/acme/*/wipe"
_FIX = {"product": "dnfix", "version": "1.x", "impl_id": "dnfix"}
_CONNECTOR = "dnfix-1.x"
#: A ``caution`` operation whose only delete sign is the built-in ``*.delete``.
_DELETE_OP = "dnfix.vm.delete"
_SERVICE_SUB = "svc:delete-names"


def _env(monkeypatch: pytest.MonkeyPatch, *, switch_on: bool, names: str) -> None:
    if switch_on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", names)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [_OTHER_NAMES, "", " , "], ids=["other", "empty", "blank"])
def test_switch_on_keeps_every_built_in_delete_name(
    monkeypatch: pytest.MonkeyPatch, names: str
) -> None:
    _env(monkeypatch, switch_on=True, names=names)

    patterns = get_settings().service_grant_delete_shaped_patterns

    assert set(DELETE_SHAPED_OP_ID_PATTERNS) <= set(patterns)
    assert patterns[: len(_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS)] == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


def test_switch_on_adds_the_listed_names_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, switch_on=True, names=f"{_OTHER_NAMES}, *.delete, acme.custom.nuke")

    assert get_settings().service_grant_delete_shaped_patterns == (
        *_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS,
        "acme.custom.nuke",
        "POST:/acme/*/wipe",
    )


def test_switch_off_the_setting_replaces_the_built_in_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, switch_on=False, names=_OTHER_NAMES)

    assert get_settings().service_grant_delete_shaped_patterns == (
        "acme.custom.nuke",
        "POST:/acme/*/wipe",
    )


def test_switch_off_an_empty_setting_keeps_the_built_in_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, switch_on=False, names="")

    assert get_settings().service_grant_delete_shaped_patterns == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


# ---------------------------------------------------------------------------
# A whole call: a service login over MCP, under a standing grant
# ---------------------------------------------------------------------------


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"deleted": True}


@pytest_asyncio.fixture
async def _granted_delete_op(embedding: AsyncMock) -> AsyncIterator[None]:
    """The ``caution`` delete operation, and a standing grant on it for the service login.

    The grant row is stored directly: the grant route refuses a delete, so
    the row stands for a grant made before the install changed its list.
    """
    await register_typed_operation(
        **_FIX,
        op_id=_DELETE_OP,
        handler=_handler,
        summary="Fixture delete.",
        description="Fixture delete.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="vm",
        when_to_use="Fixture group.",
        safety_level="caution",
        embedding_service=embedding,
    )
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="dn-tenant", name="Delete Names Tenant"))
        await session.flush()
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=OPERATOR_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                op_id=_DELETE_OP,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="fixture: standing grant",
                created_by_sub="fixture-admin",
            )
        )
    yield


@pytest.fixture
def _service_client() -> Iterator[TestClient]:
    async def _fake_verify() -> Operator:
        return Operator(
            sub=_SERVICE_SUB,
            name="Delete names",
            email=None,
            raw_jwt="fixture-jwt-not-real",
            tenant_id=OPERATOR_TENANT_ID,
            tenant_role=TenantRole.OPERATOR,
            principal_kind=PrincipalKind.SERVICE,
        )

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call(client: TestClient) -> dict[str, Any]:
    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {"connector_id": _CONNECTOR, "op_id": _DELETE_OP, "params": {}},
            },
        },
    ).json()
    content: dict[str, Any] = body["result"]["structuredContent"]
    return content


async def _waiting_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ApprovalRequest.op_id)))


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in rows if row.payload.get("op_id") == _DELETE_OP]


@pytest.mark.usefixtures("_granted_delete_op")
async def test_switch_on_a_built_in_delete_name_waits_despite_a_grant(
    monkeypatch: pytest.MonkeyPatch, _service_client: TestClient
) -> None:
    _env(monkeypatch, switch_on=True, names=_OTHER_NAMES)

    content = _call(_service_client)

    assert content["status"] == "awaiting_approval", content
    assert await _waiting_ops() == [_DELETE_OP]
    rows = await _audit_rows()
    assert [(row.path, row.status_code) for row in rows] == [("approval.request", 202)]
    assert rows[0].payload["principal_sub"] == _SERVICE_SUB


@pytest.mark.usefixtures("_granted_delete_op")
async def test_switch_off_the_same_grant_lets_the_call_run(
    monkeypatch: pytest.MonkeyPatch, _service_client: TestClient
) -> None:
    """The control case: with the switch off the setting replaced ``*.delete``."""
    _env(monkeypatch, switch_on=False, names=_OTHER_NAMES)

    content = _call(_service_client)

    assert content["status"] == "ok", content
    assert await _waiting_ops() == []
    rows = await _audit_rows()
    assert any(row.path == "approval.decision" for row in rows)
