# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: a destructive approve needs a sign-in of its own, also at the same moment.

Inside the approval window one sign-in may approve several requests, but a
request whose operation is ``destructive`` needs a sign-in that approved
nothing else (#3979, #5515). The check reads the person's earlier
``approval.decision`` audit rows. Two approves of two different requests
with the same sign-in can run at the same moment, so each could read the
audit log before the other writes. The check takes a transaction-level
advisory lock on the sign-in key, so the second approve waits for the
first to commit and then sees its audit row.

SQLite (the unit-suite driver) has no advisory lock, so only a real
Postgres can show this. The two approves run on two connections, as in
:mod:`tests.integration.test_approval_idempotent_retry_pg`.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.auth.approval_sign_in import ApprovalAuth
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalSignInReusedError,
    approve_request,
    create_pending_request,
)

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
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
        sub="agent:sign-in-race",
        name="Sign-in Race Agent",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _reviewer() -> Operator:
    return Operator(
        sub="op-sign-in-race",
        name="Sign-in Race Reviewer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _park() -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_requester(),
            connector_id="rectest-1.x",
            op_id="rectest.sign-in-race",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": "rectest.sign-in-race", "safety_level": "destructive"},
            preview_hash="preview-hash",
        )
        await session.commit()
        return request.id


@_skip_no_docker
async def test_two_approves_with_one_sign_in_at_the_same_moment_only_one_wins(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """Two destructive approves with one sign-in: one passes, one is refused.

    Each approve holds its transaction open for a short time after its
    check and audit write, before it commits. Without the lock, the other
    approve would read the audit log in that time, find nothing, and pass
    too. With the lock it waits, then sees the first audit row.
    """
    first, second = await _park(), await _park()
    sign_in = ApprovalAuth(
        channel="rest", method="fresh_sign_in", auth_time=int(time.time()), sid="s-race"
    )

    async def _approve(request_id: uuid.UUID) -> str:
        async with get_sessionmaker()() as session:
            try:
                await approve_request(
                    session, request_id, operator=_reviewer(), approval_auth=sign_in
                )
            except ApprovalSignInReusedError:
                await session.rollback()
                return "refused"
            await asyncio.sleep(0.5)
            await session.commit()
            return "approved"

    outcomes = await asyncio.wait_for(asyncio.gather(_approve(first), _approve(second)), 20)

    assert sorted(outcomes) == ["approved", "refused"]
    async with get_sessionmaker()() as session:
        statuses = []
        for request_id in (first, second):
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            statuses.append(row.status)
    assert sorted(statuses) == sorted(
        [ApprovalRequestStatus.APPROVED.value, ApprovalRequestStatus.PENDING.value]
    )
