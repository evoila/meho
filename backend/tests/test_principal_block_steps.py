# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each step of a multi-step operation checks the admin block list.

The tests drive the real dispatcher against the migrated SQLite engine. The
person is blocked (or the block list becomes unreadable) while the
operation runs, between two steps. Covered:

* two steps through ``dispatch_child``: the first step reaches the target,
  the second is refused with ``principal_blocked`` and is not sent;
* a governed direct step (``run_governed_subop`` and
  ``enforce_subop_policy``) of a blocked person: its call is not made;
* the same governed step when the block list cannot be read: refused with
  ``principal_block_check_unavailable``;
* an approver runs an approved multi-step operation, and the person who
  asked is blocked between two steps: the next step (through
  ``dispatch_child`` or a governed step) is refused and not sent;
* once a step is refused, no later step of the run is sent, also when the
  handler catches the refusal and the list can be read again;
* an approved run that the block list refuses after its check of the person
  who asked: the answer and the stored result say ``uncertain`` when a step
  was sent before the wait, on both resume paths.

The dispatcher imports the handlers below by their module path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth import principal_block
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    PrincipalBlockedStepError,
    block_principal,
    reset_block_cache,
)
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations import (
    DispatchChild,
    approval_queue,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.composite import enforce_subop_policy, run_governed_subop
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h
from ._people_behind_helpers import give_agent_a_known_person

pytestmark = pytest.mark.asyncio

_MODULE = "tests.test_principal_block_steps"
_STEP_OP_ID = "vault.things.touch"
_TWO_STEPS_OP_ID = "vault.composite.two_child_steps"
_GOVERNED_OP_ID = "vault.composite.governed_step"
_GATED_OP_ID = "vault.composite.gated_step"
_WAIT_THEN_CHILD_OP_ID = "vault.composite.wait_then_child_steps"
_WAIT_THEN_GOVERNED_OP_ID = "vault.composite.wait_then_governed_step"
_PARALLEL_OP_ID = "vault.composite.parallel_parts"
_NESTED_OP_ID = "vault.composite.nested_part"
_THREE_GOVERNED_OP_ID = "vault.composite.three_governed_steps"
_THREE_GATED_OP_ID = "vault.composite.three_gated_steps"
_STEP_THEN_NESTED_WAIT_OP_ID = "vault.composite.step_then_nested_wait"
_DIRECT_STEP = "POST:/v1/sys/things"

#: Every call that reached the target, in order.
sent: list[str] = []
#: What happens between the first and the second step (set by each test).
between_steps: list[Callable[[Operator], Awaitable[None]]] = []
#: What happens after the handler caught a refusal (set by each test).
after_refusal: list[Callable[[Operator], Awaitable[None]]] = []
#: Where the nested part of ``parallel_parts_composite`` waits: step sent, then go on.
nested_events: list[asyncio.Event] = []


async def _between(operator: Operator) -> None:
    for hook in between_steps:
        await hook(operator)


async def touch_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed step: one call to the target."""
    sent.append(f"{_STEP_OP_ID}:{params['n']}")
    return {"ok": True}


async def two_child_steps_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """Two steps through ``dispatch_child``, with the hook between them."""
    first = await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 1})
    if first.status != "ok":
        return first
    await _between(operator)
    return await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 2})


async def governed_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The hook, then one governed direct step run by ``run_governed_subop``."""
    await _between(operator)

    async def _send() -> dict[str, Any]:
        sent.append(_DIRECT_STEP)
        return {"ok": True}

    step = await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": "thing-1"},
        call=_send,
    )
    return step.stopped if step.stopped is not None else step.value


async def gated_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The hook, then one direct step checked by ``enforce_subop_policy``."""
    await _between(operator)
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": "thing-1"},
    )
    if gated is not None:
        return gated
    sent.append(_DIRECT_STEP)
    return {"ok": True}


async def wait_then_child_steps_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits for approval, then two steps through ``dispatch_child``."""
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is not None:
        return gated
    return await two_child_steps_composite(operator, target, params, dispatch_child)


