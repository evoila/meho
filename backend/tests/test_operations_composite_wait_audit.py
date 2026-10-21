# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step operation that stops part-way gets its own audit row (#4275).

A step of a multi-step operation (a composite) can wait for approval or be
denied. The handler returns that result as it is. Before #4275 the dispatcher
wrote no row for the operation, although the rows of the steps that ran, the
step's approval request and the flight-recorder trace all point at the
operation's audit id. The decision record is
``docs/decisions/composite-audit-when-a-step-waits.md``.

Every test drives the real :func:`meho_backplane.operations.dispatch` against
the migrated SQLite engine and fails without the change. Covered here:

* a handler that returns ``awaiting_approval`` or ``denied`` leaves one
  ``operation_execution`` row under the operation's own audit id, with the
  caller's status, the matching status code, the result's extras and the real
  elapsed time;
* the step rows and the step's ``approval.request`` row point at that row;
* a step the policy check says no to, and a step refused before it could wait
  (#3542), both leave their own ``denied`` decision row under the operation's
  ``denied`` row.

The approval, replay and failed-save cases are in
``test_operations_composite_wait_resume.py``; the trace and query routes in
``test_api_audit_wait_rows.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PERMISSION_PREFLIGHTS,
    PreviewContext,
    register_permission_preflight,
)
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_RETURNS_OP_ID = "vault.returns"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# A typed handler that returns a waiting or denied result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returned", "status_code"),
    [
        (
            OperationResult(
                status="awaiting_approval",
                op_id=_RETURNS_OP_ID,
                duration_ms=0.0,
                extras={
                    "error_code": "awaiting_approval",
                    "approval_request_id": "00000000-0000-0000-0000-000000000001",
                },
            ),
            202,
        ),
        (
            OperationResult(
                status="denied",
                op_id=_RETURNS_OP_ID,
                error="denied: policy denied",
                duration_ms=0.0,
                extras={"error_code": "denied", "reason": "policy denied"},
            ),
            403,
        ),
    ],
    ids=["awaiting_approval", "denied"],
)
async def test_returned_wait_or_deny_writes_one_execution_row(
    stub_embedding_service: AsyncMock,
    returned: OperationResult,
    status_code: int,
) -> None:
    """The row carries the caller's status, its code, the result's extras and the real time."""
    h.returned_result[:] = [returned]
    register_connector_v2(product="vault", version="", impl_id="", cls=h.NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_RETURNS_OP_ID,
        handler=h.returning_handler,
        summary="Returns.",
        description="Returns a built result.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await h.run(_RETURNS_OP_ID)

    rows = await h.rows(_RETURNS_OP_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_class == AuditEventClass.OPERATION_EXECUTION.value
    assert row.status_code == status_code
    assert row.payload["result_status"] == returned.status
    assert row.payload["error"] == dict(returned.extras)
    # The handler-built result says 0 ms; the row records the real elapsed time.
    assert row.duration_ms >= 20
    # The caller gets the same result as before, plus the row's id.
    assert result.audit_id == row.id
    assert result.model_dump(exclude={"audit_id"}) == returned.model_dump(exclude={"audit_id"})


# ---------------------------------------------------------------------------
# A step waits: the operation's row, and what points at it
# ---------------------------------------------------------------------------


async def test_step_waits_leaves_one_row_under_the_operations_own_id(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """One ``awaiting_approval`` row; the step row and the request row point at it."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.WAIT_OP_ID, "step_waits_composite")

    result = await h.run(h.WAIT_OP_ID)

    assert result.status == "awaiting_approval", result.error
    rows = await h.rows(h.WAIT_OP_ID)
    assert len(rows) == 1
    h.assert_wait_row(rows[0], result)
    assert rows[0].parent_audit_id is None

    request = await h.request(UUID(result.extras["approval_request_id"]))
    assert request.status == ApprovalRequestStatus.PENDING.value
    request_row = await h.row(request.request_audit_id)
    assert request_row is not None
    assert request_row.path == "approval.request"
    assert request_row.event_class == AuditEventClass.POLICY_DECISION.value
    assert request_row.parent_audit_id == rows[0].id

    steps = await h.rows(h.CHILD_OP_ID)
    assert len(steps) == 1
    assert steps[0].parent_audit_id == rows[0].id


# ---------------------------------------------------------------------------
# A step is denied: the step's decision row and the operation's row
# ---------------------------------------------------------------------------


async def test_step_denied_by_policy_leaves_a_step_row_and_an_operation_row(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A plain policy "no" for a step: a decision row for the step, a ``denied`` run row."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.DENY_OP_ID, "step_denied_composite")

    result = await h.run(h.DENY_OP_ID, h.operator(principal_kind=PrincipalKind.AGENT))

    assert result.status == "denied", result.error
    runs = await h.rows(h.DENY_OP_ID)
    assert len(runs) == 1
    run = runs[0]
    assert run.event_class == AuditEventClass.OPERATION_EXECUTION.value
    assert run.status_code == 403
    assert run.payload["result_status"] == "denied"
    assert run.payload["error"]["error_code"] == "denied"
    assert result.audit_id == run.id

    steps = await h.rows(h.STEP_DENIED)
    assert len(steps) == 1
    assert steps[0].event_class == AuditEventClass.POLICY_DECISION.value
    assert steps[0].status_code == 403
    assert steps[0].payload["result_status"] == "denied"
    assert steps[0].parent_audit_id == run.id

    reads = await h.rows(h.CHILD_OP_ID)
    assert [r.parent_audit_id for r in reads] == [run.id]


async def test_step_refused_before_it_could_wait_keeps_its_row_and_gets_an_operation_row(
    session: AsyncSession,
) -> None:
    """The #3542 refusal keeps the step's ``denied`` row and adds the operation's row."""
    register_connector_v2(product="vault", version="", impl_id="", cls=h.NoOpVaultConnector)
    await h.register_composite(session, h.PREFLIGHT_OP_ID, "step_refused_before_wait_composite")

    async def _preflight(ctx: PreviewContext) -> dict[str, Any]:
        return {
            "check": "vault.capabilities-self",
            "path": "secret/data/example",
            "required": ["create", "update"],
            "granted": ["read"],
            "will_be_denied": True,
        }

    register_permission_preflight(h.STEP_PREFLIGHT, _preflight)
    try:
        result = await h.run(h.PREFLIGHT_OP_ID)
    finally:
        _PERMISSION_PREFLIGHTS.pop(h.STEP_PREFLIGHT, None)

    assert result.status == "denied", result.error
    assert result.extras["error_code"] == "write_capability_denied"
    runs = await h.rows(h.PREFLIGHT_OP_ID)
    assert len(runs) == 1
    assert runs[0].event_class == AuditEventClass.OPERATION_EXECUTION.value
    assert runs[0].status_code == 403
    assert runs[0].payload["result_status"] == "denied"
    assert runs[0].payload["error"]["error_code"] == "write_capability_denied"
    assert result.audit_id == runs[0].id

    steps = await h.rows(h.STEP_PREFLIGHT)
    assert len(steps) == 1
    assert steps[0].event_class == AuditEventClass.POLICY_DECISION.value
    assert steps[0].parent_audit_id == runs[0].id
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        assert (await s.execute(select(ApprovalRequest))).scalars().all() == []
