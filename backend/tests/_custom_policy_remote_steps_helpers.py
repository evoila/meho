# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step operation whose step under test is stored under another connector.

The multi-step operation ``probe.composite.remote_steps`` is stored under
connector ``probehub-1.x``. Its first step waits for a person. Its second
step is checked under connector ``probe-1.x``, whose rows the test stores
(a built-in row, a row of the caller's tenant, or both). The same step also
runs as a child call (``probe.composite.remote_child``). A service login
starts the run, a person approves the first step with a token, and the run
goes on. The composite handlers live here because the dispatcher imports
them by their module path.

All names and values are made up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._people_behind_helpers import give_service_a_known_person

REMOTE_RUN = "probe.composite.remote_steps"
REMOTE_CHILD_RUN = "probe.composite.remote_child"
REMOTE_CONNECTOR = "probehub-1.x"
STEP_CONNECTOR = "probe-1.x"
_PLAIN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"url": {"type": "string"}}}


class _ProbeHubConnector(h.NoOpVaultConnector):
    product = "probehub"
    version = "1.x"
    impl_id = "probehub"


class _ProbeConnector(h.NoOpVaultConnector):
    product = "probe"
    version = "1.x"
    impl_id = "probe"


async def _first_step(operator: Operator, target: Any, run_op_id: str) -> Any:
    """The first step waits for a person; it records itself in ``c.ran`` when it runs."""
    if composite_resume_state(run_op_id) is not None:
        return None
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=REMOTE_CONNECTOR,
        op_id=h.STEP_A,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"name": "vm-1"},
    )
    if gated is None:
        c.ran.append(h.STEP_A)
    return gated


async def remote_steps(operator: Any, target: Any, params: dict[str, Any]) -> Any:
    """A step that waits, then the step under test, checked under connector ``probe-1.x``."""
    gated = await _first_step(operator, target, REMOTE_RUN)
    if gated is not None:
        return gated
    [step] = c.current
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=STEP_CONNECTOR,
        op_id=step.op_id,
        safety_level=step.safety_level,
        requires_approval=step.requires_approval,
        target=target,
        params={},
        resume_state={"at": "remote"},
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    c.ran.append(step.op_id)
    return {"done": True}


async def remote_child(
    operator: Any, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits, then the step under test as a child call under ``probe-1.x``."""
    gated = await _first_step(operator, target, REMOTE_CHILD_RUN)
    if gated is not None:
        return gated
    [step] = c.current
    return await dispatch_child(connector_id=STEP_CONNECTOR, op_id=step.op_id, params={})


async def probe_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The handler of a typed row of the step under test; it records that it ran."""
    c.ran.append(c.current[0].op_id)
    return {"done": True}


def probe_row(step: Step, tenant_id: UUID | None, facts: dict[str, Any]) -> EndpointDescriptor:
    """A stored row of the step's operation under ``probe-1.x``.

    *facts* may hold ``schema``, ``tags``, ``level`` (the stored risk
    level; by default the step's declared level), ``hard_to_undo``,
    ``changes_rights`` and ``typed`` (a typed row whose handler is :func:`probe_handler`, so a
    child call needs no target; by default an ingested row). An op id
    without a verb (``probe.things.wipe_old``) stores no verb and no path.
    """
    typed = facts.get("typed", False)
    method, path = step.op_id.split(":", 1) if ":" in step.op_id else (None, None)
    return EndpointDescriptor(
        tenant_id=tenant_id,
        product="probe",
        version="1.x",
        impl_id="probe",
        op_id=step.op_id,
        source_kind="typed" if typed else "ingested",
        handler_ref=f"{__name__}.probe_handler" if typed else None,
        method=method,
        path=path,
        summary="The step under test.",
        description="The step under test.",
        tags=facts.get("tags", []),
        parameter_schema=facts.get("schema", _PLAIN_SCHEMA),
        safety_level=facts.get("level", step.safety_level),
        requires_approval=step.requires_approval,
        hard_to_undo=facts.get("hard_to_undo"),
        changes_rights=facts.get("changes_rights"),
        is_enabled=True,
        embedding=h.EMBEDDING,
    )


def _composite_row(op_id: str, handler: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        tenant_id=None,
        product="probehub",
        version="1.x",
        impl_id="probehub",
        op_id=op_id,
        source_kind="composite",
        handler_ref=f"{__name__}.{handler}",
        summary="The run.",
        description="The run.",
        tags=[],
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=h.EMBEDDING,
    )


async def run_remote(
    session: AsyncSession,
    stub: AsyncMock,
    step: Step,
    built_in: dict[str, Any] | None,
    tenant_row: dict[str, Any] | None,
    *,
    child: bool = False,
    approver_role: TenantRole = TenantRole.OPERATOR,
) -> Any:
    """Store the rows, start the run as a service login, approve with a token, go on.

    With *child* the step under test runs as a child call
    (:data:`REMOTE_CHILD_RUN`), else through the step gate (:data:`REMOTE_RUN`).
    *approver_role* is the role of the person who approves the run.
    """
    await h.register_child(stub)
    register_connector_v2(
        product="probehub", version="1.x", impl_id="probehub", cls=_ProbeHubConnector
    )
    register_connector_v2(product="probe", version="1.x", impl_id="probe", cls=_ProbeConnector)
    async with get_sessionmaker()() as s, s.begin():
        if built_in is not None:
            s.add(probe_row(step, None, built_in))
        if tenant_row is not None:
            s.add(probe_row(step, h.TENANT_ID, tenant_row))
    run_op_id = REMOTE_CHILD_RUN if child else REMOTE_RUN
    session.add(_composite_row(run_op_id, "remote_child" if child else "remote_steps"))
    await session.commit()
    c.current.append(step)
    service = h.operator(sub=c.SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)
    await give_service_a_known_person(h.TENANT_ID, c.SERVICE_SUB)
    first = await dispatch(
        operator=service, connector_id=REMOTE_CONNECTOR, op_id=run_op_id, target=None, params={}
    )
    assert first.status == "awaiting_approval", first.error
    request = await h.approve(
        UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB, role=approver_role
    )
    approver = c.token_approver().model_copy(update={"tenant_role": approver_role})
    return await resume_dispatch_after_approval(operator=approver, request=request, params=None)
