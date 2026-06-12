# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbook contextvar plumbing (G12.1-T2 #1294).

Acceptance criteria covered:

* ``test_audit_row_carries_run_step_when_contextvars_set`` -- bind
  ``run_id_var`` + ``step_id_var``, dispatch a safe op, fetch the
  resulting ``audit_log`` row, assert ``row.run_id == <uuid>`` and
  ``row.step_id == "rotate-creds"``. Skipped when the DB schema
  predates migration ``0034`` (G12.1-T1 #1292) -- the columns are
  wired by this module but only present after T1 merges.
* ``test_audit_row_null_when_contextvars_unset`` -- dispatch with no
  contextvar binding, assert ``row.run_id is None`` and
  ``row.step_id is None``. Same skip guard as the prior test.
* ``test_audit_payload_mirrors_run_step`` -- bind contextvars,
  dispatch, assert ``row.payload["run_id"] == str(<uuid>)`` and
  ``row.payload["step_id"] == "rotate-creds"``. No DB column
  required -- always runs.
* ``test_audit_payload_omits_run_step_when_unset`` -- dispatch with
  no contextvars, assert ``"run_id" not in row.payload`` and
  ``"step_id" not in row.payload``. Always runs.
* ``test_contextvar_resets_cleanly`` -- pure unit test: ``set`` →
  assert non-None → ``reset`` → assert ``.get() is None``. No
  dispatch, no DB. Always runs.

The tests exercise the dispatcher's audit-write path directly by
binding the new contextvars (the same shape the G12.3 step-execution
engine will use) and seeding a typed operation; this isolates the
propagation contract from the runbook engine's implementation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import run_id_var, step_id_var
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")

# Guard: skip DB-column assertions when the schema predates migration 0034.
# Once G12.1-T1 (#1292) merges and the column exists on AuditLog, the
# skip guard becomes False and both tests run.
_COLUMNS_PRESENT = hasattr(AuditLog, "run_id") and hasattr(AuditLog, "step_id")
_skip_if_no_columns = pytest.mark.skipif(
    not _COLUMNS_PRESENT,
    reason="audit_log.run_id / .step_id columns added by migration 0034 (G12.1-T1 #1292); "
    "skip until that PR merges into main.",
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires."""
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


class _StubConnector(Connector):
    """Minimal connector so dispatcher resolver finds a class for the triple."""

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
    """Typed handler: return params verbatim."""
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
        summary="Stub op for runbook-correlation tests.",
        description="Echo params; used to assert the audit row's run_id / step_id.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


# ---------------------------------------------------------------------------
# DB column assertions (skipped until migration 0034 lands)
# ---------------------------------------------------------------------------


@_skip_if_no_columns
async def test_audit_row_carries_run_step_when_contextvars_set(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A dispatch inside a runbook step carries the run_id + step_id on the row.

    Mirrors the agent_session_id propagation test: bind the contextvars,
    dispatch, assert the real DB columns. The G12.3 engine will bind these
    contextvars around every ``call_operation(...)`` invocation; this test
    validates the plumbing without the engine present.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()
    run_id = uuid.uuid4()

    run_id_token = run_id_var.set(run_id)
    step_id_token = step_id_var.set("rotate-creds")
    try:
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.op_call",
            target=None,
            params={},
        )
    finally:
        run_id_var.reset(run_id_token)
        step_id_var.reset(step_id_token)

    rows = (await db_session.scalars(select(AuditLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id  # type: ignore[attr-defined]
    assert row.step_id == "rotate-creds"  # type: ignore[attr-defined]


@_skip_if_no_columns
async def test_audit_row_null_when_contextvars_unset(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A top-level dispatch (no runbook in scope) leaves run_id/step_id NULL.

    Regression guard: chassis HTTP, MCP tool, and agent-loop dispatches
    that fire outside a runbook step must not gain spurious correlation.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    # No contextvar binding.
    await dispatch(
        operator=operator,
        connector_id="stub-1.x",
        op_id="stub.op_call",
        target=None,
        params={},
    )

    rows = (await db_session.scalars(select(AuditLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id is None  # type: ignore[attr-defined]
    assert row.step_id is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Payload mirror assertions (always run -- no DB column dependency)
# ---------------------------------------------------------------------------


async def test_audit_payload_mirrors_run_step(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The run_id + step_id appear in the JSON payload when contextvars are set.

    Broadcast consumers parse ``payload`` (not the dedicated columns), so
    the mirror is the load-bearing surface for G12.3 audit subscribers.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()
    run_id = uuid.uuid4()

    run_id_token = run_id_var.set(run_id)
    step_id_token = step_id_var.set("rotate-creds")
    try:
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.op_call",
            target=None,
            params={},
        )
    finally:
        run_id_var.reset(run_id_token)
        step_id_var.reset(step_id_token)

    rows = (await db_session.scalars(select(AuditLog))).all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert isinstance(payload, dict)
    assert payload["run_id"] == str(run_id)
    assert payload["step_id"] == "rotate-creds"


async def test_audit_payload_omits_run_step_when_unset(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Payload carries no run_id / step_id keys when contextvars are unset.

    The mirror uses ``if value is not None:`` guards (consistent with the
    parent_audit_id / agent_session_id mirrors); this test confirms the
    guard fires correctly so non-runbook rows stay clean.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    # No contextvar binding.
    await dispatch(
        operator=operator,
        connector_id="stub-1.x",
        op_id="stub.op_call",
        target=None,
        params={},
    )

    rows = (await db_session.scalars(select(AuditLog))).all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert isinstance(payload, dict)
    assert "run_id" not in payload
    assert "step_id" not in payload


# ---------------------------------------------------------------------------
# Pure unit test -- no dispatch, no DB
# ---------------------------------------------------------------------------


def test_contextvar_resets_cleanly() -> None:
    """set → non-None → reset → None; no dispatch, no DB.

    Validates that the contextvar tokens work correctly so the G12.3
    engine's set/reset pattern around step execution is sound.
    """
    assert run_id_var.get() is None
    assert step_id_var.get() is None

    run_id = uuid.uuid4()
    run_token = run_id_var.set(run_id)
    step_token = step_id_var.set("rotate-creds")

    assert run_id_var.get() == run_id
    assert step_id_var.get() == "rotate-creds"

    run_id_var.reset(run_token)
    step_id_var.reset(step_token)

    assert run_id_var.get() is None
    assert step_id_var.get() is None
