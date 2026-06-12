# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit ``work_ref`` persistence tests (work_ref I1-T1 #1655).

Proves the three primary audit writers stamp ``audit_log.work_ref`` from
the shared :data:`meho_backplane.operations._audit.work_ref_var`
ContextVar: present when a ``work_ref`` is bound in scope, ``NULL``
otherwise. The bind *source* (request transport / agent loop) is a
separate task (I1-T2), so these tests bind the var directly -- the same
shape the eventual binder will use.

The three primary writers (mirroring the ``actor_sub`` #816 coverage):

* chassis HTTP -- :func:`meho_backplane.audit._write_audit_row`
* dispatcher DISPATCH -- ``meho_backplane.operations.dispatch`` →
  :func:`meho_backplane.operations._audit.write_audit_row`
* MCP -- :func:`meho_backplane.mcp.audit.write_mcp_audit_row`

The ~8 system-internal writers (memory / topology / reaper / ui-session)
legitimately leave the column ``NULL`` and are explicitly out of scope.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit import _write_audit_row
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORK_REF = "gh:evoila/meho#1"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires (conftest provides the DB)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear connector + dispatcher caches around each test."""
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test for direct row inspection."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Embedding stub for the typed-op descriptor's embedding column."""
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _operator() -> Operator:
    """Test operator in tenant A."""
    return Operator(
        sub="alice@example.com",
        name="Alice",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )


def _chassis_kwargs(audit_id: uuid.UUID) -> dict[str, object]:
    """Minimal kwargs for the chassis writer :func:`_write_audit_row`."""
    return {
        "audit_id": audit_id,
        "operator_sub": "alice@example.com",
        "tenant_id": _TENANT_A,
        "target_id": None,
        "method": "POST",
        "path": "/api/v1/targets",
        "status_code": 200,
        "request_id": None,
        "duration_ms": 1.0,
        "payload": {},
    }


class _StubConnector(Connector):
    """Minimal connector so the dispatcher resolver finds a class for the triple."""

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: return a trivial result."""
    return {"ok": True}


async def _seed_typed_op(stub_embedding_service: AsyncMock) -> None:
    """Register the stub connector + typed op the dispatcher can find."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id="stub.op_call",
        handler=_echo_handler,
        summary="Stub op for work_ref tests.",
        description="Echo a result; used to assert the audit row's work_ref.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def _read_row(db_session: AsyncSession, audit_id: uuid.UUID) -> AuditLog:
    """Fetch one audit row by id."""
    result = await db_session.execute(select(AuditLog).where(AuditLog.id == audit_id))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# work_ref bound → every primary writer stamps the column
# ---------------------------------------------------------------------------


async def test_work_ref_var_stamped(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """With ``work_ref_var`` bound, all 3 primary writers stamp ``work_ref``.

    Drives the chassis HTTP writer, the dispatcher DISPATCH writer (via
    ``dispatch``), and the MCP writer in turn under one binding, then
    asserts each produced ``audit_log`` row carries
    ``work_ref == "gh:evoila/meho#1"``.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    chassis_id = uuid.uuid4()
    token = work_ref_var.set(_WORK_REF)
    try:
        # 1. chassis HTTP writer.
        await _write_audit_row(**_chassis_kwargs(chassis_id))  # type: ignore[arg-type]

        # 2. dispatcher DISPATCH writer (one row per dispatch).
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.op_call",
            target=None,
            params={},
        )

        # 3. MCP writer (returns the audit id it used).
        mcp_id = await write_mcp_audit_row(
            operator=operator,
            method="MCP",
            path="/mcp/tools/call/stub.op_call",
            status_code=200,
            duration_ms=1.0,
            payload={"op_id": "stub.op_call"},
        )
    finally:
        work_ref_var.reset(token)

    chassis_row = await _read_row(db_session, chassis_id)
    assert chassis_row.work_ref == _WORK_REF  # type: ignore[attr-defined]

    mcp_row = await _read_row(db_session, mcp_id)
    assert mcp_row.work_ref == _WORK_REF  # type: ignore[attr-defined]

    dispatch_rows = (
        await db_session.scalars(select(AuditLog).where(AuditLog.method == "DISPATCH"))
    ).all()
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# work_ref unset → every primary writer leaves the column NULL
# ---------------------------------------------------------------------------


async def test_work_ref_null_when_unset(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """With no ``work_ref_var`` binding, all 3 primary writers leave it NULL.

    Regression guard: operations outside a change-ticket scope (and the
    system-internal writers) must not gain a spurious ``work_ref``.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    # No work_ref binding in scope.
    assert work_ref_var.get() is None

    chassis_id = uuid.uuid4()
    await _write_audit_row(**_chassis_kwargs(chassis_id))  # type: ignore[arg-type]

    await dispatch(
        operator=operator,
        connector_id="stub-1.x",
        op_id="stub.op_call",
        target=None,
        params={},
    )

    mcp_id = await write_mcp_audit_row(
        operator=operator,
        method="MCP",
        path="/mcp/tools/call/stub.op_call",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "stub.op_call"},
    )

    chassis_row = await _read_row(db_session, chassis_id)
    assert chassis_row.work_ref is None  # type: ignore[attr-defined]

    mcp_row = await _read_row(db_session, mcp_id)
    assert mcp_row.work_ref is None  # type: ignore[attr-defined]

    dispatch_rows = (
        await db_session.scalars(select(AuditLog).where(AuditLog.method == "DISPATCH"))
    ).all()
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0].work_ref is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Pure unit test -- the contextvar set/reset contract
# ---------------------------------------------------------------------------


def test_work_ref_var_resets_cleanly() -> None:
    """set → non-None → reset → None; no dispatch, no DB.

    Validates the set/reset token contract so the I1-T2 binder's
    bind-around-request pattern is sound.
    """
    assert work_ref_var.get() is None

    token = work_ref_var.set(_WORK_REF)
    assert work_ref_var.get() == _WORK_REF

    work_ref_var.reset(token)
    assert work_ref_var.get() is None
