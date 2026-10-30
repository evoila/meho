# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Oldest-first listing and chain steps for approval requests (#3596).

Requests sent under one change ticket (``work_ref``) are often steps of one
chain. These tests pin the service side:

* ``list_pending(order=...)`` lists newest first by default and oldest first
  with ``order="oldest"``.
* ``pending_chain_positions`` numbers the pending requests of each change
  ticket that has two or more of them, oldest first.
* The ``approval.pending`` step event carries ``chain_step`` /
  ``chain_total`` for such a request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_queue import (
    ChainPosition,
    list_pending,
    pending_chain_positions,
    record_approval_step_event,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a596")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000b596")
_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_TICKET = "gh:evoila/meho#1"


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
    async with get_sessionmaker()() as s:
        yield s


def _row(
    *,
    minute: int,
    work_ref: str | None = _TICKET,
    status: str = ApprovalRequestStatus.PENDING.value,
    tenant_id: uuid.UUID = _TENANT,
) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        run_id=None,
        principal_sub="agent-sub",
        op_id=f"step.{minute}",
        connector_id="vault-1.x",
        target_id=None,
        params_hash="0" * 64,
        params={},
        proposed_effect={"op_id": f"step.{minute}", "connector_id": "vault-1.x"},
        status=status,
        work_ref=work_ref,
        created_at=_BASE + timedelta(minutes=minute),
    )


async def _add(session: AsyncSession, *rows: ApprovalRequest) -> list[uuid.UUID]:
    session.add_all(rows)
    await session.commit()
    return [row.id for row in rows]


@pytest.mark.asyncio
async def test_list_pending_orders_newest_first_by_default_and_oldest_on_request(
    session: AsyncSession,
) -> None:
    ids = await _add(session, _row(minute=1), _row(minute=2), _row(minute=3))

    newest = await list_pending(session, tenant_id=_TENANT)
    oldest = await list_pending(session, tenant_id=_TENANT, order="oldest")

    assert [r.id for r in newest] == list(reversed(ids))
    assert [r.id for r in oldest] == ids
    # Paging follows the chosen order.
    page = await list_pending(session, tenant_id=_TENANT, order="oldest", limit=1, offset=1)
    assert [r.id for r in page] == [ids[1]]


@pytest.mark.asyncio
async def test_chain_positions_number_pending_rows_of_each_ticket_oldest_first(
    session: AsyncSession,
) -> None:
    first, second, third = await _add(session, _row(minute=1), _row(minute=2), _row(minute=3))
    (lone,) = await _add(session, _row(minute=4, work_ref="gh:evoila/meho#2"))
    (no_ticket,) = await _add(session, _row(minute=5, work_ref=None))

    positions = await pending_chain_positions(session, tenant_id=_TENANT)

    assert positions[first] == ChainPosition(work_ref=_TICKET, step=1, total=3)
    assert positions[second] == ChainPosition(work_ref=_TICKET, step=2, total=3)
    assert positions[third] == ChainPosition(work_ref=_TICKET, step=3, total=3)
    # A ticket with one pending request and a request with no ticket have
    # no order to show.
    assert lone not in positions
    assert no_ticket not in positions


@pytest.mark.asyncio
async def test_chain_positions_count_only_pending_rows(session: AsyncSession) -> None:
    (approved,) = await _add(session, _row(minute=1, status=ApprovalRequestStatus.APPROVED.value))
    second, third = await _add(session, _row(minute=2), _row(minute=3))

    positions = await pending_chain_positions(session, tenant_id=_TENANT)

    assert approved not in positions
    assert positions[second].step == 1
    assert positions[third] == ChainPosition(work_ref=_TICKET, step=2, total=2)


@pytest.mark.asyncio
async def test_chain_positions_are_tenant_isolated_and_filter_by_ticket(
    session: AsyncSession,
) -> None:
    mine = await _add(session, _row(minute=1), _row(minute=2))
    await _add(
        session,
        _row(minute=0, tenant_id=_OTHER_TENANT),
        _row(minute=3, tenant_id=_OTHER_TENANT),
    )
    await _add(
        session,
        _row(minute=4, work_ref="gh:evoila/meho#2"),
        _row(minute=5, work_ref="gh:evoila/meho#2"),
    )

    positions = await pending_chain_positions(session, tenant_id=_TENANT, work_ref=_TICKET)

    assert set(positions) == set(mine)
    assert [positions[i].step for i in mine] == [1, 2]


@pytest.mark.asyncio
async def test_pending_step_event_carries_the_chain_step(session: AsyncSession) -> None:
    rows = [_row(minute=1), _row(minute=2)]
    await _add(session, *rows)
    lone = _row(minute=3, work_ref="gh:evoila/meho#2")
    await _add(session, lone)

    target = (
        "meho_backplane.operations.addon_step_events."
        "AddonStepEventService.record_if_owned_committed"
    )
    with patch(target, new_callable=AsyncMock) as record:
        await record_approval_step_event(
            tenant_id=_TENANT, request=rows[1], decision="pending", audit_id=uuid.uuid4()
        )
        await record_approval_step_event(
            tenant_id=_TENANT, request=lone, decision="pending", audit_id=uuid.uuid4()
        )
        await record_approval_step_event(
            tenant_id=_TENANT, request=rows[0], decision="approved", audit_id=uuid.uuid4()
        )

    grouped, single, decided = (call.kwargs["payload"] for call in record.await_args_list)
    assert grouped["chain_step"] == 2
    assert grouped["chain_total"] == 2
    assert "chain_step" not in single
    assert "chain_step" not in decided
