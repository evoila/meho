# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the tests of the steps of an approved run.

A multi-step operation whose first step waits for a person. A service login
starts it, a person approves the first step, and the run goes on to the
step under test: once through the step gate
(:func:`~meho_backplane.operations.composite.enforce_subop_policy`) and once
as a child call (``dispatch_child``). The composite handlers live here
because the dispatcher imports them by their module path.

All names and values are made up.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    OperationRun,
    OperationRunStatus,
    Tenant,
)
from meho_backplane.operations import grant_approval, reset_dispatcher_caches
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy import fixed_rules
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.routes import build_router as build_ui_router
from tests import _composite_wait_audit_helpers as h
from tests._oidc_jwt_helpers import mint_token
from tests._people_behind_helpers import give_service_a_known_person

STEP_RUN = "vault.composite.custom_policy_step"
CHILD_RUN = "vault.composite.custom_policy_child"
SERVICE_SUB = "svc-probe-7"
APPROVER_SUB = "person-probe-3"
_AT_STEP = {"at": "step"}
_MODULE = __name__
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.approvals.routes._resolve_operator"
#: The words of a reason that lets a call run at once; never on a waiting step's row.
RUNS_AT_ONCE = "runs at once"
#: The stored reason of a wait the step rule makes; ``{}`` is the rule.
STEP_RULE_WAIT = (
    "run_step_keeps_own_approval: a step of a run after an approval keeps its own approval ({})"
)


@dataclass(frozen=True)
class Step:
    """The step under test: its operation and its declared risk level and flag."""

    op_id: str
    safety_level: str
    requires_approval: bool


RIGHTS = Step("probe.things.share", "dangerous", False)
POLICY = Step("probe.things.policy_set", "dangerous", False)
ROOT_SHELL = Step("probe.things.root_run", "dangerous", False)
SNAPSHOT_SAVE = Step("probe.host.snapshot_save", "safe", False)

#: The step the run reaches after the approval; set by each test.
current: list[Step] = []
#: The steps that ran, in order.
ran: list[str] = []


async def _first_step(operator: Operator, target: Any, composite_op_id: str) -> Any:
    """The first step waits for a person; it records itself in :data:`ran` when it runs."""
    if composite_resume_state(composite_op_id) is not None:
        return None
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is None:
        ran.append(h.STEP_A)
    return gated


