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

Every call with a key takes the lock of that key first
(``pg_advisory_xact_lock``), so the second call looks the key up only after
the first one committed. A helper lets each retry wait a short time after
its first key lookup for the other one: the two meet there when the lock is
missing, and the test then shows two requests.

A late retry for another call, asked another way, meets an approve the
same way. It must wait for the approve to commit and then be refused: the
approved request still holds the key.

A third race is two retries whose key a request of other people behind
the login holds. Each gets its own request beside the holder; the two end
with one new request. A fourth one rejects the holder while the first
retry stores its request: the second retry still gets that request.

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
import contextlib
import contextvars
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestKey,
    ApprovalRequestStatus,
    AuditLog,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    IdempotencyKeyConflictError,
    approve_request,
    create_pending_request,
    reject_request,
)
from tests._people_behind_helpers import give_known_person

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


async def _submit(
    *,
    expires_at: datetime | None = None,
    target_id: uuid.UUID | None = None,
    reveal_secret: bool = False,
) -> ApprovalRequest:
    """One ``call_operation`` park with the shared key, in its own transaction."""
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_requester(),
            connector_id="rectest-1.x",
            op_id=_OP_ID,
            target=SimpleNamespace(id=target_id) if target_id is not None else None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            expires_at=expires_at,
            idempotency_key=_KEY,
            reveal_secret=reveal_secret,
            proposed_effect={"safety_level": "caution"},
        )
        await session.commit()
    return request


def _wait_for_the_other_after_the_first_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let each of two retries wait up to two seconds after its first key lookup.

    Without the key lock both retries look the key up before either stores a
    request, and they meet here. With the lock the second retry waits for the
    first one's commit before its lookup, so the first one waits here alone
    until the time is up.
    """
    real_find = approval_queue_module._find_idempotent_request
    lookups = {"n": 0}
    both_looked = asyncio.Event()

    async def _find_then_wait(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        found = await real_find(*args, **kwargs)
        lookups["n"] += 1
        if lookups["n"] == 2:
            both_looked.set()
        elif lookups["n"] == 1:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_looked.wait(), timeout=2)
        return found

    monkeypatch.setattr(approval_queue_module, "_find_idempotent_request", _find_then_wait)


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

    The winner takes the key lock, expires the row, parks the replacement
    and commits. The other retry then looks the key up and gets the
    replacement as a reused request. Neither caller gets the expired id. One expiry, one new
    request, one audit row each.
    """
    past = datetime.now(UTC) - timedelta(hours=1)
    overdue = await _submit(expires_at=past)

    _wait_for_the_other_after_the_first_lookup(monkeypatch)

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
    await give_known_person(_requester())
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


