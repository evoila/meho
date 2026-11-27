# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: one sign-in removes the own block or approves, also at the same moment.

A fresh sign-in that removed the person's own block cannot approve a
``destructive`` request, and a sign-in that approved a request cannot
remove the person's own block. Both steps read the person's earlier audit
rows (:func:`meho_backplane.auth.sign_in_use.earlier_sign_in_uses`). The two
steps can run at the same moment, so each could read the audit log before
the other writes. Both take the transaction-level advisory lock of the
sign-in first, so the second step waits for the first to commit and then
sees its audit row.

The person is blocked in tenant A and approves in tenant B: in tenant A
the block itself refuses the approve before the sign-in check. SQLite (the
unit-suite driver) has no advisory lock, so only a real Postgres can show
this. Each test runs the production app with every middleware, through
``DELETE /api/v1/principals/blocked/{sub}`` and
``POST /api/v1/approvals/{request_id}/decide``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth import principal_block, sign_in_use
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.auth.sign_in_use import SignInUse
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, PrincipalBlock
from meho_backplane.main import app
from meho_backplane.operations import approval_queue
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

from .conftest import (
    DOCKER_AVAILABLE,
    SKIP_REASON,
    pg_engine,  # noqa: F401 — pytest-discovered fixture
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON),
]

#: The tenants ``pg_engine`` seeds on entry.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_PERSON = "platform-person-one-sign-in"
_PARAMS: dict[str, Any] = {"name": "web-01"}
_WAIT_SECONDS = 10.0


@pytest.fixture
async def _client(
    pg_engine: None,  # noqa: F811 — the fixture above
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from meho_backplane.api.v1 import approvals as approvals_module

    async def _resume(*, operator: Operator, request: ApprovalRequest, params: Any) -> Any:
        return SimpleNamespace(status="ok", op_id=request.op_id, result=None, error=None)

    monkeypatch.setattr(approvals_module, "_resume_dispatch_after_approval", _resume)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_block_cache()
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(PrincipalBlock))
    key = make_rsa_keypair("kid-own-unblock-sign-in-once")
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            yield client, key
    reset_block_cache()


def _sign_in(key: Any, auth_time: int) -> str:
    """A token of the one sign-in both steps use (same ``sid``, same ``auth_time``)."""
    return mint_token(
        key,
        sub=_PERSON,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        fresh_sign_in=True,
        extra_claims={"platform_admin": True, "sid": "s-one-sign-in", "auth_time": auth_time},
    )


async def _seed_block() -> None:
    """The own block of the person in tenant A, set by a tenant admin."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            PrincipalBlock(
                sub=_PERSON,
                scope=str(_TENANT_A),
                tenant_id=_TENANT_A,
                reason="left the team",
                blocked_by_sub="tenant-admin-a",
                blocked_at=datetime.now(UTC),
            )
        )


async def _park() -> uuid.UUID:
    """A ``destructive`` request that waits for approval in tenant B."""
    requester = Operator(
        sub="op-requester-b",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_B,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="vmware-rest-9.0",
            op_id="vm.create",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": "vm.create", "safety_level": "destructive"},
            preview_hash="preview-hash",
        )
        await session.commit()
        return request.id


async def _blocked() -> bool:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(PrincipalBlock).where(PrincipalBlock.sub == _PERSON))
        return rows.first() is not None


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


async def _someone_waits_for_a_lock() -> bool:
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "AND pid <> pg_backend_pid()"
    )
    async with get_sessionmaker()() as session:
        return (await session.execute(query)).scalar_one() > 0


class _HeldScan:
    """Hold the first step right after its read of the audit rows.

    The first step goes on once the second step has read the audit rows
    too, or once Postgres shows the second step waiting for a lock, or
    after :data:`_WAIT_SECONDS`. So the second step reads the audit log
    before the first step commits, unless a lock makes it wait.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.first_read = asyncio.Event()
        self._second_read = asyncio.Event()
        self.second_waited = False
        self._reads = 0
        scan = sign_in_use.earlier_sign_in_uses

        async def _scan_and_hold(session: AsyncSession, **kwargs: Any) -> list[SignInUse]:
            uses = await scan(session, **kwargs)
            self._reads += 1
            if self._reads == 1:
                self.first_read.set()
                await self._until_the_second_step_reads_or_waits()
            else:
                self._second_read.set()
            return uses

        monkeypatch.setattr(principal_block, "earlier_sign_in_uses", _scan_and_hold)
        monkeypatch.setattr(approval_queue, "earlier_sign_in_uses", _scan_and_hold)

    async def _until_the_second_step_reads_or_waits(self) -> None:
        deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if self._second_read.is_set():
                return
            if await _someone_waits_for_a_lock():
                self.second_waited = True
                return
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("first", ["unblock", "approve"])
async def test_one_sign_in_at_the_same_moment_removes_the_own_block_or_approves(
    _client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """The own unblock in tenant A and a destructive approve in tenant B, one sign-in.

    The step that starts first takes the lock and wins. The other step
    waits, then sees the first step's audit row and is refused with
    ``401 fresh_sign_in_already_used``.
    """
    client, key = _client
    await _seed_block()
    request_id = await _park()
    token = _sign_in(key, int(time.time()))
    held = _HeldScan(monkeypatch)

    def _unblock() -> Any:
        return client.request(
            "DELETE",
            f"/api/v1/principals/blocked/{_PERSON}",
            json={"reason": "the block was a mistake"},
            headers={"Authorization": f"Bearer {token}", "Meho-Tenant": str(_TENANT_A)},
        )

    def _approve() -> Any:
        return client.post(
            f"/api/v1/approvals/{request_id}/decide",
            json={"decision": "approved", "reason": ""},
            headers={"Authorization": f"Bearer {token}", "Meho-Tenant": str(_TENANT_B)},
        )

    steps = {"unblock": _unblock, "approve": _approve}
    second = "approve" if first == "unblock" else "unblock"
    first_task = asyncio.ensure_future(steps[first]())
    await asyncio.wait_for(held.first_read.wait(), _WAIT_SECONDS)
    second_task = asyncio.ensure_future(steps[second]())
    answers = await asyncio.wait_for(asyncio.gather(first_task, second_task), 2 * _WAIT_SECONDS)
    won, refused = answers

    assert won.status_code == 200, won.text
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"].startswith("fresh_sign_in_already_used: ")
    if first == "unblock":
        assert not await _blocked()
        assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    else:
        assert await _blocked()
        assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert held.second_waited, "the second step did not wait for the lock of the sign-in"
