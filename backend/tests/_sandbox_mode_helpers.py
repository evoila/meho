# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for the sandbox-mode tests (#4622).

Sandbox mode is a process flag set once at boot
(:func:`meho_backplane.sandbox_mode.set_sandbox_mode_active`). Tests that
need the mode on call :func:`turn_sandbox_mode_on`, which also pins the
two settings and a host name with a ``sandbox`` part, so a test that also
runs the boot guards sees a consistent picture. The autouse fixture in
``conftest.py`` turns the flag off again after every test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, SandboxDeleteGrant, ServicePrincipalGrant
from meho_backplane.sandbox_mode import set_sandbox_mode_active
from meho_backplane.settings import get_settings

SANDBOX_HOST = "meho.sandbox.test"


def turn_sandbox_mode_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sandbox settings and set the process flag on."""
    monkeypatch.setenv("BACKPLANE_URL", f"https://{SANDBOX_HOST}")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", SANDBOX_HOST)
    get_settings.cache_clear()
    set_sandbox_mode_active(True)


def turn_sandbox_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the sandbox settings and set the process flag off."""
    monkeypatch.delenv("MEHO_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("MEHO_SANDBOX_MODE_CONFIRM", raising=False)
    get_settings.cache_clear()
    set_sandbox_mode_active(False)


def soon(days: int = 7) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def seed_grant_row(
    *,
    model: type[ServicePrincipalGrant] | type[SandboxDeleteGrant],
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None = None,
    target_product: str | None = None,
    target_name_pattern: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> uuid.UUID:
    """Write a grant row straight into one of the two grant tables."""
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            model(
                id=grant_id,
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                target_product=target_product,
                target_name_pattern=target_name_pattern,
                reason="seeded by a sandbox-mode test",
                created_by_sub="op-admin",
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )
        await session.commit()
    return grant_id


async def seed_delete_grant(
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None = None,
    target_product: str | None = None,
    target_name_pattern: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> uuid.UUID:
    """Write a delete grant (expiring in 7 days unless given) into ``sandbox_delete_grant``."""
    return await seed_grant_row(
        model=SandboxDeleteGrant,
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        target_product=target_product,
        target_name_pattern=target_name_pattern,
        expires_at=expires_at or soon(),
        revoked_at=revoked_at,
    )


async def grant_use_rows(grant_id: uuid.UUID | None = None) -> list[AuditLog]:
    """The ``approval.decision`` rows a grant use writes (``decision='auto-approved'``)."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.method == "APPROVAL", AuditLog.path == "approval.decision"
            )
        )
        rows = result.scalars().all()
    return [
        row
        for row in rows
        if row.payload.get("decision") == "auto-approved"
        and (grant_id is None or row.payload.get("grant_id") == str(grant_id))
    ]


async def rows_in(model: Any) -> int:
    from sqlalchemy import func

    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())