@_skip_no_docker
async def test_a_late_retry_for_another_call_that_waits_on_an_approval_is_refused(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late retry for another call, asked another way, waits for the approve and is refused.

    The approve starts before the deadline and holds the row lock. The
    retry runs after the deadline with another target and
    ``reveal_secret``, so it does not get the request back. It reads the
    row as overdue, locks it again and waits for the approve to commit.
    The approved request still holds the key for another call, so the
    retry is refused and MEHO stores no second request.
    """
    await give_known_person(_requester())
    original = await _submit(expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert original.expires_at is not None
    late: datetime = original.expires_at + timedelta(seconds=1)
    real_now = approval_queue_module._now
    clock = {"now": real_now}
    monkeypatch.setattr(approval_queue_module, "_now", lambda: clock["now"]())

    about_to_lock = asyncio.Event()
    real_lock = approval_queue_module._holds_key_after_lock

    async def _signal_then_lock(*args: Any, **kwargs: Any) -> bool:
        about_to_lock.set()
        return await real_lock(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "_holds_key_after_lock", _signal_then_lock)

    async with get_sessionmaker()() as approver:
        # Before the deadline: approve and hold the row lock, do not commit yet.
        await approve_request(approver, original.id, operator=_reviewer(), params=None)
        await approver.flush()

        clock["now"] = lambda: late
        retry_task = asyncio.create_task(_submit(target_id=uuid.uuid4(), reveal_secret=True))
        await asyncio.wait_for(about_to_lock.wait(), timeout=10)
        # Give the retry time to block on the lock before the approve commits.
        await asyncio.sleep(0.5)
        assert not retry_task.done()
        await approver.commit()

        with pytest.raises(IdempotencyKeyConflictError) as refused:
            await asyncio.wait_for(retry_task, timeout=10)

    assert refused.value.request_id == original.id
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ApprovalRequest).where(ApprovalRequest.tenant_id == _TENANT)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.id, row.status) for row in rows] == [
            (original.id, ApprovalRequestStatus.APPROVED.value)
        ]

    decisions = await _audit_rows("approval.decision", original.id)
    assert [d.payload.get("decision") for d in decisions] == ["approved"]


@_skip_no_docker
async def test_two_retries_beside_a_holder_of_other_people_get_one_request(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent retries with new people behind the login end with one new request.

    The key is held by a request asked with person-a behind the login. Both
    retries run with person-b behind it, so neither may get that request.
    The first one stores its own request with the key in its linked row.
    The second one waits for the key lock, then finds that request through
    the linked key.
    """
    behind = {"subs": ["person-a"]}

    async def _people(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"sub": sub, "name": None, "why": "agent_owner"} for sub in behind["subs"]]

    monkeypatch.setattr(approval_queue_module, "find_people_behind", _people)
    holder = await _submit()
    behind["subs"] = ["person-b"]

    _wait_for_the_other_after_the_first_lookup(monkeypatch)

    one, two = await asyncio.wait_for(asyncio.gather(_submit(), _submit()), timeout=20)

    assert one.id == two.id
    assert one.id != holder.id
    assert sorted(getattr(r, "_reused", False) for r in (one, two)) == [False, True]

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ApprovalRequest).where(ApprovalRequest.tenant_id == _TENANT)
                )
            )
            .scalars()
            .all()
        )
        assert {row.id: row.idempotency_key for row in rows} == {holder.id: _KEY, one.id: None}
        links = (
            (
                await session.execute(
                    select(ApprovalRequestKey).where(
                        ApprovalRequestKey.approval_request_id.in_([holder.id, one.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(link.approval_request_id, link.idempotency_key) for link in links] == [
            (one.id, _KEY)
        ]
    assert len(await _audit_rows("approval.request", one.id)) == 1


@_skip_no_docker
async def test_a_holder_rejected_while_two_retries_run_leaves_one_request(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two retries end with one new request when the holder is rejected in between.

    The key is held by a request asked with person-a behind the login; both
    retries run with person-b behind it. The first retry looks the key up,
    finds nothing it may get, and tries to store its request: the holder
    still has the key. A reviewer rejects the holder right after that retry
    looked the key up again and before it stores its request. The
    second retry then gets the first one's request, and only that request
    can be approved.
    """
    behind = {"subs": ["person-a"]}

    async def _people(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"sub": sub, "name": None, "why": "agent_owner"} for sub in behind["subs"]]

    monkeypatch.setattr(approval_queue_module, "find_people_behind", _people)
    holder = await _submit()
    behind["subs"] = ["person-b"]

    real_find = approval_queue_module._find_idempotent_request
    real_insert = approval_queue_module._insert_idempotent_request
    storing: contextvars.ContextVar[bool] = contextvars.ContextVar("storing", default=False)
    lookups = {"n": 0}
    both_looked = asyncio.Event()
    rejected = asyncio.Event()

    async def _reject_holder() -> None:
        async with get_sessionmaker()() as session:
            await reject_request(session, holder.id, operator=_reviewer())
            await session.commit()
        rejected.set()

    async def _find_then_reject(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        found = await real_find(*args, **kwargs)
        lookups["n"] += 1
        if lookups["n"] == 1:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_looked.wait(), timeout=2)
        else:
            both_looked.set()
        if storing.get() and found is None and not rejected.is_set():
            # The insert met the holder and looked the key up again.
            await _reject_holder()
        return found

    async def _store(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        token = storing.set(True)
        try:
            return await real_insert(*args, **kwargs)
        finally:
            storing.reset(token)

    monkeypatch.setattr(approval_queue_module, "_find_idempotent_request", _find_then_reject)
    monkeypatch.setattr(approval_queue_module, "_insert_idempotent_request", _store)

    one, two = await asyncio.wait_for(asyncio.gather(_submit(), _submit()), timeout=20)

    assert rejected.is_set()
    assert one.id == two.id
    assert one.id != holder.id
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ApprovalRequest).where(ApprovalRequest.tenant_id == _TENANT)
                )
            )
            .scalars()
            .all()
        )
        assert {row.id: row.status for row in rows} == {
            holder.id: ApprovalRequestStatus.REJECTED.value,
            one.id: ApprovalRequestStatus.PENDING.value,
        }
    assert len(await _audit_rows("approval.request", one.id)) == 1
