# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step run goes on or ends after a person decides on its credential step.

The step makes a credential (``vault.token.create``), at level ``safe``, so
only the credential rule makes it wait. The caller is an agent with an
``auto-execute`` grant row for the step, or a service login with a standing
grant for it. As ``docs/codebase/approvals.md`` says, the step waits for its
own approval, and the approval of the step re-runs the multi-step operation
with this one step cleared:

* the person approves: the run goes on, the step runs once, the run ends
  ``ok`` and no second approval request is made;
* the person rejects: the run ends, and the step never runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    EndpointDescriptor,
    PermissionVerdict,
    ServicePrincipalGrant,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approval_queue import reject_request, resume_dispatch_after_approval
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_COMPOSITE = "vault.composite.credential_step"
_STEP = "vault.token.create"

ran: list[str] = []


async def credential_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """One step that makes a credential; it records itself in :data:`ran` when it runs."""
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"policies": ["default"]},
    )
    if gated is not None:
        return gated
    ran.append(_STEP)
    return {"done": True}


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


async def _caller_with_a_grant(kind: PrincipalKind) -> Operator:
    """An agent with ``auto-execute`` rows, or a service login with a standing grant."""
    caller = h.operator(sub=f"{kind.value}-requester", principal_kind=kind)
    async with get_sessionmaker()() as s, s.begin():
        if kind is PrincipalKind.AGENT:
            for op_pattern in (_COMPOSITE, _STEP):
                s.add(
                    AgentPermission(
                        tenant_id=h.TENANT_ID,
                        principal_sub=caller.sub,
                        op_pattern=op_pattern,
                        verdict=PermissionVerdict.AUTO_EXECUTE.value,
                        created_by_sub="admin",
                    )
                )
        else:
            s.add(
                ServicePrincipalGrant(
                    tenant_id=h.TENANT_ID,
                    principal_sub=caller.sub,
                    op_id=_STEP,
                    connector_id=h.STEP_CONNECTOR_ID,
                    target_id=None,
                    reason="test: the credential step",
                    created_by_sub="admin",
                )
            )
    return caller


async def _first_run(session: AsyncSession, stub: AsyncMock, kind: PrincipalKind) -> UUID:
    """Run the multi-step operation once: its credential step waits for a person."""
    await h.register_child(stub)
    await h.register_composite(session, _COMPOSITE, "step_waits_composite")
    async with get_sessionmaker()() as s, s.begin():
        row = (
            await s.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _COMPOSITE)
            )
        ).scalar_one()
        row.handler_ref = f"{__name__}.credential_step_composite"
    first = await h.run(_COMPOSITE, await _caller_with_a_grant(kind))
    assert first.status == "awaiting_approval", first.error
    assert ran == []
    return UUID(first.extras["approval_request_id"])


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(ApprovalRequest))).scalars().all())


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_the_run_goes_on_after_a_person_approves_the_credential_step(
    stub_embedding_service: AsyncMock, session: AsyncSession, kind: PrincipalKind
) -> None:
    request_id = await _first_run(session, stub_embedding_service, kind)
    [waiting] = await _requests()
    assert waiting.id == request_id
    assert waiting.op_id == _STEP

    request = await h.approve(request_id)
    second = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )

    assert second.status == "ok", second.error
    assert ran == [_STEP]
    assert [r.id for r in await _requests()] == [request_id]
    runs = await h.rows(_COMPOSITE)
    assert [r.payload["result_status"] for r in runs] == ["awaiting_approval", "ok"]


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_the_run_ends_when_a_person_rejects_the_credential_step(
    stub_embedding_service: AsyncMock, session: AsyncSession, kind: PrincipalKind
) -> None:
    request_id = await _first_run(session, stub_embedding_service, kind)

    async with get_sessionmaker()() as s:
        await reject_request(s, request_id, operator=h.operator(sub="op-approver"))
        await s.commit()

    [rejected] = await _requests()
    assert rejected.status == ApprovalRequestStatus.REJECTED
    assert ran == []
    runs = await h.rows(_COMPOSITE)
    assert [r.payload["result_status"] for r in runs] == ["awaiting_approval"]
