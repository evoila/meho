# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the #2322 approval-TTL periodic sweeper.

Coverage matrix (Task #2322 acceptance criteria):

* **Sweep expires past-deadline rows across tenants** — a sweep with a
  stale pending row in each of two tenants flips both to ``expired`` and
  writes a decision audit row per row, under a per-tenant system operator.
* **Fresh rows are left pending** — a not-yet-expired row survives a sweep.
* **Legacy null-expiry rows age out** — the sweeper passes the configured
  ``APPROVAL_DEFAULT_TTL`` so a pre-#2322 null-``expires_at`` row past
  ``created_at + TTL`` is coalesced and expired.
* **Per-tenant failure isolation** — one tenant's sweep raising does not
  stop the others.
* **start/stop lifecycle** — the lifespan helpers create and cleanly
  cancel the background task.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog, Tenant
from meho_backplane.operations import approval_expiry
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_expiry import (
    start_approval_expiry_sweeper,
    stop_approval_expiry_sweeper,
    sweep_expired_approvals,
)
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="agent-requester",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _seed_tenant(slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _park(
    tenant_id: uuid.UUID,
    op_id: str,
    *,
    expires_at: datetime | None,
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator(tenant_id),
            connector_id="vault-1.x",
            op_id=op_id,
            target=None,
            params={},
            params_hash=compute_params_hash({}),
            expires_at=expires_at,
        )
        await session.commit()
        return request.id


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


@pytest.mark.asyncio
async def test_sweep_expires_past_deadline_across_tenants() -> None:
    tenant_a = await _seed_tenant("ttl-a")
    tenant_b = await _seed_tenant("ttl-b")
    past = datetime.now(UTC) - timedelta(hours=1)

    stale_a = await _park(tenant_a, "vault.kv.stale-a", expires_at=past)
    stale_b = await _park(tenant_b, "vault.kv.stale-b", expires_at=past)

    expired = await sweep_expired_approvals()

    assert expired == 2
    assert await _status(stale_a) == ApprovalRequestStatus.EXPIRED.value
    assert await _status(stale_b) == ApprovalRequestStatus.EXPIRED.value

    # One decision audit row per expired request.
    async with get_sessionmaker()() as session:
        decisions = (
            (
                await session.execute(
                    AuditLog.__table__.select().where(AuditLog.path == "approval.decision")
                )
            )
            .mappings()
            .all()
        )
    assert len(decisions) == 2
    assert all(d["status_code"] == 410 for d in decisions)


@pytest.mark.asyncio
async def test_sweep_leaves_fresh_pending() -> None:
    tenant = await _seed_tenant("ttl-fresh")
    fresh = await _park(tenant, "vault.kv.fresh", expires_at=datetime.now(UTC) + timedelta(hours=1))

    expired = await sweep_expired_approvals()

    assert expired == 0
    assert await _status(fresh) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_sweep_ages_out_legacy_null_expiry() -> None:
    tenant = await _seed_tenant("ttl-legacy")
    row_id = await _park(tenant, "vault.kv.legacy", expires_at=None)

    # Rewrite to the pre-#2322 shape: null deadline, created > default TTL ago.
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, row_id)
        assert row is not None
        row.expires_at = None
        row.created_at = datetime.now(UTC) - timedelta(days=365)
        await session.commit()

    expired = await sweep_expired_approvals()

    assert expired == 1
    async with get_sessionmaker()() as session:
        final = await session.get(ApprovalRequest, row_id)
        assert final is not None
        assert final.status == ApprovalRequestStatus.EXPIRED.value
        # The coalesced deadline was backfilled onto the row.
        assert final.expires_at is not None


@pytest.mark.asyncio
async def test_sweep_isolates_per_tenant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tenant's sweep raising is swallowed; the others still get swept."""
    good = await _seed_tenant("ttl-good")
    bad = await _seed_tenant("ttl-bad")
    past = datetime.now(UTC) - timedelta(hours=1)
    good_row = await _park(good, "vault.kv.good", expires_at=past)
    await _park(bad, "vault.kv.bad", expires_at=past)

    real_expire = approval_expiry.expire_stale_requests

    async def _flaky(session: object, *, operator: Operator, **kwargs: object) -> object:
        if operator.tenant_id == bad:
            raise RuntimeError("boom")
        return await real_expire(session, operator=operator, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(approval_expiry, "expire_stale_requests", _flaky)

    # Does not raise despite the bad tenant blowing up.
    expired = await sweep_expired_approvals()

    assert expired == 1
    assert await _status(good_row) == ApprovalRequestStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_start_stop_lifecycle() -> None:
    task = start_approval_expiry_sweeper()
    assert isinstance(task, asyncio.Task)
    assert not task.done()
    await stop_approval_expiry_sweeper(task)
    assert task.done()
