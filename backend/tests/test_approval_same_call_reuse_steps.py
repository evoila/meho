# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step of a multi-step operation never shares a waiting request with a direct call.

With ``APPROVAL_SAME_CALL_REUSE`` on, a direct call gets the waiting request
of the same call. A step that a multi-step operation sends through
``dispatch_child`` is not a direct call: approving its request runs that
step. So the step always makes its own request, and a direct call never gets
the request of such a step. Both orders go through the real ``dispatch``
path. A retry of the same step with the same key still gets its request back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import (
    DispatchChild,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.operations.composite import composite_depth_var
from meho_backplane.settings import get_settings
from tests.test_operations_composite import (
    _FakeTarget,
    _insert_composite_descriptor,
    _make_operator,
    _NoOpVaultConnector,
)

_CONNECTOR_ID = "vault-1.x"
_STEP_OP_ID = "vault.kv.write_waits"
_COMPOSITE_OP_ID = "vault.composite.one_waiting_step"
_STEP_PARAMS = {"name": "vm-1"}

#: The answer the step got inside the multi-step operation.
_step_results: list[OperationResult] = []


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_SAME_CALL_REUSE", "true")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    _step_results.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, uuid.UUID]]:
    events: list[tuple[str, uuid.UUID]] = []

    async def _record(**kwargs: Any) -> None:
        events.append((kwargs["decision"], kwargs["request"].id))

    monkeypatch.setattr(approval_queue_module, "record_approval_step_event", _record)
    return events


async def _step_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"written": params}


async def _one_waiting_step_composite(
    operator: Any, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """Send one step that waits for approval, and return its answer."""
    result = await dispatch_child(
        connector_id=_CONNECTOR_ID, op_id=_STEP_OP_ID, params=dict(_STEP_PARAMS)
    )
    _step_results.append(result)
    return result


async def _register_ops() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_STEP_OP_ID,
        handler=_step_handler,
        summary="Write a thing.",
        description="Write a thing; waits for approval.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        requires_approval=True,
        embedding_service=embedding,
    )
    async with get_sessionmaker()() as session:
        await _insert_composite_descriptor(
            session=session,
            op_id=_COMPOSITE_OP_ID,
            handler_ref="tests.test_approval_same_call_reuse_steps._one_waiting_step_composite",
            embedding=[0.1] * 384,
        )


async def _direct(target: _FakeTarget) -> OperationResult:
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_STEP_OP_ID,
        target=target,
        params=dict(_STEP_PARAMS),
    )


async def _multi_step(target: _FakeTarget) -> OperationResult:
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP_ID,
        target=target,
        params={},
    )
    assert result.status == "awaiting_approval", result.error
    assert len(_step_results) == 1
    return _step_results[0]


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await s.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_a_step_after_a_waiting_direct_call_gets_its_own_request(
    recorded: list[tuple[str, uuid.UUID]],
) -> None:
    await _register_ops()
    target = _FakeTarget(product="vault")

    direct = await _direct(target)
    step = await _multi_step(target)

    assert direct.status == "awaiting_approval", direct.error
    assert step.status == "awaiting_approval", step.error
    direct_id = uuid.UUID(direct.extras["approval_request_id"])
    step_id = uuid.UUID(step.extras["approval_request_id"])
    assert step_id != direct_id
    assert "approval_request_reused" not in step.extras
    rows = await _requests()
    assert [row.id for row in rows] == [direct_id, step_id]
    assert [bool(row.composite_step) for row in rows] == [False, True]
    assert recorded == [("pending", direct_id), ("pending", step_id)]


@pytest.mark.asyncio
async def test_a_direct_call_after_a_waiting_step_gets_its_own_request(
    recorded: list[tuple[str, uuid.UUID]],
) -> None:
    await _register_ops()
    target = _FakeTarget(product="vault")

    step = await _multi_step(target)
    direct = await _direct(target)

    assert step.status == "awaiting_approval", step.error
    assert direct.status == "awaiting_approval", direct.error
    step_id = uuid.UUID(step.extras["approval_request_id"])
    direct_id = uuid.UUID(direct.extras["approval_request_id"])
    assert direct_id != step_id
    assert "approval_request_reused" not in direct.extras
    assert len(await _requests()) == 2
    assert recorded == [("pending", step_id), ("pending", direct_id)]


async def _park_step_with_key(key: str) -> ApprovalRequest:
    depth_token = composite_depth_var.set(1)
    try:
        async with get_sessionmaker()() as s:
            request = await create_pending_request(
                s,
                operator=_make_operator(),
                connector_id=_CONNECTOR_ID,
                op_id=_STEP_OP_ID,
                target=None,
                params=dict(_STEP_PARAMS),
                params_hash=compute_params_hash(_STEP_PARAMS),
                idempotency_key=key,
                proposed_effect={"safety_level": "safe"},
            )
            await s.commit()
    finally:
        composite_depth_var.reset(depth_token)
    return request


@pytest.mark.asyncio
async def test_a_retry_of_the_same_step_with_the_same_key_gets_its_request() -> None:
    first = await _park_step_with_key("step-key")
    retry = await _park_step_with_key("step-key")

    assert retry.id == first.id
    assert getattr(retry, "_reused", False) is True
    assert first.composite_step is True
    assert len(await _requests()) == 1
