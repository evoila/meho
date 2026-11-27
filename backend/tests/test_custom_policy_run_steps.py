# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, each step of an approved run asks the fixed rules.

The whole path, as a real caller takes it: a service login calls a
multi-step operation. Its first step waits for a person. A person approves
it with a token over REST, and the run goes on under that approval. Then
the run reaches the step under test, as a step of the step gate
(:func:`~meho_backplane.operations.composite.enforce_subop_policy`) or as a
child call (``dispatch_child``, the dispatcher's approved-run branch). The
test reads the audit row of the run after the approval.

With the switch on:

* a step whose operation has only the plain "always ask" mark goes ahead
  under the run's approval;
* a step that a fixed rule covers (a delete name, a change of rights, a
  credential call, a call to an address the caller gives) waits for its own
  approval;
* a root-shell step and a step an override made wait still wait.

With the switch off, each of these steps does what it did before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, OperationPolicyOverride
from meho_backplane.operations import grant_approval, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h
from tests._people_behind_helpers import give_service_a_known_person

pytestmark = pytest.mark.asyncio

_STEP_RUN = "vault.composite.custom_policy_step"
_CHILD_RUN = "vault.composite.custom_policy_child"
_AT_STEP = {"at": "step"}


@dataclass(frozen=True)
class _Step:
    """The step under test: its operation and its declared risk level and flag."""

    op_id: str
    safety_level: str
    requires_approval: bool


_PLAIN = _Step("vault.things.label", "safe", True)
_DELETE_NAME = _Step("vault.things.delete", "dangerous", False)
_RIGHTS = _Step("vault.things.share", "dangerous", False)
_CREDENTIAL = _Step("vault.token.create", "safe", False)
_ROOT_SHELL = _Step("vault.things.root_run", "dangerous", False)
_OVERRIDDEN = _Step("vault.things.resize", "safe", False)
_CALLER_ADDRESS = _Step("vault.things.fetch", "dangerous", False)

#: The step the run reaches after the approval; set by each test.
current: list[_Step] = []
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
    gated = await _first_step(operator, target, _STEP_RUN)
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
    gated = await _first_step(operator, target, _CHILD_RUN)
    if gated is not None:
        return gated
    [step] = current
    return await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=step.op_id, params={})


async def child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append(current[0].op_id)
    return {"done": True}


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_RIGHTS.op_id})
    monkeypatch.setattr("meho_backplane.op_classes._ROOT_SHELL", {_ROOT_SHELL.op_id})
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    ran.clear()
    current.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


def _rest_approver() -> Operator:
    """A person who approves with a token over REST (no web console click)."""
    return Operator(
        sub="op-approver",
        name="REST Approver",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        approver=True,
    )


async def _register(
    session: AsyncSession, stub: AsyncMock, step: _Step, *, child: bool, schema: dict[str, Any]
) -> None:
    await h.register_child(stub)
    if child:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=step.op_id,
            handler=child_handler,
            summary="The step under test.",
            description="The step under test.",
            parameter_schema=schema,
            when_to_use=None,
            embedding_service=stub,
            safety_level=step.safety_level,
            requires_approval=step.requires_approval,
        )
    op_id, handler = (_CHILD_RUN, "child_run") if child else (_STEP_RUN, "step_run")
    await h.register_composite(session, op_id, handler, module=__name__)
    current.append(step)


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(ApprovalRequest))).scalars().all()
        return sorted(rows, key=lambda r: r.created_at)


async def _approved_run(op_id: str) -> Any:
    """A service login's run waits on its first step; a person approves it over REST."""
    service = h.operator(sub="service-requester", principal_kind=PrincipalKind.SERVICE)
    await give_service_a_known_person(h.TENANT_ID, "service-requester")
    first = await h.run(op_id, service)
    assert first.status == "awaiting_approval", first.error
    [waiting] = await _requests()
    assert waiting.op_id == h.STEP_A
    request = await h.approve(UUID(first.extras["approval_request_id"]))
    return await resume_dispatch_after_approval(
        operator=_rest_approver(), request=request, params=None
    )


