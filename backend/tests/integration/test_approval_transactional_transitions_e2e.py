# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres concurrency check for transactional approval transitions (F12 / #274).

Security review finding F12 (meho-internal#274). The approval decision paths
(approve / reject) load the row ``SELECT ... FOR UPDATE`` and the expiry sweep
uses ``FOR UPDATE SKIP LOCKED``, so a competing approve / reject / expiry on
one row serialises on the row lock and yields exactly **one** winning
transition. The claim primitive
(:func:`~meho_backplane.operations.approval_queue.claim_resume`) additionally
requires the ``approved`` state in the same conditional UPDATE, so no execution
can follow a losing / rejected / expired transition.

Correctness under a genuine race depends on the database serialising concurrent
writers to the same row — a property SQLite (the unit-suite driver) cannot
exercise the way production Postgres does; ``FOR UPDATE`` / ``SKIP LOCKED`` are
silent no-ops there. This suite boots a real Postgres and proves the invariant
under true concurrency, mirroring the sibling exactly-one-resumer suite
(:mod:`tests.integration.test_approval_exactly_one_resumer_e2e`):

* **approve vs reject** — two concurrent decisions on one in-window pending row
  yield exactly one committed decision, one decision audit row, and one terminal
  status; the loser sees the already-decided guard.
* **no execution for the loser** — after the race, the execution claim succeeds
  **iff** the winning state is ``approved``.
* **reject vs expiry** — a concurrent reject and expiry sweep on one overdue row
  yield exactly one terminal transition (rejected XOR expired) and one decision
  audit row.
* **overdue is un-approvable** — the decision-time deadline gate refuses an
  overdue row on real Postgres even with no sweep, and the reviewer /
  self-approval / hash checks still fire.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRequestAlreadyDecidedError,
    ApprovalRequestExpiredError,
    ParamsMismatchError,
    SelfApprovalForbiddenError,
    approve_request,
    claim_resume,
    create_pending_request,
    expire_stale_requests,
    reject_request,
)

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _operator(*, sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Transition Race Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _commit_pending(
    *,
    requester_sub: str,
    op_id: str,
    expires_at: datetime | None = None,
    params: dict[str, object] | None = None,
) -> ApprovalRequest:
    """Insert + commit a pending request in Postgres and return the row."""
    requester = _operator(sub=requester_sub, kind=PrincipalKind.AGENT)
    call_params = params if params is not None else {"path": "secret/agent"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="rectest-1.x",
            op_id=op_id,
            target=None,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            expires_at=expires_at,
        )
        await session.commit()
    return request


async def _decision_audit_rows(request_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


async def _reload(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


@_skip_no_docker
async def test_concurrent_approve_reject_yields_one_winning_decision(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """A concurrent approve + reject on one row commit exactly one decision (F12).

    Both decisions load the row ``FOR UPDATE``, so they serialise: the winner
    commits its transition + one decision audit row, and the loser blocks on
    the lock, re-reads the committed terminal state, and hits the already-
    decided guard. The final row carries exactly one terminal status, and no
    execution can follow a losing / rejected state — the post-race claim
    succeeds iff the winner was ``approved``.
    """
    request = await _commit_pending(requester_sub="agent:race", op_id="rectest.approve-reject")
    reviewer_a = _operator(sub="human:approver")
    reviewer_b = _operator(sub="human:rejecter")

    async def _do_approve() -> str:
        async with get_sessionmaker()() as session:
            try:
                await approve_request(session, request.id, operator=reviewer_a, params=None)
                await session.commit()
                return "approved"
            except ApprovalRequestAlreadyDecidedError:
                return "lost"

    async def _do_reject() -> str:
        async with get_sessionmaker()() as session:
            try:
                await reject_request(session, request.id, operator=reviewer_b)
                await session.commit()
                return "rejected"
            except ApprovalRequestAlreadyDecidedError:
                return "lost"

    outcomes = await asyncio.gather(_do_approve(), _do_reject())

    winners = [o for o in outcomes if o != "lost"]
    assert len(winners) == 1, outcomes
    assert outcomes.count("lost") == 1, outcomes

    row = await _reload(request.id)
    assert row.status in {
        ApprovalRequestStatus.APPROVED.value,
        ApprovalRequestStatus.REJECTED.value,
    }
    assert row.status == (
        ApprovalRequestStatus.APPROVED.value
        if winners[0] == "approved"
        else ApprovalRequestStatus.REJECTED.value
    )

    # Exactly one decision audit row committed — no conflicting second record.
    assert len(await _decision_audit_rows(request.id)) == 1

    # No execution follows a losing / rejected state: the claim latches the
    # row iff it committed ``approved``.
    claimed = await claim_resume(request.id)
    assert claimed is (row.status == ApprovalRequestStatus.APPROVED.value)


@_skip_no_docker
async def test_concurrent_reject_expire_yields_one_terminal_transition(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """A concurrent reject + expiry sweep on one overdue row transition it once (F12).

    The reject loads the row ``FOR UPDATE``; the sweep selects ``FOR UPDATE
    SKIP LOCKED``. Whichever acquires the row first wins its transition and
    writes the single decision audit row; the other either skips the locked
    row (sweep) or re-reads the committed terminal state (reject → already
    decided). The row ends in exactly one terminal state — rejected XOR
    expired — never both.
    """
    past = datetime.now(UTC) - timedelta(hours=1)
    request = await _commit_pending(
        requester_sub="agent:race", op_id="rectest.reject-expire", expires_at=past
    )
    rejecter = _operator(sub="human:rejecter")
    sweeper = _operator(sub="system:approval-expiry")

    async def _do_reject() -> str:
        async with get_sessionmaker()() as session:
            try:
                await reject_request(session, request.id, operator=rejecter)
                await session.commit()
                return "rejected"
            except ApprovalRequestAlreadyDecidedError:
                return "lost"

    async def _do_expire() -> str:
        async with get_sessionmaker()() as session:
            expired = await expire_stale_requests(session, operator=sweeper)
            await session.commit()
            return "expired" if any(r.id == request.id for r in expired) else "skipped"

    outcomes = await asyncio.gather(_do_reject(), _do_expire())

    row = await _reload(request.id)
    assert row.status in {
        ApprovalRequestStatus.REJECTED.value,
        ApprovalRequestStatus.EXPIRED.value,
    }
    # Exactly one decision audit row — one winning transition, one record.
    assert len(await _decision_audit_rows(request.id)) == 1
    # The winner's outcome matches the committed status; the loser no-op'd.
    if row.status == ApprovalRequestStatus.REJECTED.value:
        assert "rejected" in outcomes
    else:
        assert "expired" in outcomes
    # The row is not executable in either terminal state.
    assert await claim_resume(request.id) is False


@_skip_no_docker
async def test_overdue_request_unapprovable_and_checks_preserved(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """The deadline gate + reviewer / self-approval / hash checks hold on Postgres (F12).

    An overdue pending row cannot be approved even with no sweep running, and
    the pre-existing precondition checks (self-approval, params-hash) still
    fire on the real database.
    """
    # Overdue row: refused by the decision-time deadline gate, no sweep needed.
    overdue = await _commit_pending(
        requester_sub="agent:overdue",
        op_id="rectest.overdue",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    reviewer = _operator(sub="human:approver")
    async with get_sessionmaker()() as session:
        with pytest.raises(ApprovalRequestExpiredError):
            await approve_request(session, overdue.id, operator=reviewer, params=None)
    assert (await _reload(overdue.id)).status == ApprovalRequestStatus.PENDING.value

    # Self-approval is still refused (requester == approver) on an in-window row.
    params: dict[str, object] = {"path": "secret/agent"}
    self_row = await _commit_pending(
        requester_sub="agent:selfapprove", op_id="rectest.self", params=params
    )
    requester_as_reviewer = _operator(sub="agent:selfapprove", kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(session, self_row.id, operator=requester_as_reviewer, params=None)

    # A params-hash mismatch is still refused on the REST (params-bearing) path.
    async with get_sessionmaker()() as session:
        with pytest.raises(ParamsMismatchError):
            await approve_request(
                session, self_row.id, operator=reviewer, params={"path": "secret/other"}
            )
