# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :func:`meho_backplane.operations.composite.enforce_subop_policy`.

Task #2254 (Initiative #2249, Goal #2247). The seam re-applies the
dispatcher's policy/approval gate around a direct-session write sub-op so
a write composite that has migrated off ``dispatch_child`` keeps property
3 of #508's four guarantees — a now-internal write sub-op that is itself
approval-gated still queues for approval instead of executing.

Coverage
--------

* **queues on needs-approval** -- a ``requires_approval`` sub-op routed
  through the seam writes a durable pending :class:`ApprovalRequest` and
  returns an ``awaiting_approval`` result (the hard acceptance gate).
* **proceeds on auto-execute** -- a ``safe`` / non-approval sub-op
  returns ``None`` so the handler runs its direct session call.
* **denies without lowering governance** -- a ``dangerous`` sub-op with
  no grant (agent principal) returns ``denied``; the write never runs.
* **two-world purity** -- the seam never persists an
  :class:`EndpointDescriptor` row for the synthetic sub-op descriptor.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    EndpointDescriptor,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000024a4")
_CONNECTOR_ID = "vmware-rest-9.0"
_SUB_OP_ID = "POST:/vcenter/vm"
_SUB_PARAMS = {"spec": {"name": "vm-under-test", "guest_OS": "OTHER"}}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open a session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(
    *,
    principal_kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub="ops-operator-sub",
        name="Ops Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=principal_kind,
    )


@pytest.mark.asyncio
async def test_gated_subop_queues_for_approval(session: AsyncSession) -> None:
    """A ``requires_approval`` write sub-op queues instead of executing.

    The hard acceptance gate for #2254: an approval-gated sub-op called
    via the direct-session seam produces a durable pending
    :class:`ApprovalRequest` and returns ``awaiting_approval`` — the
    handler returns that verbatim, so no write happens.
    """
    result = await enforce_subop_policy(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_SUB_OP_ID,
        safety_level="dangerous",
        requires_approval=True,
        target=None,
        params=_SUB_PARAMS,
    )

    # The seam signalled "do not execute" by returning a result.
    assert result is not None
    assert result.status == "awaiting_approval"
    assert result.op_id == _SUB_OP_ID
    request_id = uuid.UUID(result.extras["approval_request_id"])

    # A durable pending approval row was written for the sub-op.
    row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.op_id == _SUB_OP_ID
    assert row.connector_id == _CONNECTOR_ID
    assert row.status == ApprovalRequestStatus.PENDING.value
    assert row.params == _SUB_PARAMS
    assert row.principal_sub == "ops-operator-sub"


@pytest.mark.asyncio
async def test_safe_subop_auto_executes(session: AsyncSession) -> None:
    """A ``safe`` non-approval sub-op clears the gate (returns ``None``)."""
    result = await enforce_subop_policy(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="GET:/vcenter/vm",
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={"filter.names": ["vm-under-test"]},
    )
    assert result is None

    # No approval row was created for an auto-execute verdict.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0


@pytest.mark.asyncio
async def test_dangerous_subop_denied_for_agent_without_grant(
    session: AsyncSession,
) -> None:
    """A ``dangerous`` sub-op with no grant denies — governance not lowered.

    For an agent principal the ``dangerous`` safety default is ``deny``.
    The seam surfaces that as a ``denied`` result the handler returns
    verbatim, so the write never runs and the sub-op cannot execute at a
    lower governance level than it had through ``dispatch_child``.
    """
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id="DELETE:/vcenter/vm/{vm}",
        safety_level="dangerous",
        requires_approval=False,
        target=None,
        params={"vm": "vm-42"},
    )
    assert result is not None
    assert result.status == "denied"
    assert result.op_id == "DELETE:/vcenter/vm/{vm}"

    # No pending row: a deny is not a park.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0


async def _seed_needs_approval_grant(op_pattern: str) -> None:
    """Insert one ``needs-approval`` grant for the test operator's sub."""
    async with get_sessionmaker()() as s:
        s.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub="ops-operator-sub",
                op_pattern=op_pattern,
                verdict="needs-approval",
                created_by_sub="admin",
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_needs_approval_grant_parks_agent_but_not_user(session: AsyncSession) -> None:
    """A grant gates the agent-kind dispatch only; a user-kind one is untouched.

    Pins the ``principal_kind=agent``-only enforcement scope (#2489): a
    ``needs-approval`` grant on a ``safe`` op (whose no-grant default is
    ``auto-execute``) parks the **agent** principal's dispatch, but the
    identical grant has **zero** effect on a ``user`` principal's dispatch
    of the same op — the non-agent branch never loads an
    ``agent_permission`` row (G11.2-T3). If the grant leaked into the
    user branch, the user dispatch would park too and this test would
    fail — exactly the misdiagnosed "grant on my own sub does not gate me"
    dogfood report, held as a contract.
    """
    op_id = "GET:/vcenter/vm"
    await _seed_needs_approval_grant(op_pattern="GET:/vcenter/*")

    # Agent-kind principal: the grant is consulted → the dispatch parks.
    agent_result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={"filter.names": ["vm-under-test"]},
    )
    assert agent_result is not None
    assert agent_result.status == "awaiting_approval"

    # User-kind principal: the grant is NOT consulted → the safe op
    # auto-executes (the seam returns None), and no approval row is written
    # for it.
    user_result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.USER),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={"filter.names": ["vm-under-test"]},
    )
    assert user_result is None

    # Exactly one pending row — from the agent dispatch, never the user one.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 1


@pytest.mark.asyncio
async def test_seam_persists_no_descriptor_row(session: AsyncSession) -> None:
    """The synthetic sub-op descriptor is never written to the catalog.

    Two-world purity (Goal #2247): the in-memory descriptor exists only
    to feed ``policy_gate`` the sub-op's declared governance; it must not
    land as an ``endpoint_descriptor`` row.
    """
    before = await session.scalar(select(func.count()).select_from(EndpointDescriptor))
    await enforce_subop_policy(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_SUB_OP_ID,
        safety_level="dangerous",
        requires_approval=True,
        target=None,
        params=_SUB_PARAMS,
    )
    after = await session.scalar(select(func.count()).select_from(EndpointDescriptor))
    assert after == before
