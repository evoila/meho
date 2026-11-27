# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step of a multi-step operation that writes or makes a credential waits for a person.

The multi-step operation is ``safe`` without the approval flag. Its one step
is sent through ``run_governed_subop`` at ``safe`` or ``caution`` without the
approval flag, and the step's op id is declared ``credential_write`` or
``credential_mint`` (or not declared: a plain step). Each case runs through
the real ``dispatch``:

* A person, with a token or in the web console, as Operator or Admin: the
  credential step waits, it is not sent, and one approval request is stored,
  for the step. After a second person approves, the step is sent once.
* An agent login, a service login and a hosted agent run: the credential
  step waits and is not sent.
* A plain step keeps its answers: at ``safe`` it is sent for every caller;
  at ``caution`` it is sent for a person in the web console and waits for
  every other caller.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation, delegated_operator
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.op_classes import _DECLARED, declare_op_classes
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import approve_request, resume_dispatch_after_approval
from meho_backplane.settings import get_settings

from . import _credential_step_always_waits_helpers as s
from ._composite_wait_audit_helpers import NoOpVaultConnector

pytestmark = pytest.mark.asyncio

_TENANT = UUID("00000000-0000-0000-0000-00000000c4e2")
_HANDLER_REF = "tests._credential_step_always_waits_helpers.one_step_composite"
_EMBEDDING = [0.1] * 384
_CREDENTIAL_STEPS = [s.WRITE_STEP, s.MINT_STEP]
_CREDENTIAL_IDS = ["credential_write", "credential_mint"]
_LEVELS = ["safe", "caution"]
_ROLES = [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN]


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    s.sent.clear()
    declare_op_classes(credential_write=(s.WRITE_STEP,), credential_mint=(s.MINT_STEP,))
    yield
    for op_id in (s.WRITE_STEP, s.MINT_STEP):
        _DECLARED.pop(op_id, None)
    s.sent.clear()
    reset_dispatcher_caches()
    clear_registry()
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _composite(_state: None) -> AsyncIterator[None]:
    register_connector_v2(product="vault", version="", impl_id="", cls=NoOpVaultConnector)
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=s.COMPOSITE_OP_ID,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref=_HANDLER_REF,
                summary="A stand-in multi-step operation.",
                description="A stand-in multi-step operation.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=_EMBEDDING,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    yield


def _login(
    *,
    sub: str = "op-person",
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.OPERATOR,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name="A Caller",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        client_id=sub if kind is not PrincipalKind.USER else None,
        web_session=web_session,
    )


async def _run(who: Operator, step: str, level: str, *, hosted: bool = False) -> OperationResult:
    acting = actor_delegation("agent-standin") if hosted else contextlib.nullcontext()
    with acting:
        return await dispatch(
            operator=delegated_operator(who) if hosted else who,
            connector_id=s.CONNECTOR_ID,
            op_id=s.COMPOSITE_OP_ID,
            target=None,
            params={"step": step, "level": level},
        )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await session.execute(stmt)).scalars().all())


async def _approve_and_resume(result: OperationResult) -> OperationResult:
    request_id = UUID(result.extras["approval_request_id"])
    approver = _login(sub="op-approver")
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    resumed: OperationResult = await resume_dispatch_after_approval(
        operator=approver, request=request, params=None
    )
    return resumed


async def _assert_waits_for_the_step(result: OperationResult, step: str) -> None:
    assert result.status == "awaiting_approval", result.error
    assert s.sent == [], "the step is not sent"
    (request,) = await _requests()
    assert request.op_id == step
    assert str(request.id) == result.extras["approval_request_id"]


@pytest.mark.parametrize("step", _CREDENTIAL_STEPS, ids=_CREDENTIAL_IDS)
@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("role", _ROLES, ids=["operator", "admin"])
@pytest.mark.parametrize("web_session", [False, True], ids=["token", "web-console"])
async def test_a_persons_credential_step_waits_and_is_sent_once_after_the_approval(
    step: str, level: str, role: TenantRole, web_session: bool
) -> None:
    first = await _run(_login(role=role, web_session=web_session), step, level)

    await _assert_waits_for_the_step(first, step)

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert s.sent == [step], "the step is sent once"
    assert len(await _requests()) == 1


_NOT_A_PERSON = {
    "agent": {"kind": PrincipalKind.AGENT, "sub": "agent-login"},
    "service": {"kind": PrincipalKind.SERVICE, "sub": "service-login"},
}


@pytest.mark.parametrize("step", _CREDENTIAL_STEPS, ids=_CREDENTIAL_IDS)
@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("caller", ["agent", "service", "hosted"])
async def test_a_credential_step_waits_for_an_agent_a_service_and_a_hosted_run(
    step: str, level: str, caller: str
) -> None:
    if caller == "hosted":
        result = await _run(_login(), step, level, hosted=True)
    else:
        login: dict[str, Any] = _NOT_A_PERSON[caller]
        result = await _run(_login(sub=login["sub"], kind=login["kind"]), step, level)

    await _assert_waits_for_the_step(result, step)


_CALLERS = ["token", "web-console", "agent", "service", "hosted"]


async def _run_as(caller: str, level: str) -> OperationResult:
    if caller == "hosted":
        return await _run(_login(), s.PLAIN_STEP, level, hosted=True)
    if caller in _NOT_A_PERSON:
        login: dict[str, Any] = _NOT_A_PERSON[caller]
        return await _run(_login(sub=login["sub"], kind=login["kind"]), s.PLAIN_STEP, level)
    return await _run(_login(web_session=caller == "web-console"), s.PLAIN_STEP, level)


@pytest.mark.parametrize("caller", _CALLERS)
async def test_a_plain_safe_step_is_sent_for_every_caller(caller: str) -> None:
    result = await _run_as(caller, "safe")

    assert result.status == "ok", result.error
    assert s.sent == [s.PLAIN_STEP]
    assert await _requests() == []


@pytest.mark.parametrize("caller", _CALLERS)
async def test_a_plain_caution_step_keeps_its_answer(caller: str) -> None:
    result = await _run_as(caller, "caution")

    if caller == "web-console":
        assert result.status == "ok", result.error
        assert s.sent == [s.PLAIN_STEP]
        assert await _requests() == []
    else:
        await _assert_waits_for_the_step(result, s.PLAIN_STEP)