async def step_then_nested_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """One step, then a nested part whose first step waits for approval."""
    await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 1})
    return await dispatch_child(
        connector_id=h.CONNECTOR_ID, op_id=_WAIT_THEN_CHILD_OP_ID, params={}
    )


async def wait_then_governed_step_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits for approval, one step through ``dispatch_child``, then a governed step."""
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is not None:
        return gated
    first = await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 1})
    if first.status != "ok":
        return first
    return await governed_step_composite(operator, target, params)


async def nested_part_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """One step, a wait for the other part of the operation, then a second step."""
    step_sent, go_on = nested_events
    await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 1})
    step_sent.set()
    await go_on.wait()
    return await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 2})


async def parallel_parts_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A nested part, and next to it one step whose refusal this handler catches."""
    step_sent, go_on = nested_events
    nested = asyncio.create_task(
        dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_NESTED_OP_ID, params={})
    )
    await step_sent.wait()
    await _between(operator)
    try:
        await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 3})
    except PrincipalBlockedStepError:
        for hook in after_refusal:
            await hook(operator)
    go_on.set()
    await asyncio.gather(nested, return_exceptions=True)
    return {"done": True}


async def _governed(operator: Operator, target: Any, n: int) -> None:
    async def _send() -> dict[str, Any]:
        sent.append(f"{_DIRECT_STEP}:{n}")
        return {"ok": True}

    await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"n": n},
        call=_send,
    )


async def _gated(operator: Operator, target: Any, n: int) -> None:
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"n": n},
    )
    if gated is None:
        sent.append(f"{_DIRECT_STEP}:{n}")


async def three_governed_steps_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """Three ``run_governed_subop`` steps; the handler ignores a refusal and goes on."""
    await _governed(operator, target, 1)
    await _between(operator)
    await _governed(operator, target, 2)
    for hook in after_refusal:
        await hook(operator)
    await _governed(operator, target, 3)
    return {"done": True}


async def three_gated_steps_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """Three ``enforce_subop_policy`` steps; the handler ignores a refusal and goes on."""
    await _gated(operator, target, 1)
    await _between(operator)
    await _gated(operator, target, 2)
    for hook in after_refusal:
        await hook(operator)
    await _gated(operator, target, 3)
    return {"done": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    between_steps.clear()
    after_refusal.clear()
    nested_events.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    between_steps.clear()
    after_refusal.clear()
    nested_events.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="block-steps", name="Block Steps"))
        await s_.commit()
        yield s_


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


def _admin() -> Operator:
    return Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _block(operator: Operator) -> None:
    await block_principal(_admin(), operator.sub, "left the team")


def _unreadable(monkeypatch: pytest.MonkeyPatch) -> Callable[[Operator], Awaitable[None]]:
    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    async def _break_now(_operator: Operator) -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", _broken)
        reset_block_cache()

    return _break_now


def _readable(monkeypatch: pytest.MonkeyPatch) -> Callable[[Operator], Awaitable[None]]:
    original = principal_block._load_entries

    async def _restore_now(_operator: Operator) -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", original)
        reset_block_cache()

    return _restore_now


async def _register(session: AsyncSession, embedding: AsyncMock, op_id: str, handler: str) -> None:
    await h.register_child(embedding)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_STEP_OP_ID,
        handler=touch_handler,
        summary="Touch a thing.",
        description="Touch a thing.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        embedding_service=embedding,
    )
    await h.register_composite(session, op_id, handler, module=_MODULE)


