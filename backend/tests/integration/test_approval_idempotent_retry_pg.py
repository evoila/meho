# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres checks: a late retry races an expiry or an approve (#3486).

A retry with an ``idempotency_key`` that finds its ``pending`` request past
its deadline expires that request and parks a replacement
(:func:`~meho_backplane.operations.approval_queue.create_pending_request`).
When two retries read the same overdue row at once, only one can lock it
(``SELECT ... FOR UPDATE``). The other blocks, then finds no ``pending`` row
under the lock once the winner commits. It must look the key up again and
return the winner's replacement, not the expired row it read first: the
client would otherwise hold an id that can no longer be approved.

The second race is a late retry against an approve. The approve locks the
row before the deadline and commits while the retry waits for the lock.
The retry's locked re-read misses, and its next key lookup must refresh the
row it already holds to ``approved`` and return it; a cached ``pending``
state would make it try to expire the row again, forever.

SQLite (the unit-suite driver) turns ``FOR UPDATE`` into a no-op, so the
unit suite can only simulate the stale first read
(``tests/test_approval_idempotent_park.py``). This suite boots a real
Postgres and runs the two retries on two connections, mirroring the sibling
:mod:`tests.integration.test_approval_transactional_transitions_e2e`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import approve_request, create_pending_request

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_KEY = "addon-run-7:row-42"
_OP_ID = "rectest.retry-expire"
_PARAMS: dict[str, Any] = {"name": "web-01"}


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _requester() -> Operator:
    return Operator(
        sub="agent:retry-race",
        name="Retry Race Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _reviewer() -> Operator:
    return Operator(
        sub="op-reviewer",
        name="Retry Race Reviewer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _submit(*, expires_at: datetime | None = None) -> ApprovalRequest:
    """One ``call_operation`` park with the shared key, in its own transaction."""
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_requester(),
            connector_id="rectest-1.x",
            op_id=_OP_ID,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            expires_at=expires_at,
            idempotency_key=_KEY,
        )
        await session.commit()
    return request


async def _audit_rows(path: str, request_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == path))).scalars()
        return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


@_skip_no_docker
async def test_two_retries_on_an_overdue_request_both_get_the_replacement(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent late retries end with one replacement that both callers get.

    Both retries read the overdue row before either locks it (a barrier after
    the first key lookup pins that order). The winner expires the row, parks
    the replacement and commits. The loser's ``FOR UPDATE`` re-read then
    misses, so it looks the key up again and gets the replacement as a
    reused request. Neither caller gets the expired id. One expiry, one new
    request, one audit row each.
    """
    past = datetime.now(UTC) - timedelta(hours=1)
    overdue = await _submit(expires_at=past)

    barrier = asyncio.Barrier(2)
    real_find = approval_queue_module._find_idempotent_request
    lookups = {"n": 0}

    async def _find_then_wait_for_the_other(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        found = await real_find(*args, **kwargs)
        lookups["n"] += 1
        if lookups["n"] <= 2:
            await barrier.wait()
        return found

    monkeypatch.setattr(
        approval_queue_module, "_find_idempotent_request", _find_then_wait_for_the_other
    )

    one, two = await asyncio.gather(_submit(), _submit())

    assert one.id == two.id
    assert one.id != overdue.id
    assert sorted(getattr(r, "_reused", False) for r in (one, two)) == [False, True]
    winner = one if getattr(two, "_reused", False) else two
    assert winner._expired_request.id == overdue.id  # type: ignore[attr-defined]

    async with get_sessionmaker()() as session:
        old = await session.get(ApprovalRequest, overdue.id)
        new = await session.get(ApprovalRequest, one.id)
        assert old is not None and new is not None
        assert old.status == ApprovalRequestStatus.EXPIRED.value
        assert new.status == ApprovalRequestStatus.PENDING.value
        count = await session.execute(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.tenant_id == _TENANT, ApprovalRequest.idempotency_key == _KEY)
        )
        assert count.scalar_one() == 2

    assert len(await _audit_rows("approval.decision", overdue.id)) == 1
    assert len(await _audit_rows("approval.request", one.id)) == 1


@_skip_no_docker
async def test_late_retry_that_waits_on_an_approval_gets_the_approved_row(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late retry that waits for an approve to commit gets the approved row.

    The approve starts before the deadline: it locks the row (``FOR
    UPDATE``) on one connection and holds the lock. The retry runs after
    the deadline on another connection. It reads the row as overdue and
    blocks on the lock. The approve commits. The retry's locked re-read
    then misses (the row is no longer ``pending``), so it looks the key up
    again. That lookup must refresh the row to ``approved`` and return it
    as a reuse: one row, one approve decision, no expiry.
    """
    original = await _submit(expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert original.expires_at is not None
    late: datetime = original.expires_at + timedelta(seconds=1)
    real_now = approval_queue_module._now
    clock = {"now": real_now}
    monkeypatch.setattr(approval_queue_module, "_now", lambda: clock["now"]())

    about_to_lock = asyncio.Event()
    real_lock = approval_queue_module._lock_and_expire_pending

    async def _signal_then_lock(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        about_to_lock.set()
        return await real_lock(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "_lock_and_expire_pending", _signal_then_lock)

    async with get_sessionmaker()() as approver:
        # Before the deadline: approve and hold the row lock, do not commit yet.
        await approve_request(approver, original.id, operator=_reviewer(), params=None)
        await approver.flush()

        clock["now"] = lambda: late
        retry_task = asyncio.create_task(_submit())
        await asyncio.wait_for(about_to_lock.wait(), timeout=10)
        # Give the retry time to block on the lock before the approve commits.
        await asyncio.sleep(0.5)
        assert not retry_task.done()
        await approver.commit()

        retry = await asyncio.wait_for(retry_task, timeout=10)

    assert retry.id == original.id
    assert retry.status == ApprovalRequestStatus.APPROVED.value
    assert getattr(retry, "_reused", False) is True
    assert getattr(retry, "_expired_request", None) is None

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, original.id)
        assert row is not None
        assert row.status == ApprovalRequestStatus.APPROVED.value
        assert row.reviewed_by == "op-reviewer"
        count = await session.execute(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.tenant_id == _TENANT, ApprovalRequest.idempotency_key == _KEY)
        )
        assert count.scalar_one() == 1

    decisions = await _audit_rows("approval.decision", original.id)
    assert [d.payload.get("decision") for d in decisions] == ["approved"]
    assert len(await _audit_rows("approval.request", original.id)) == 1
