# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central un-reported-mint security alarm sweeper (#2901, #3193, mechanism 4).

Fail-closed conformance for :mod:`meho_backplane.gateway.unreported_mint`:

* an un-reported minted **remote-write** capability past ``expires_at`` fires the
  security alarm **exactly once** (a second tick is a no-op);
* a **reported** (consumed) write does not fire it;
* a **safe** (read) capability past expiry does not fire it (wrong tier);
* an **unexpired** remote-write does not fire it.

The alarm is a distinct **security** event (``gateway.command.unreported_mint``,
``event_class='security'``), separate from the liveness dead-man flip
(``gateway.runner.stale``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GatewayCommand, Tenant
from meho_backplane.gateway.unreported_mint import (
    GATEWAY_UNREPORTED_MINT_PATH,
    _run_one_tick,
)
from meho_backplane.settings import get_settings

_RUNNER = "runner-uw"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(tenant_id: uuid.UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            slug = f"t-{tenant_id.hex[:8]}"
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
            await session.commit()


async def _seed_command(
    *,
    tenant_id: uuid.UUID,
    command_id: uuid.UUID,
    safety_level: str,
    expires_in_seconds: float,
    consumed: bool,
) -> None:
    """Seed one gateway_command with explicit expiry / tier / consumption state."""
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            GatewayCommand(
                id=command_id,
                tenant_id=tenant_id,
                runner_id=_RUNNER,
                op_id="vmware.vm.tag.set",
                params={"tag": "prod"},
                enqueued_by_sub="op-admin",
                params_hash="ph-1",
                safety_level=safety_level,
                expires_at=now + timedelta(seconds=expires_in_seconds),
                consumed_at=now if consumed else None,
            )
        )
        await session.commit()


async def _alarm_rows(command_id: uuid.UUID | None = None) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.path == GATEWAY_UNREPORTED_MINT_PATH)
                )
            )
            .scalars()
            .all()
        )
    if command_id is None:
        return list(rows)
    return [r for r in rows if r.payload.get("command_id") == str(command_id)]


async def _alarm_at(command_id: uuid.UUID) -> datetime | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await session.scalar(
            select(GatewayCommand.unreported_alarm_at).where(GatewayCommand.id == command_id)
        )


@pytest.mark.asyncio
async def test_unreported_remote_write_past_expiry_alarms_once() -> None:
    """AC: an unreported minted write past expiry fires the security alarm exactly once."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_command(
        tenant_id=tenant,
        command_id=command_id,
        safety_level="caution",
        expires_in_seconds=-600,  # expired 10 minutes ago
        consumed=False,
    )

    await _run_one_tick()

    assert await _alarm_at(command_id) is not None, "the latch must be flipped"
    rows = await _alarm_rows(command_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "INTERNAL"
    assert row.path == GATEWAY_UNREPORTED_MINT_PATH
    assert row.payload["event_class"] == "security"
    assert row.payload["runner"] == _RUNNER
    assert row.payload["op_id"] == "vmware.vm.tag.set"
    assert row.payload["lapse_seconds"] >= 0

    # Idempotent: a second tick flips nothing and writes no extra audit row.
    await _run_one_tick()
    assert len(await _alarm_rows(command_id)) == 1


@pytest.mark.asyncio
async def test_reported_write_does_not_alarm() -> None:
    """AC: a reported (consumed) write past expiry does not fire the alarm."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_command(
        tenant_id=tenant,
        command_id=command_id,
        safety_level="caution",
        expires_in_seconds=-600,
        consumed=True,  # its effect WAS reported
    )

    await _run_one_tick()

    assert await _alarm_at(command_id) is None
    assert await _alarm_rows(command_id) == []


@pytest.mark.asyncio
async def test_safe_capability_past_expiry_does_not_alarm() -> None:
    """A ``safe`` (read) capability past expiry is not a remote-write — no alarm."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_command(
        tenant_id=tenant,
        command_id=command_id,
        safety_level="safe",
        expires_in_seconds=-600,
        consumed=False,
    )

    await _run_one_tick()

    assert await _alarm_at(command_id) is None
    assert await _alarm_rows(command_id) == []


@pytest.mark.asyncio
async def test_unexpired_remote_write_does_not_alarm() -> None:
    """A remote-write still within its expiry window has not gone unreported yet."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_command(
        tenant_id=tenant,
        command_id=command_id,
        safety_level="caution",
        expires_in_seconds=600,  # expires 10 minutes from now
        consumed=False,
    )

    await _run_one_tick()

    assert await _alarm_at(command_id) is None
    assert await _alarm_rows(command_id) == []
