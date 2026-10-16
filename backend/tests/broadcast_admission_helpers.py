# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for tests that publish announcements (#3736).

Publishing now needs a trusted agent identity. Wire-shape tests that are
not about admission patch the admission check with
:func:`patch_agent_admission`, so a plain test operator is admitted as a
registered agent principal. Admission itself is tested for real in
``test_broadcast_agent_admission.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.agent_admission import AgentAdmission
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import AgentAnnouncementEvent
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRun

__all__ = [
    "fake_admission",
    "patch_agent_admission",
    "seed_agent_principal",
    "seed_agent_run",
    "trusted_event",
]

#: The source module is patched too: the shared ``isolated_registry``
#: fixture reloads the MCP tool module, and a reload after this patch
#: re-imports the name from the (patched) source.
_ADMISSION_TARGETS = (
    "meho_backplane.auth.agent_admission.resolve_agent_admission",
    "meho_backplane.mcp.tools.broadcast.resolve_agent_admission",
    "meho_backplane.api.v1.broadcast.resolve_agent_admission",
)


def fake_admission(operator: Operator) -> AgentAdmission:
    """Admit *operator* as a registered agent principal named after its ``sub``."""
    return AgentAdmission(
        source="agent_principal",
        owner_sub=f"agent:{operator.sub}",
        principal_sub=operator.sub,
        actor_sub=None,
        run_id=None,
        work_ref=None,
    )


def patch_agent_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit every caller as an agent principal on the MCP and REST paths."""

    async def _admit(operator: Operator) -> AgentAdmission:
        return fake_admission(operator)

    for target in _ADMISSION_TARGETS:
        monkeypatch.setattr(target, AsyncMock(side_effect=_admit))


def trusted_event(
    *,
    tenant_id: UUID,
    principal_sub: str = "op-test",
    activity: str = "investigating",
    phase: str = "start",
    claim_id: UUID | None = None,
    owner_sub: str | None = None,
    ts: datetime | None = None,
    **fields: Any,
) -> AgentAnnouncementEvent:
    """Build a trusted claim event, the only kind the publisher accepts."""
    return AgentAnnouncementEvent.model_validate(
        {
            "tenant_id": tenant_id,
            "principal_sub": principal_sub,
            "activity": activity,
            "phase": phase,
            "ts": ts or datetime.now(UTC),
            "claim_id": claim_id or uuid4(),
            "owner_sub": owner_sub or f"agent:{principal_sub}",
            "admission": "agent_principal",
            **fields,
        }
    )


async def seed_agent_run(
    tenant_id: UUID,
    *,
    identity_sub: str,
    status: str = "running",
    work_ref: str | None = None,
    identity_act: str | None = None,
) -> UUID:
    """Insert one ``agent_run`` row, as the hosted-agent runtime would."""
    run_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                identity_sub=identity_sub,
                identity_act=identity_act,
                trigger="direct",
                model_tier="standard",
                status=status,
                work_ref=work_ref,
            )
        )
        await session.commit()
    return run_id


async def seed_agent_principal(tenant_id: UUID, client_id: str, *, revoked: bool = False) -> None:
    """Insert one registered ``agent_principal`` row for *client_id*."""
    name = client_id.removeprefix("agent:")
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=client_id,
                keycloak_internal_id=f"kc-{uuid4().hex[:12]}",
                owner_sub="op-admin",
                revoked=revoked,
                created_by_sub="op-admin",
            )
        )
        await session.commit()