async def _run_step(
    session: AsyncSession,
    stub: AsyncMock,
    step: _Step,
    *,
    child: bool = False,
    schema: dict[str, Any] | None = None,
) -> Any:
    await _register(session, stub, step, child=child, schema=schema or {"type": "object"})
    return await _approved_run(_CHILD_RUN if child else _STEP_RUN)


async def _assert_went_ahead(result: Any, step: _Step, run_op: str) -> None:
    assert result.status == "ok", result.error
    assert ran == [h.STEP_A, step.op_id]
    assert len(await _requests()) == 1
    found = await h.row(result.audit_id)
    assert found is not None
    assert found.payload["result_status"] == "ok"
    assert found.path in {run_op, step.op_id}


async def _assert_waits(result: Any, step: _Step, run_op: str) -> None:
    assert result.status == "awaiting_approval", result.error
    assert ran == [h.STEP_A]
    _, own = await _requests()
    assert own.op_id == step.op_id
    assert str(own.id) == result.extras["approval_request_id"]
    h.assert_wait_row((await h.rows(run_op))[-1], result)


@pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
async def test_on_a_step_with_only_the_plain_mark_goes_ahead_under_the_run_approval(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    _switch(monkeypatch, on=True)
    result = await _run_step(session, stub_embedding_service, _PLAIN, child=child)
    await _assert_went_ahead(result, _PLAIN, _CHILD_RUN if child else _STEP_RUN)


@pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
async def test_off_a_step_with_the_plain_mark_waits_as_before(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    _switch(monkeypatch, on=False)
    result = await _run_step(session, stub_embedding_service, _PLAIN, child=child)
    await _assert_waits(result, _PLAIN, _CHILD_RUN if child else _STEP_RUN)


@pytest.mark.parametrize(
    "step", [_DELETE_NAME, _RIGHTS, _CREDENTIAL, _ROOT_SHELL], ids=lambda s: s.op_id
)
@pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
async def test_on_a_step_a_fixed_rule_or_the_root_shell_mark_covers_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: _Step,
    child: bool,
) -> None:
    _switch(monkeypatch, on=True)
    result = await _run_step(session, stub_embedding_service, step, child=child)
    await _assert_waits(result, step, _CHILD_RUN if child else _STEP_RUN)


async def test_on_a_child_call_to_an_address_the_caller_gives_waits(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    _switch(monkeypatch, on=True)
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string", CALLER_ADDRESS_MARKER: True}},
    }
    result = await _run_step(
        session, stub_embedding_service, _CALLER_ADDRESS, child=True, schema=schema
    )
    await _assert_waits(result, _CALLER_ADDRESS, _CHILD_RUN)


@pytest.mark.parametrize(
    ("step", "child"),
    [
        (_DELETE_NAME, False),
        (_RIGHTS, False),
        (_ROOT_SHELL, False),
        (_DELETE_NAME, True),
        (_ROOT_SHELL, True),
    ],
    ids=["step-delete", "step-rights", "step-root-shell", "child-delete", "child-root-shell"],
)
async def test_off_the_same_steps_go_ahead_under_the_run_approval_as_before(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: _Step,
    child: bool,
) -> None:
    _switch(monkeypatch, on=False)
    result = await _run_step(session, stub_embedding_service, step, child=child)
    await _assert_went_ahead(result, step, _CHILD_RUN if child else _STEP_RUN)


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_a_credential_step_waits_in_both_switch_states(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    _switch(monkeypatch, on=on)
    result = await _run_step(session, stub_embedding_service, _CREDENTIAL)
    await _assert_waits(result, _CREDENTIAL, _STEP_RUN)


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_a_step_an_override_made_wait_still_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    _switch(monkeypatch, on=on)
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationPolicyOverride(
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=_OVERRIDDEN.op_id,
                set_by_sub="admin-a",
                requires_approval=True,
            )
        )
    result = await _run_step(session, stub_embedding_service, _OVERRIDDEN)
    await _assert_waits(result, _OVERRIDDEN, _STEP_RUN)
