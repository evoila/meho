# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A read that a run sent before its wait, sent again after the approval.

A person starts a multi-step operation with a token. A step of it waits for
a person, a second person approves it, and the run goes on
(``resume_dispatch_after_approval``). A credential read that the run sent
before the wait is sent again then.

* Case 1: the read's policy let it run at once before the wait. It is sent
  again with no new wait, and the run ends ``ok``.
* Case 2: the read's own policy made it wait, and a person approved it. A
  later step waits. The answer is the refusal
  ``composite_resume_read_not_sent``, as with ``CUSTOM_POLICY`` off.

Each case runs with ``CUSTOM_POLICY`` on and off; the answer is the same.
All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane import op_classes
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import (
    DispatchChild,
    composite_steps,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.composite import enforce_subop_policy, run_governed_subop
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c

pytestmark = pytest.mark.asyncio

_NAMES_READ = "probe.secrets.names"
_LIST_THEN_WRITE = "probe.composite.list_then_write"
_APPROVED_READ_RUN = "probe.composite.approved_read_then_wait"
_LATER_WAIT = "probe.things.delete"
_STARTER_SUB = "person-probe-2"
_BOTH_STATES = pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])

#: The steps that reached the target, in order.
sent: list[str] = []


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # ``probe.secrets.names`` reads a credential, returns no secret value and
    # may be sent again by a resume, like ``vault.kv.list``.
    monkeypatch.setitem(op_classes._DECLARED, _NAMES_READ, "credential_read")
    monkeypatch.setattr(op_classes, "_NO_SECRET_VALUE", op_classes._NO_SECRET_VALUE | {_NAMES_READ})
    monkeypatch.setattr(
        composite_steps,
        "_READS_SAFE_TO_SEND_AGAIN",
        composite_steps._READS_SAFE_TO_SEND_AGAIN | {_NAMES_READ},
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    sent.clear()
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


async def names_read_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    sent.append(_NAMES_READ)
    return {"names": ["app-a", "app-b"]}


async def list_then_write(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """Case 1: the child read, then a step that waits for a person."""
    read = await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_NAMES_READ, params={})
    if read.status != "ok":
        return read
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is not None:
        return gated
    sent.append(h.STEP_A)
    return {"done": True}


async def _send_read() -> dict[str, Any]:
    sent.append(_NAMES_READ)
    return {"names": ["app-a"]}


async def approved_read_then_wait(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Case 2: a read that waits for its own approval, then a step that waits too."""
    read = await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_NAMES_READ,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"path": "app"},
        call=_send_read,
    )
    if read.stopped is not None:
        return read.stopped
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_LATER_WAIT,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"id": "t-1"},
    )
    if gated is not None:
        return gated
    sent.append(_LATER_WAIT)
    return {"done": True}


async def _start_and_resume(op_id: str) -> Any:
    """``person-probe-2`` starts *op_id*; ``person-probe-3`` approves its wait; the run goes on."""
    first = await h.run(op_id, h.operator(sub=_STARTER_SUB))
    assert first.status == "awaiting_approval", first.error
    # The first wait has the plain mark, so with the switch on only an Admin approves it.
    request = await h.approve(
        UUID(first.extras["approval_request_id"]),
        sub=c.APPROVER_SUB,
        role=TenantRole.TENANT_ADMIN,
    )
    return await resume_dispatch_after_approval(
        operator=c.token_approver().model_copy(update={"tenant_role": TenantRole.TENANT_ADMIN}),
        request=request,
        params=None,
    )


@_BOTH_STATES
async def test_a_read_sent_at_once_before_the_wait_is_sent_again_and_the_run_ends(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    await h.register_child(stub_embedding_service)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_NAMES_READ,
        handler=names_read_handler,
        summary="List secret names.",
        description="List secret names.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
        safety_level="safe",
        requires_approval=False,
    )
    await h.register_composite(session, _LIST_THEN_WRITE, "list_then_write", module=__name__)

    result = await _start_and_resume(_LIST_THEN_WRITE)

    assert result.status == "ok", result.error
    assert len(await c.requests()) == 1
    assert sent == [_NAMES_READ, _NAMES_READ, h.STEP_A]


@_BOTH_STATES
async def test_a_read_a_person_approved_is_not_sent_again_when_a_later_step_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    await h.register_child(stub_embedding_service)
    await h.register_composite(
        session, _APPROVED_READ_RUN, "approved_read_then_wait", module=__name__
    )

    result = await _start_and_resume(_APPROVED_READ_RUN)

    assert result.status == "error"
    assert result.extras["error_code"] == "composite_resume_read_not_sent"
    assert result.extras["blocked_op_id"] == _NAMES_READ
    assert _NAMES_READ in (result.error or "")
    assert _LATER_WAIT in (result.error or "")
    assert sent == [_NAMES_READ]
    (only,) = await c.requests()
    assert only.op_id == _NAMES_READ
