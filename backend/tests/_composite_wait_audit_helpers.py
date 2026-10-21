# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces for the #4275 tests: a multi-step operation that stops part-way.

Used by ``test_operations_composite_wait_audit.py`` (the rows),
``test_operations_composite_wait_resume.py`` (approval, replay, failed save)
and ``test_api_audit_wait_rows.py`` (the trace and query routes). The
composite handlers live here because the dispatcher imports them by their
module path (``handler_ref``); closures are rejected.

Every governed step is ``safe`` + ``requires_approval``, so a person and an
agent both wait for approval on it. The one denied step is ``dangerous``
without a grant, which the policy check refuses for an agent.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.audit_query.replay import ReplayNode, replay_session
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations import DispatchChild, dispatch, register_typed_operation
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.operations.composite import composite_resume_state, enforce_subop_policy

TENANT_ID = UUID("00000000-0000-0000-0000-00000000427a")
CONNECTOR_ID = "vault-1.x"
# The step is governed under the composite's own connector, as in every shipped
# composite: the approval request stores this connector id, and the resume
# re-dispatches the parent composite under it.
STEP_CONNECTOR_ID = CONNECTOR_ID
CHILD_OP_ID = "vault.kv.list"
WAIT_OP_ID = "vault.composite.step_waits"
DENY_OP_ID = "vault.composite.step_denied"
PREFLIGHT_OP_ID = "vault.composite.step_refused_before_wait"
TWO_WAIT_OP_ID = "vault.composite.two_waits"
STEP_A = "POST:/vcenter/vm"
STEP_B = "POST:/vcenter/vm/{vm}/hardware/cdrom"
STEP_DENIED = "DELETE:/vcenter/vm/{vm}"
STEP_PREFLIGHT = "PUT:/vcenter/preflight-denied"
SAVED_STATE = {"vm": "vm-1"}

EMBEDDING = [0.1] * 384


def operator(
    *,
    sub: str = "op-requester",
    principal_kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: UUID = TENANT_ID,
) -> Operator:
    return Operator(
        sub=sub,
        name="Wait Audit Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class NoOpVaultConnector(Connector):
    """Resolver-satisfying connector class -- never actually called."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed child step: a read that leaves a step row under the composite."""
    return {"echo": params}


returned_result: list[OperationResult] = []


async def returning_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> OperationResult:
    """Typed handler that returns a built result instead of raising."""
    await asyncio.sleep(0.02)
    return returned_result[0]


async def gate(
    operator: Operator, target: Any, op_id: str, **kwargs: Any
) -> OperationResult | None:
    """Gate one governed step so that a person or an agent waits for approval on it."""
    return await enforce_subop_policy(
        operator=operator,
        connector_id=STEP_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"name": "vm-1"},
        **kwargs,
    )


async def step_waits_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """One read step through ``dispatch_child``, then a step that waits for approval."""
    await dispatch_child(connector_id=CONNECTOR_ID, op_id=CHILD_OP_ID, params={"path": "/a"})
    gated = await gate(operator, target, STEP_A)
    if gated is not None:
        return gated
    return {"done": True}


async def step_denied_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """One read step, then a ``dangerous`` step the policy check says no to for an agent."""
    await dispatch_child(connector_id=CONNECTOR_ID, op_id=CHILD_OP_ID, params={"path": "/a"})
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=STEP_CONNECTOR_ID,
        op_id=STEP_DENIED,
        safety_level="dangerous",
        requires_approval=False,
        target=target,
        params={"vm": "vm-42"},
    )
    if gated is not None:
        return gated
    return {"done": True}


async def step_refused_before_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A step whose permission preflight says the write will be denied (#3542)."""
    gated = await gate(operator, target, STEP_PREFLIGHT)
    if gated is not None:
        return gated
    return {"done": True}


async def two_waits_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """Step A waits; after its approval step B waits again; after that the run ends ok.

    Step B runs after the main change and saves where to continue, so its
    approval re-enters the composite at step B (#4217) and repeats no earlier
    write.
    """
    if composite_resume_state(TWO_WAIT_OP_ID) is None:
        await dispatch_child(connector_id=CONNECTOR_ID, op_id=CHILD_OP_ID, params={"path": "/a"})
        gated_a = await gate(operator, target, STEP_A)
        if gated_a is not None:
            return gated_a
    gated_b = await gate(operator, target, STEP_B, resume_state=SAVED_STATE, after_main_effect=True)
    if gated_b is not None:
        return gated_b
    return {"done": True}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def register_child(embedding: Any) -> None:
    """Register the connector and the typed read step the composites fan out."""
    register_connector_v2(product="vault", version="", impl_id="", cls=NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=CHILD_OP_ID,
        handler=child_handler,
        summary="List.",
        description="List.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )


async def register_composite(session: AsyncSession, op_id: str, handler_name: str) -> None:
    """Insert one ``source_kind='composite'`` descriptor pointing at a handler here."""
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            source_kind="composite",
            method=None,
            path=None,
            handler_ref=f"tests._composite_wait_audit_helpers.{handler_name}",
            summary=f"Composite {op_id}.",
            description=f"Composite {op_id}.",
            tags=[],
            parameter_schema={"type": "object"},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=EMBEDDING,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Running and reading back
# ---------------------------------------------------------------------------


async def run(op_id: str, who: Operator | None = None) -> OperationResult:
    return await dispatch(
        operator=who or operator(),
        connector_id=CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params={},
    )


async def rows(path: str) -> list[AuditLog]:
    """Every audit row on *path*, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        stmt = select(AuditLog).where(AuditLog.path == path).order_by(AuditLog.occurred_at)
        return list((await s.execute(stmt)).scalars().all())


async def row(audit_id: UUID) -> AuditLog | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        return await s.get(AuditLog, audit_id)


async def request(request_id: UUID) -> ApprovalRequest:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        found = await s.get(ApprovalRequest, request_id)
        assert found is not None
        return found


async def approve(request_id: UUID, *, sub: str = "op-approver") -> ApprovalRequest:
    """A second person approves; returns the approved row."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await approve_request(s, request_id, operator=operator(sub=sub), params=None)
        await s.commit()
    return await request(request_id)


async def replay(session_id: UUID) -> list[ReplayNode]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        return await replay_session(session_id, tenant_id=TENANT_ID, session=s)


def fail_operation_row(monkeypatch: pytest.MonkeyPatch, op_id: str, exc: BaseException) -> Any:
    """Make only the write of the operation's own row raise *exc*; return the real writer."""
    original = audit_module.write_audit_row

    async def _write(**kwargs: Any) -> None:
        if kwargs["descriptor"].op_id == op_id:
            raise exc
        await original(**kwargs)

    monkeypatch.setattr(audit_module, "write_audit_row", _write)
    return original


def assert_wait_row(found: AuditLog, result: OperationResult) -> None:
    """The operation's own row for a run whose step waits for approval."""
    assert found.event_class == "operation_execution"
    assert found.status_code == 202
    assert found.payload["result_status"] == "awaiting_approval"
    assert found.payload["error"]["approval_request_id"] == result.extras["approval_request_id"]
    assert result.audit_id == found.id
