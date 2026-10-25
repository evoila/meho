# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Result handles from an approval resume belong to the parking principal (#3548).

Principal A parks an op. A different principal B approves it, so the resume
runs under B. The op returns a large set, so the reducer spills it into a
result handle. ``GET /api/v1/approvals/{id}/result`` serves only A, so A must
be able to read that handle with ``result_query``. Before #3548 the handle was
stamped with B's ``sub`` and A got ``handle_not_found``.

The suite drives the real resume path, the real dispatcher, and the real
:class:`JsonFluxReducer`, with one in-memory store shared by the spill and the
``result_query`` read core.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    get_request,
    read_approval_result,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer, handle_owner_sub_var
from meho_backplane.operations.result_query import (
    ResultHandleNotFoundError,
    read_result_window,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003548")
_PARKER_SUB = "service:parker"
_APPROVER_SUB = "human:approver"
_OTHER_SUB = "human:bystander"

_CONNECTOR_ID = "vault-1.x"
_OP_ID = "vault.kv.list.bulk"
_ROWS = 60


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


class _FakeRedis:
    """In-memory async Valkey stand-in (mirrors the store's own test double)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def _spilling_reducer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a spilling reducer and point the read core at the same store."""
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))
    yield
    set_default_reducer(PassThroughReducer())


async def _set_shaped_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    return {"results": [{"k": f"k-{i}", "v": i} for i in range(_ROWS)]}


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture
async def _registered_set_shaped_op() -> AsyncIterator[None]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP_ID,
        handler=_set_shaped_handler,
        summary="List many secrets.",
        description="List many secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )
    yield


def _operator(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _park_and_approve(parker: Operator) -> UUID:
    params: dict[str, Any] = {"path": "/secret"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=parker,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        await s.commit()
        return request.id


@pytest.mark.usefixtures("_registered_set_shaped_op", "_spilling_reducer")
@pytest.mark.asyncio
async def test_parker_reads_handle_from_resume_approved_by_other_principal() -> None:
    """Park (A) → approve + resume (B) → A reads /result → A pages the handle; C misses."""
    parker = _operator(_PARKER_SUB, PrincipalKind.SERVICE)
    approver = _operator(_APPROVER_SUB)
    bystander = _operator(_OTHER_SUB)

    request_id = await _park_and_approve(parker)
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    result = await resume_dispatch_after_approval(operator=approver, request=row)
    assert result.status == "ok", f"resume failed: {result.error!r}"
    assert result.handle is not None, "a 60-row result must spill into a handle"

    async with get_sessionmaker()() as s:
        stored = await read_approval_result(s, operator=parker, request_id=request_id)
        await s.commit()
    assert stored.resume_result is not None
    handle_id = UUID(stored.resume_result["handle"]["handle_id"])

    window = await read_result_window(parker, handle_id, offset=0, limit=_ROWS)
    assert len(window["rows"]) == _ROWS

    with pytest.raises(ResultHandleNotFoundError):
        await read_result_window(bystander, handle_id)
    # The approver ran the op but did not ask for the data; the handle has one
    # owner, the parker.
    with pytest.raises(ResultHandleNotFoundError):
        await read_result_window(approver, handle_id)


@pytest.mark.usefixtures("_registered_set_shaped_op", "_spilling_reducer")
@pytest.mark.asyncio
async def test_owner_override_does_not_leak_past_the_resume() -> None:
    """After a resume, a normal dispatch is owned by its own caller again."""
    parker = _operator(_PARKER_SUB, PrincipalKind.SERVICE)
    approver = _operator(_APPROVER_SUB)

    request_id = await _park_and_approve(parker)
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    await resume_dispatch_after_approval(operator=approver, request=row)
    assert handle_owner_sub_var.get() is None

    direct = await dispatch(
        operator=approver,
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params={"path": "/secret"},
    )
    assert direct.status == "ok", f"dispatch failed: {direct.error!r}"
    assert direct.handle is not None
    window = await read_result_window(approver, direct.handle.handle_id, offset=0, limit=_ROWS)
    assert len(window["rows"]) == _ROWS
    with pytest.raises(ResultHandleNotFoundError):
        await read_result_window(parker, direct.handle.handle_id)