async def test_second_step_through_dispatch_is_refused_once_the_person_is_blocked(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    await _register(session, embedding, _TWO_STEPS_OP_ID, "two_child_steps_composite")
    between_steps.append(_block)

    result = await h.run(_TWO_STEPS_OP_ID)

    assert sent == [f"{_STEP_OP_ID}:1"]
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "uncertain"


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        pytest.param(_GOVERNED_OP_ID, "governed_step_composite", id="run_governed_subop"),
        pytest.param(_GATED_OP_ID, "gated_step_composite", id="enforce_subop_policy"),
    ],
)
async def test_governed_step_of_a_blocked_person_is_not_sent(
    session: AsyncSession, embedding: AsyncMock, op_id: str, handler: str
) -> None:
    await _register(session, embedding, op_id, handler)
    between_steps.append(_block)

    result = await h.run(op_id)

    assert sent == []
    assert isinstance(result, OperationResult)
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "not_started"


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        pytest.param(_GOVERNED_OP_ID, "governed_step_composite", id="run_governed_subop"),
        pytest.param(_GATED_OP_ID, "gated_step_composite", id="enforce_subop_policy"),
    ],
)
async def test_governed_step_is_refused_when_the_block_list_cannot_be_read(
    session: AsyncSession,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler: str,
) -> None:
    await _register(session, embedding, op_id, handler)
    between_steps.append(_unreadable(monkeypatch))

    result = await h.run(op_id)

    assert sent == []
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert result.extras["execution_outcome"] == "not_started"


async def _block_requester(_operator: Operator) -> None:
    await block_principal(_admin(), h.operator().sub, "left the team")


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        pytest.param(
            _WAIT_THEN_CHILD_OP_ID, "wait_then_child_steps_composite", id="dispatch_child"
        ),
        pytest.param(
            _WAIT_THEN_GOVERNED_OP_ID, "wait_then_governed_step_composite", id="run_governed_subop"
        ),
    ],
)
async def test_approver_run_stops_once_the_person_who_asked_is_blocked(
    session: AsyncSession, embedding: AsyncMock, op_id: str, handler: str
) -> None:
    await _register(session, embedding, op_id, handler)
    first = await h.run(op_id)
    assert first.status == "awaiting_approval", first
    request = await h.approve(UUID(first.extras["approval_request_id"]), sub="op-approver")
    between_steps.append(_block_requester)

    result = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )

    assert sent == [f"{_STEP_OP_ID}:1"]
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "uncertain"


@pytest.mark.parametrize(
    ("op_id", "handler", "outcome"),
    [
        pytest.param(h.WAIT_OP_ID, "step_waits_composite", "uncertain", id="after-a-step"),
        pytest.param(
            _WAIT_THEN_CHILD_OP_ID,
            "wait_then_child_steps_composite",
            "not_started",
            id="at-the-first-step",
        ),
    ],
)
@pytest.mark.parametrize("case", ["blocked", "unreadable"])
async def test_approved_run_refused_after_a_wait_says_if_a_step_was_sent(
    session: AsyncSession,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler: str,
    outcome: str,
    case: str,
) -> None:
    await _register(session, embedding, _TWO_STEPS_OP_ID, "two_child_steps_composite")
    module = h.__name__ if op_id == h.WAIT_OP_ID else _MODULE
    await h.register_composite(session, op_id, handler, module=module)
    first = await h.run(op_id)
    assert first.status == "awaiting_approval", first
    request = await h.approve(UUID(first.extras["approval_request_id"]), sub="op-approver")
    if case == "blocked":
        await _block_requester(h.operator())
    else:
        await _unreadable(monkeypatch)(h.operator())

    result = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )

    assert sent == []
    assert result.status == ("denied" if case == "blocked" else "error"), result
    assert result.extras["execution_outcome"] == outcome
    assert ("Try again" in str(result.error)) is (case == "unreadable" and outcome == "not_started")


def _assert_refused_after_an_earlier_step(result: OperationResult) -> None:
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert result.extras["execution_outcome"] == "uncertain"
    assert "Try again" not in str(result.error)


