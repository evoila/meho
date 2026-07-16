# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres concurrency check for the exactly-one-resumer claim (#2293).

Initiative #2286 (G0.30), Task #2293. The exactly-one-resumer invariant is
a single conditional ``UPDATE approval_request SET resumed_at = now WHERE
resumed_at IS NULL`` (:func:`~meho_backplane.operations.approval_queue.claim_resume`).
Its correctness under a genuine race depends on the database serialising
concurrent writers to the same row — a property SQLite (the unit-suite
driver, which serialises at the file/connection level) cannot exercise the
way the production Postgres does. This suite boots a real Postgres and
proves the claim under true concurrency, with no advisory locks:

* **The claim primitive** — several concurrent :func:`claim_resume` calls
  on one freshly-parked request yield exactly **one** winner; the rest lose
  on the row lock and re-evaluate ``resumed_at IS NULL`` to false after the
  winner commits.

* **The full resume path** — two concurrent
  :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`
  calls (standing in for the in-process agent waiter racing an operator
  approval surface) dispatch the approved op **exactly once**: one returns
  ``status="ok"`` (won the claim, executed), the other
  ``status="already_resumed"`` (lost, no-op'd). This is both polarities at
  once — the waiter-alive case executes exactly once (no double dispatch of
  the approved write, AC2) and, since a lone resumer would still win and
  execute, the waiter-gone fallback executes rather than silently skipping
  (AC3).

``dispatch`` is replaced with a call-counting spy: the claim gates whether
it is reached, so a spy count of exactly one *is* the "the approved op runs
once" assertion under the race, without needing a live upstream connector
(and avoiding the DB-target-rehydration ``no_connector`` trap #147/#2173 by
never rehydrating a live-container fixture here).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    approve_request,
    claim_resume,
    create_pending_request,
    resume_dispatch_after_approval,
)

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _make_operator(*, sub: str, kind: PrincipalKind = PrincipalKind.AGENT) -> Operator:
    return Operator(
        sub=sub,
        name="Resumer Race Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _commit_run_bound_pending(*, requester_sub: str) -> ApprovalRequest:
    """Insert + commit a run-bound (``run_id`` set) parked request in Postgres."""
    requester = _make_operator(sub=requester_sub)
    params = {"path": "secret/agent", "value": "tok"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="rectest-1.x",
            op_id="rectest.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),
        )
        await session.commit()
    return request


async def _reload_resumed_at(request_id: uuid.UUID) -> object:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.resumed_at


@_skip_no_docker
async def test_claim_resume_concurrent_race_yields_exactly_one_winner(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """N concurrent claims on one request resolve to exactly one winner (#2293).

    The real-row-lock proof the SQLite unit suite cannot give: fire eight
    ``claim_resume`` calls at once against a single freshly-parked request;
    exactly one wins the conditional UPDATE and the rest lose after it
    commits. ``resumed_at`` ends up stamped exactly once.
    """
    request = await _commit_run_bound_pending(requester_sub="agent:race")
    assert await _reload_resumed_at(request.id) is None

    outcomes = await asyncio.gather(*(claim_resume(request.id) for _ in range(8)))

    assert sum(1 for won in outcomes if won) == 1, outcomes
    assert await _reload_resumed_at(request.id) is not None


@_skip_no_docker
async def test_resume_dispatch_concurrent_race_executes_exactly_once(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent resumers dispatch the approved op exactly once (#2293).

    Stands in for the in-process agent waiter racing an operator approval
    surface on a run-bound approval: both call
    ``resume_dispatch_after_approval`` on the same committed, approved row.
    The exactly-one-resumer claim admits exactly one to ``dispatch`` — one
    result is ``ok`` (executed), the other ``already_resumed`` (no-op) —
    and the dispatch spy fires exactly once (no double execution of the
    approved write; the lone winner proves the fallback executes rather
    than skipping).
    """
    request = await _commit_run_bound_pending(requester_sub="agent:double")

    reviewer = _make_operator(sub="human:reviewer", kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        await approve_request(session, request.id, operator=reviewer, params=None)
        await session.commit()

    dispatch_calls = 0

    async def _spy_dispatch(**kwargs: object) -> OperationResult:
        nonlocal dispatch_calls
        dispatch_calls += 1
        assert kwargs.get("_approved") is True
        return OperationResult(status="ok", op_id=str(kwargs["op_id"]), duration_ms=0.1)

    import meho_backplane.operations.dispatcher as dispatcher_module

    monkeypatch.setattr(dispatcher_module, "dispatch", _spy_dispatch)

    results = await asyncio.gather(
        resume_dispatch_after_approval(operator=reviewer, request=request),
        resume_dispatch_after_approval(operator=reviewer, request=request),
    )

    statuses = sorted(r.status for r in results)
    assert statuses == ["already_resumed", "ok"], statuses
    assert dispatch_calls == 1, "the approved op must dispatch exactly once under the race"
