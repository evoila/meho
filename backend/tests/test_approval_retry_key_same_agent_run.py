# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A retry key gives back the earlier request only for the same agent and the same run.

A call with an ``idempotency_key`` gets the request that already holds the
key only when the acting agent and the agent run are the same as at the
first call, besides the login, the people behind it and the secret choice.
A call that differs gets its own request, and the key stays linked to it.

Every call here runs through the real dispatch, every approve through
:func:`~meho_backplane.operations.approval_queue.approve_request`. Each
test runs with the switch ``APPROVAL_SAME_CALL_REUSE`` off and on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestKey, AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests.test_destructive_preview_binding import _operator

from . import _reveal_fetch_helpers as reveal_helpers
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_KEY = "retry-1"
_OP = "vault.app.rotate"
_PERSON = "person-p"
_AGENT_A = "agent:alpha"
_AGENT_B = "agent:beta"
_RUN_1 = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_RUN_2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


@pytest.fixture(autouse=True, params=["off", "on"])
def _settings_env(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_SAME_CALL_REUSE", "true" if request.param == "on" else "false")
    monkeypatch.delenv("APPROVAL_ALLOW_SELF_APPROVAL", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


async def _rotate(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"name": "app", "password": reveal_helpers.SECRET_VALUE}


@pytest.fixture
async def vault_ops() -> None:
    await reveal_helpers.register_vault_read()
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_rotate,
        summary="Rotate the app password.",
        description="Rotate the app password.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        safety_level="dangerous",
        embedding_service=reveal_helpers._embedding_service(),
    )


def _person(sub: str = _PERSON, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return _operator(sub=sub).model_copy(update={"tenant_role": role})


@contextmanager
def _as(agent: str | None, run: uuid.UUID | None) -> Iterator[None]:
    """Bind the acting agent and the agent run of a call, as an agent run does."""
    token = current_agent_run_id_var.set(run)
    try:
        if agent is None:
            yield
        else:
            with actor_delegation(agent):
                yield
    finally:
        current_agent_run_id_var.reset(token)


async def _call(
    agent: str | None = None, run: uuid.UUID | None = None, key: str = _KEY
) -> uuid.UUID:
    """Call the dangerous operation for the person; the call waits for approval."""
    with _as(agent, run):
        parked = await dispatch(
            operator=_person(),
            connector_id=reveal_helpers.CONNECTOR_ID,
            op_id=_OP,
            target=None,
            params={},
            idempotency_key=key,
        )
    assert parked.status == "awaiting_approval", parked.error
    return uuid.UUID(parked.extras["approval_request_id"])


async def _rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await session.execute(stmt)).scalars())


async def _audit_paths() -> list[str]:
    async with get_sessionmaker()() as session:
        stmt = select(AuditLog).where(AuditLog.method == "APPROVAL").order_by(AuditLog.occurred_at)
        return [row.path for row in (await session.execute(stmt)).scalars()]


async def _approve(request_id: uuid.UUID, operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approval_queue_module.approve_request(session, request_id, operator=operator)
        await session.commit()


async def _reject(request_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        await approval_queue_module.reject_request(
            session, request_id, operator=_person("reviewer", TenantRole.TENANT_ADMIN)
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_a_direct_call_and_an_agent_call_with_one_key_get_two_requests() -> None:
    direct = await _call()
    acting = await _call(_AGENT_A, _RUN_1)

    assert acting != direct
    rows = await _rows()
    assert [(row.id, row.principal_act, row.run_id) for row in rows] == [
        (direct, None, None),
        (acting, _AGENT_A, _RUN_1),
    ]
    assert await _audit_paths() == ["approval.request", "approval.request"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_an_agent_call_and_then_a_direct_call_with_one_key_get_two_requests() -> None:
    acting = await _call(_AGENT_A, _RUN_1)
    direct = await _call()

    assert direct != acting
    assert [row.id for row in await _rows()] == [acting, direct]
    assert await _audit_paths() == ["approval.request", "approval.request"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ((_AGENT_A, _RUN_1), (_AGENT_B, _RUN_2)),
        ((_AGENT_A, _RUN_1), (_AGENT_A, _RUN_2)),
        ((_AGENT_A, _RUN_1), (_AGENT_B, _RUN_1)),
        ((None, _RUN_1), (None, _RUN_2)),
    ],
)
async def test_two_agent_runs_with_one_key_get_two_requests(
    first: tuple[str | None, uuid.UUID], second: tuple[str | None, uuid.UUID]
) -> None:
    one = await _call(*first)
    two = await _call(*second)

    assert two != one
    assert [(row.principal_act, row.run_id) for row in await _rows()] == [first, second]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
@pytest.mark.parametrize(
    "caller", [(None, None), (_AGENT_A, _RUN_1), (None, _RUN_1)], ids=["direct", "agent", "run"]
)
async def test_the_same_caller_gets_its_request_back_also_after_approve(
    caller: tuple[str | None, uuid.UUID | None],
) -> None:
    first = await _call(*caller)
    assert await _call(*caller) == first
    assert len(await _rows()) == 1

    await _approve(first, _person("reviewer", TenantRole.TENANT_ADMIN))
    assert await _call(*caller) == first
    assert [row.status for row in await _rows()] == ["approved"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_after_reject_the_same_caller_gets_a_new_request() -> None:
    first = await _call(_AGENT_A, _RUN_1)
    await _reject(first)
    retry = await _call(_AGENT_A, _RUN_1)

    assert retry != first
    assert [row.status for row in await _rows()] == ["rejected", "pending"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_after_expiry_the_same_caller_gets_a_new_request() -> None:
    first = await _call(_AGENT_A, _RUN_1)
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, first)
        assert row is not None
        row.expires_at = row.created_at
        await session.commit()
    retry = await _call(_AGENT_A, _RUN_1)

    assert retry != first
    assert [row.status for row in await _rows()] == ["expired", "pending"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_the_retry_of_the_call_with_its_own_request_finds_that_request() -> None:
    first = await _call()
    second = await _call(_AGENT_A, _RUN_1)

    assert await _call(_AGENT_A, _RUN_1) == second
    assert await _call() == first
    assert len(await _rows()) == 2
    async with get_sessionmaker()() as session:
        links = (await session.execute(select(ApprovalRequestKey))).scalars().all()
    assert [(link.approval_request_id, link.idempotency_key) for link in links] == [(second, _KEY)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_the_person_behind_the_second_call_cannot_approve_it_but_another_person_can() -> None:
    await _call()
    second = await _call(_AGENT_A, _RUN_1)

    with pytest.raises(approval_queue_module.SelfApprovalForbiddenError):
        await _approve(second, _person())
    await _approve(second, _person("person-q", TenantRole.TENANT_ADMIN))
    assert [row.status for row in await _rows()] == ["pending", "approved"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_a_true_retry_writes_a_reuse_row_and_a_new_request_writes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_RETRY_OWN_AUDIT_ROW", "true")
    get_settings.cache_clear()

    await _call(_AGENT_A, _RUN_1)
    await _call(_AGENT_A, _RUN_1)
    assert await _audit_paths() == ["approval.request", "approval.reuse"]

    await _call(_AGENT_B, _RUN_2)
    assert await _audit_paths() == ["approval.request", "approval.reuse", "approval.request"]