async def test_no_later_step_through_dispatch_is_sent_after_a_refused_step(
    session: AsyncSession, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(session, embedding, _PARALLEL_OP_ID, "parallel_parts_composite")
    await h.register_composite(session, _NESTED_OP_ID, "nested_part_composite", module=_MODULE)
    nested_events.extend([asyncio.Event(), asyncio.Event()])
    after_refusal.append(_readable(monkeypatch))
    between_steps.append(_unreadable(monkeypatch))

    result = await h.run(_PARALLEL_OP_ID)

    assert sent == [f"{_STEP_OP_ID}:1"]
    _assert_refused_after_an_earlier_step(result)


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        pytest.param(
            _THREE_GOVERNED_OP_ID, "three_governed_steps_composite", id="run_governed_subop"
        ),
        pytest.param(_THREE_GATED_OP_ID, "three_gated_steps_composite", id="enforce_subop_policy"),
    ],
)
async def test_no_later_governed_step_is_sent_after_a_refused_step(
    session: AsyncSession,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler: str,
) -> None:
    await _register(session, embedding, op_id, handler)
    after_refusal.append(_readable(monkeypatch))
    between_steps.append(_unreadable(monkeypatch))

    result = await h.run(op_id)

    assert sent == [f"{_DIRECT_STEP}:1"]
    _assert_refused_after_an_earlier_step(result)


def _break_list_after_the_asker_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run claim comes after the check of the person who asked; the list breaks there."""
    claim = approval_queue.claim_resume

    async def _claim_then_break(request_id: UUID) -> bool:
        claimed = await claim(request_id)
        await _unreadable(monkeypatch)(h.operator())
        return claimed

    monkeypatch.setattr(approval_queue, "claim_resume", _claim_then_break)


@pytest.mark.parametrize(
    ("op_id", "handler", "outcome"),
    [
        pytest.param(h.WAIT_OP_ID, "step_waits_composite", "uncertain", id="after-a-step"),
        pytest.param(
            _WAIT_THEN_CHILD_OP_ID,
            "wait_then_child_steps_composite",
            "not_started",
            id="at-the-first-step",
        ),
        pytest.param(
            _STEP_THEN_NESTED_WAIT_OP_ID,
            "step_then_nested_wait_composite",
            "uncertain",
            id="nested-part-after-a-step",
        ),
    ],
)
@pytest.mark.parametrize("path", ["approver", "agent_waiter"])
async def test_approved_run_refused_at_its_start_says_if_a_step_was_sent(
    session: AsyncSession,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler: str,
    outcome: str,
    path: str,
) -> None:
    await _register(session, embedding, _TWO_STEPS_OP_ID, "two_child_steps_composite")
    module = h.__name__ if op_id == h.WAIT_OP_ID else _MODULE
    await h.register_composite(session, op_id, handler, module=module)
    if op_id == _STEP_THEN_NESTED_WAIT_OP_ID:
        await h.register_composite(
            session, _WAIT_THEN_CHILD_OP_ID, "wait_then_child_steps_composite", module=_MODULE
        )
    asker = h.operator()
    if path == "agent_waiter":
        asker = h.operator(sub="agent-requester", principal_kind=PrincipalKind.AGENT)
        await give_agent_a_known_person(h.TENANT_ID, "agent-requester")
    first = await h.run(op_id, asker)
    assert first.status == "awaiting_approval", first
    request_id = UUID(first.extras["approval_request_id"])
    request = await h.approve(request_id, sub="op-approver")
    sent.clear()
    _break_list_after_the_asker_check(monkeypatch)

    if path == "approver":
        result = await resume_dispatch_after_approval(
            operator=h.operator(sub="op-approver"), request=request, params=None
        )
        answer = result.model_dump(mode="json")
    else:
        answer = await resume_or_surface_awaiting_approval(
            operator=asker,
            call_arguments={
                "connector_id": h.CONNECTOR_ID,
                "op_id": op_id,
                "params": {},
                "target": None,
            },
            awaiting_envelope=first.model_dump(mode="json"),
            timeout_seconds=2.0,
        )

    assert sent == []
    stored = (await h.request(request_id)).resume_result
    assert stored is not None
    for envelope in (answer, stored):
        assert envelope["status"] == "error", envelope
        assert envelope["extras"]["error_code"] == "principal_block_check_unavailable"
        assert envelope["extras"]["execution_outcome"] == outcome
        assert ("Try again" in envelope["error"]) is (outcome == "not_started")
        assert (envelope["extras"].get("retry") is False) is (outcome == "uncertain")
