# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A ``targets.register`` or ``targets.update`` call that the model refuses.

Through ``call_operation`` and through the MCP tools ``meho_targets_register``
and ``meho_targets_update``:

* the call is refused at once, before it can wait: no approval request is
  stored and no target changes;
* the answer names the field and the rule;
* a made-up value of the sent ``extras`` is in none of the answer, the
  stored approval requests (params and result) and the audit rows.

A direct handler call (as after an approval) gets the same kind of text.

Every test value is made up at run time.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    register_targets_registry_operations,
    targets_register,
)
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    register_targets_update_operation,
    targets_update,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.targets.extras_limits import EXTRAS_MAX_DEPTH
from tests._targets_helpers import _insert_target
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

from .test_targets_schemas import _VALID_CA_PEM

_TOOLS = {
    TARGETS_REGISTER_OP_ID: "meho_targets_register",
    TARGETS_UPDATE_OP_ID: "meho_targets_update",
}
_DEEP_REASON = f"extras may be at most {EXTRAS_MAX_DEPTH} levels deep"
_PIN_REASON = "tls_ca_pin and verify_tls=false are mutually exclusive"
_BAD_PIN_REASON = "tls_ca_pin is not a valid PEM certificate bundle"
_CASES = ["too-deep-extras", "model-rule", "non-ascii-pin"]


def _agent() -> Operator:
    return Operator(
        sub="agent:refused",
        name="Refused",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.AGENT,
        scopes=frozenset({"mcp:admin"}),
    )


@pytest_asyncio.fixture(autouse=True)
async def _registered_ops(required_settings_env: None) -> AsyncIterator[None]:  # noqa: F811
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_registry_operations(embedding_service=service)
    await register_targets_update_operation(embedding_service=service)
    yield


@pytest_asyncio.fixture
async def _target() -> AsyncIterator[TargetORM]:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield await _insert_target(
        tenant_id=OPERATOR_TENANT_ID,
        name="web-01",
        product="ssh",
        host="203.0.113.5",
        extras={"a": 1},
    )


@pytest.fixture
def _mcp_client() -> Iterator[TestClient]:
    operator = _agent()

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _too_deep(value: str) -> dict[str, Any]:
    nested: dict[str, Any] = {"inner_key": 1}
    for level in range(EXTRAS_MAX_DEPTH + 1):
        nested = {f"level_{level}": nested}
    # The made-up value is last: the end of a long input is what a shortened
    # copy of the input keeps.
    return {**nested, "made_key": value}


def _params(op_id: str, case: str, made: str, target: TargetORM) -> tuple[dict[str, Any], str]:
    """Params the model refuses, each with the made-up value in the sent fields."""
    if case == "too-deep-extras":
        fields: dict[str, Any] = {"extras": _too_deep(made)}
        reason = _DEEP_REASON
    elif case == "non-ascii-pin":
        fields = {"tls_ca_pin": f"caf\u00e9 {made}"}
        reason = _BAD_PIN_REASON
    else:
        fields = {
            "tls_ca_pin": _VALID_CA_PEM,
            "verify_tls": False,
            "extras": {"first_key": 1, "api_password": made},
        }
        reason = _PIN_REASON
    if op_id == TARGETS_REGISTER_OP_ID:
        return {"name": "new-01", "product": "ssh", "host": "93.184.216.34", **fields}, reason
    return {"target_id": str(target.id), **fields}, reason


async def _stored_text() -> str:
    """Every column of every approval request and audit row."""
    async with get_sessionmaker()() as session:
        rows = [
            *(await session.execute(select(ApprovalRequest))).scalars().all(),
            *(await session.execute(select(AuditLog))).scalars().all(),
        ]
    return " ".join(repr(getattr(r, c.key)) for r in rows for c in r.__table__.columns)


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        return len((await session.execute(select(ApprovalRequest))).scalars().all())


async def _assert_nothing_stored(made: str, target: TargetORM) -> None:
    assert await _approval_count() == 0
    assert made not in await _stored_text()
    async with get_sessionmaker()() as session:
        names = (await session.execute(select(TargetORM.name))).scalars().all()
        row = await session.get(TargetORM, target.id)
    assert sorted(names) == ["web-01"]
    assert row is not None
    assert row.extras == {"a": 1}


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("op_id", [TARGETS_REGISTER_OP_ID, TARGETS_UPDATE_OP_ID])
async def test_call_operation_refuses_at_once_without_the_value(
    op_id: str, case: str, _target: TargetORM
) -> None:
    made = f"v{secrets.token_hex(4)}"
    params, reason = _params(op_id, case, made, _target)

    answer = await call_operation(
        _agent(),
        {"connector_id": TARGETS_REGISTRY_CONNECTOR_ID, "op_id": op_id, "params": params},
    )

    assert answer["status"] == "error", answer
    assert answer["extras"]["error_code"] == "invalid_params", answer
    text = json.dumps(answer, default=str)
    assert reason in text
    assert made not in text
    await _assert_nothing_stored(made, _target)


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("op_id", [TARGETS_REGISTER_OP_ID, TARGETS_UPDATE_OP_ID])
async def test_the_mcp_tool_refuses_at_once_without_the_value(
    op_id: str, case: str, _target: TargetORM, _mcp_client: TestClient
) -> None:
    made = f"v{secrets.token_hex(4)}"
    params, reason = _params(op_id, case, made, _target)

    response = _mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _TOOLS[op_id], "arguments": params},
        },
    )

    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS, body
    assert reason in body["error"]["message"]
    assert made not in response.text
    await _assert_nothing_stored(made, _target)


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("op_id", [TARGETS_REGISTER_OP_ID, TARGETS_UPDATE_OP_ID])
async def test_the_handler_names_the_field_never_the_value(
    op_id: str, case: str, _target: TargetORM
) -> None:
    made = f"v{secrets.token_hex(4)}"
    params, reason = _params(op_id, case, made, _target)
    handler = targets_register if op_id == TARGETS_REGISTER_OP_ID else targets_update

    with pytest.raises(ValueError, match="invalid target parameters") as caught:
        await handler(_agent(), None, params)

    assert reason in str(caught.value)
    assert made not in str(caught.value)
    assert caught.value.__cause__ is None
