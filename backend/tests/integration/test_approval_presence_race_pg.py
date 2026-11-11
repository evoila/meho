# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres race for one passkey touch challenge (#6214).

Several approves use one challenge row at the same moment. Each takes the
advisory lock of the row and then marks it used with a conditional
``UPDATE``. Exactly one wins; the others are refused as ``used``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalPresenceChallenge
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_presence import (
    TouchChallengeError,
    start_touch,
    use_touch,
)
from meho_backplane.operations.approval_queue import create_pending_request

from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _operator(sub: str, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


@_skip_no_docker
async def test_racing_uses_of_one_challenge_have_exactly_one_winner(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    approver = _operator("person-approver", PrincipalKind.USER)
    params = {"name": "vm-01"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator("agent-requester", PrincipalKind.AGENT),
            connector_id="race-1.x",
            op_id="race.delete",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        challenge = await start_touch(
            session,
            operator=approver,
            request_ids=[request.id],
            channel="web",
            session_kind="console",
        )
        await session.commit()

    async def _attempt() -> str:
        async with get_sessionmaker()() as session:
            try:
                await use_touch(
                    session, challenge.id, operator=approver, nonce=challenge.binding_hash
                )
            except TouchChallengeError as refused:
                await session.rollback()
                return refused.reason
            await session.commit()
            return "ok"

    outcomes = await asyncio.gather(*(_attempt() for _ in range(8)))

    assert outcomes.count("ok") == 1
    assert outcomes.count("used") == 7
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalPresenceChallenge, challenge.id)
        assert row is not None
        assert row.used_at is not None