async def step_run(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """A step that waits, then the step under test through the step gate."""
    gated = await _first_step(operator, target, STEP_RUN)
    if gated is not None:
        return gated
    [step] = current
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=step.op_id,
        safety_level=step.safety_level,
        requires_approval=step.requires_approval,
        target=target,
        params={},
        resume_state=_AT_STEP,
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    ran.append(step.op_id)
    return {"done": True}


async def child_run(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits, then the step under test as a child call."""
    gated = await _first_step(operator, target, CHILD_RUN)
    if gated is not None:
        return gated
    [step] = current
    return await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=step.op_id, params={})


async def child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append(current[0].op_id)
    return {"done": True}


def reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The state of one test: the env, the marks of the made-up operations, empty lists."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {RIGHTS.op_id, POLICY.op_id})
    monkeypatch.setattr(
        fixed_rules, "POLICY_CHANGE_OPS", fixed_rules.POLICY_CHANGE_OPS | {POLICY.op_id}
    )
    monkeypatch.setattr(
        "meho_backplane.op_classes._ROOT_SHELL", {ROOT_SHELL.op_id, SNAPSHOT_SAVE.op_id}
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    ran.clear()
    current.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


def token_approver() -> Operator:
    """The person who approves with a token (no web console)."""
    return Operator(
        sub=APPROVER_SUB,
        name="Probe Approver",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        approver=True,
    )


async def register(
    session: AsyncSession,
    stub: Any,
    step: Step,
    *,
    child: bool,
    schema: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    stored: bool = False,
    method: str | None = None,
) -> None:
    """Register the composite and the step's operation.

    The step's operation is stored for a child call, and for a step-gate step
    when *stored* is set. *method* stores an HTTP verb and path on its row.
    """
    await h.register_child(stub)
    if child or stored:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=step.op_id,
            handler=child_handler,
            summary="The step under test.",
            description="The step under test.",
            parameter_schema=schema or {"type": "object"},
            when_to_use=None,
            embedding_service=stub,
            safety_level=step.safety_level,
            requires_approval=step.requires_approval,
            tags=tags,
        )
    if method is not None:
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(
                update(EndpointDescriptor)
                .where(EndpointDescriptor.op_id == step.op_id)
                .values(method=method, path="/probe/things/{id}")
            )
    op_id, handler = (CHILD_RUN, "child_run") if child else (STEP_RUN, "step_run")
    await h.register_composite(session, op_id, handler, module=_MODULE)
    current.append(step)


async def requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(ApprovalRequest))).scalars().all()
        return sorted(rows, key=lambda r: r.created_at)


async def start_run(op_id: str) -> UUID:
    """A service login starts the run; it waits on its first step. Returns the request id."""
    service = h.operator(sub=SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)
    await give_service_a_known_person(h.TENANT_ID, SERVICE_SUB)
    first = await h.run(op_id, service)
    assert first.status == "awaiting_approval", first.error
    [waiting] = await requests()
    assert waiting.op_id == h.STEP_A
    return UUID(first.extras["approval_request_id"])


def run_op(*, child: bool) -> str:
    return CHILD_RUN if child else STEP_RUN


async def assert_went_ahead(step: Step, *, child: bool, run_op_id: str | None = None) -> None:
    """The step ran under the run's approval: no second request, its row is ``ok``.

    The row of a child call is on the child's path; a step-gate step has
    its row on the run's path (*run_op_id*, by default the run of :func:`run_op`).
    """
    assert ran == [h.STEP_A, step.op_id]
    assert len(await requests()) == 1
    last = (await h.rows(step.op_id if child else run_op_id or run_op(child=child)))[-1]
    assert last.payload["result_status"] == "ok"


async def assert_waits(
    step: Step, *, child: bool, wait_reason: str | None = None, run_op_id: str | None = None
) -> ApprovalRequest:
    """The step waits for its own approval: it did not run; its request and audit rows exist.

    The run's own row names the waiting request. The step's own
    ``approval.request`` row (``own.request_audit_id``) names the step's
    operation and the request, and says nowhere that the call runs at once.
    With *wait_reason*, its stored ``wait_reason`` is that text.
    """
    assert ran == [h.STEP_A]
    _, own = await requests()
    assert own.op_id == step.op_id
    last: AuditLog = (await h.rows(run_op_id or run_op(child=child)))[-1]
    assert last.status_code == 202
    assert last.payload["result_status"] == "awaiting_approval"
    assert last.payload["error"]["approval_request_id"] == str(own.id)
    assert_own_request_row(await own_request_row(own), own, wait_reason=wait_reason)
    return own


async def own_request_row(own: ApprovalRequest) -> AuditLog:
    """The ``approval.request`` audit row of the step's own request."""
    assert own.request_audit_id is not None
    found = await h.row(own.request_audit_id)
    assert found is not None
    return found


def assert_own_request_row(
    found: AuditLog, own: ApprovalRequest, *, wait_reason: str | None = None
) -> None:
    """The row names the step's operation and its request; no text says it runs at once."""
    assert found.path == "approval.request"
    assert found.payload["op_id"] == own.op_id
    assert found.payload["approval_request_id"] == str(own.id)
    assert RUNS_AT_ONCE not in json.dumps(found.payload, default=str)
    if wait_reason is not None:
        assert found.payload["wait_reason"] == wait_reason


# -- the approve routes -------------------------------------------------------------


def console_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings of the web console; an approve there needs a recent sign-in only."""
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "probe-client-secret")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()


async def approve_with_token(
    client: httpx.AsyncClient, key: Any, request_id: UUID, *, sub: str = APPROVER_SUB
) -> httpx.Response:
    """``POST /api/v1/approvals/{id}/approve`` with a fresh token; the route runs the rest."""
    async with get_sessionmaker()() as s:
        request = await s.get(ApprovalRequest, request_id)
        assert request is not None
        params = request.params
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(h.TENANT_ID),
        fresh_sign_in=True,
    )
    response = await client.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={"params": params},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response


def console_person(
    *,
    sub: str = APPROVER_SUB,
    role: TenantRole = TenantRole.OPERATOR,
    platform_admin: bool = False,
    tenant_id: UUID = h.TENANT_ID,
) -> Operator:
    """A person signed in to the web console a few seconds ago."""
    return Operator(
        sub=sub,
        raw_jwt="<probe-console-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        platform_admin=platform_admin,
        approver=True,
        acr="1",
        auth_time=int(time.time()) - 5,
        sid="probe-sid-1",
    )


def _console_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_ui_router())
    return app


async def approve_in_console(request_id: UUID, person: Operator) -> None:
    """``POST /ui/approvals/{id}/approve`` with a web session; waits until the run ends."""
    async with get_sessionmaker()() as s, s.begin():
        if await s.get(Tenant, person.tenant_id) is None:
            s.add(Tenant(id=person.tenant_id, slug="probe-tenant", name="Probe tenant"))
        web = await create_session(
            s,
            operator_sub=person.sub,
            tenant_id=person.tenant_id,
            access_token="probe-access-token",
            refresh_token="probe-refresh-token",
            lifetime=timedelta(hours=1),
        )
    csrf = mint_csrf_token(str(web.id))
    signed_in = person.model_copy(update={"web_session": True, "web_session_id": web.id})
    with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=signed_in):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_console_app()),
            base_url="https://testserver",
            cookies={SESSION_COOKIE_NAME: str(web.id), CSRF_COOKIE_NAME: csrf},
        ) as client:
            response = await client.post(
                f"/ui/approvals/{request_id}/approve",
                data={"reason": "probe approve"},
                headers={CSRF_HEADER_NAME: csrf},
            )
    assert response.status_code == 200, response.text
    await _run_ended(request_id)


async def _run_ended(request_id: UUID) -> None:
    """Wait until the run the approve started has ended (at most 60 seconds).

    The run goes on in the background; a busy CI runner can need more than
    a few seconds for it.
    """
    going = {OperationRunStatus.PENDING.value, OperationRunStatus.RUNNING.value}
    for _ in range(1200):
        async with get_sessionmaker()() as s:
            stmt = select(OperationRun).where(OperationRun.approval_request_id == request_id)
            runs = list((await s.execute(stmt)).scalars())
        if runs and all(run.status not in going for run in runs):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"the run of {request_id} did not end")
