# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A credential step after an approved step waits for its own approval.

A service login runs a multi-step operation. Its first step waits for a
person. A person approves that step in the web interface, and the run goes
on with that person's rights. The later step makes a credential
(``vault.token.create``) and is declared ``safe``, so the approving person's
own rights would let it run. It still waits for its own approval, as a step
(:func:`~meho_backplane.operations.composite.enforce_subop_policy`) and as a
child call (``dispatch_child``). The approved step itself stays cleared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_COMPOSITE = "vault.composite.wait_then_credential"
_CHILD_COMPOSITE = "vault.composite.wait_then_credential_child"
_CREDENTIAL = "vault.token.create"
_AT_CREDENTIAL = {"at": "credential"}

ran: list[str] = []


async def _first_step(operator: Operator, target: Any, composite_op_id: str) -> Any:
    """The first step waits for a person; it records itself in :data:`ran` when it runs."""
    if composite_resume_state(composite_op_id) is not None:
        return None
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is None:
        ran.append(h.STEP_A)
    return gated


async def wait_then_credential_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A step that waits, then a ``safe`` step that makes a credential."""
    gated = await _first_step(operator, target, _COMPOSITE)
    if gated is not None:
        return gated
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_CREDENTIAL,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"policies": ["default"]},
        resume_state=_AT_CREDENTIAL,
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    ran.append(_CREDENTIAL)
    return {"done": True}


async def wait_then_credential_child_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits, then a child call that makes a credential."""
    gated = await _first_step(operator, target, _CHILD_COMPOSITE)
    if gated is not None:
        return gated
    return await dispatch_child(
        connector_id=h.CONNECTOR_ID, op_id=_CREDENTIAL, params={"policies": ["default"]}
    )


async def credential_child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append(_CREDENTIAL)
    return {"made": True}


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    ran.clear()
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


def _web_approver() -> Operator:
    """A person who approves in the web interface, with the rights to run a ``safe`` step."""
    return Operator(
        sub="op-approver",
        name="Web Approver",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        approver=True,
        web_session=True,
    )


async def _register(session: AsyncSession, stub: AsyncMock, op_id: str, handler: str) -> None:
    await h.register_child(stub)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_CREDENTIAL,
        handler=credential_child_handler,
        summary="Make a token.",
        description="Make a token.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub,
        safety_level="safe",
    )
    await h.register_composite(session, op_id, "step_waits_composite")
    async with get_sessionmaker()() as s, s.begin():
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
        ).scalar_one()
        row.handler_ref = f"{__name__}.{handler}"


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(ApprovalRequest))).scalars().all()
        return sorted(rows, key=lambda r: r.created_at)


async def _approve_first_step_in_the_web_interface(op_id: str) -> Any:
    """A service login's run waits on its first step; a person approves it on the web."""
    service = h.operator(sub="service-requester", principal_kind=PrincipalKind.SERVICE)
    first = await h.run(op_id, service)
    assert first.status == "awaiting_approval", first.error
    [waiting] = await _requests()
    assert waiting.op_id == h.STEP_A
    request = await h.approve(UUID(first.extras["approval_request_id"]))
    return await resume_dispatch_after_approval(
        operator=_web_approver(), request=request, params=None
    )


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        (_COMPOSITE, "wait_then_credential_composite"),
        (_CHILD_COMPOSITE, "wait_then_credential_child_composite"),
    ],
    ids=["step", "child-call"],
)
async def test_a_later_credential_step_waits_after_a_web_approval(
    stub_embedding_service: AsyncMock, session: AsyncSession, op_id: str, handler: str
) -> None:
    await _register(session, stub_embedding_service, op_id, handler)

    second = await _approve_first_step_in_the_web_interface(op_id)

    assert second.status == "awaiting_approval", second.error
    assert ran == [h.STEP_A]
    first_request, credential_request = await _requests()
    assert first_request.op_id == h.STEP_A
    assert credential_request.op_id == _CREDENTIAL
    assert str(credential_request.id) == second.extras["approval_request_id"]


async def test_the_credential_step_runs_once_after_its_own_approval(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    await _register(session, stub_embedding_service, _COMPOSITE, "wait_then_credential_composite")
    second = await _approve_first_step_in_the_web_interface(_COMPOSITE)
    assert second.status == "awaiting_approval", second.error

    request = await h.approve(UUID(second.extras["approval_request_id"]))
    third = await resume_dispatch_after_approval(
        operator=_web_approver(), request=request, params=None
    )

    assert third.status == "ok", third.error
    assert ran == [h.STEP_A, _CREDENTIAL]
    assert len(await _requests()) == 2


async def test_the_approval_of_a_waiting_child_call_runs_only_the_child(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    await _register(
        session, stub_embedding_service, _CHILD_COMPOSITE, "wait_then_credential_child_composite"
    )
    second = await _approve_first_step_in_the_web_interface(_CHILD_COMPOSITE)
    assert second.status == "awaiting_approval", second.error
    runs_before = len(await h.rows(_CHILD_COMPOSITE))

    request = await h.approve(UUID(second.extras["approval_request_id"]), sub="second-approver")
    assert request.resume_parent is None
    third = await resume_dispatch_after_approval(
        operator=_web_approver(), request=request, params=None
    )

    assert third.status == "ok", third.error
    assert ran == [h.STEP_A, _CREDENTIAL]
    assert len(await h.rows(_CHILD_COMPOSITE)) == runs_before
