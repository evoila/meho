# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test helpers: write a hosted agent run with saved events to the test database."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRunStatus, AgentRunTrigger
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.session_store.hosted_runs import (
    RunEventLog,
    add_run_session,
    end_run_session,
)


async def record_hosted_run(
    *,
    tenant_id: uuid.UUID,
    sub: str,
    events: Sequence[tuple[str, dict[str, Any]]] = (),
    end: AgentRunStatus | None = AgentRunStatus.SUCCEEDED,
    trigger: AgentRunTrigger = AgentRunTrigger.DIRECT,
    agent_name: str = "a1",
) -> uuid.UUID:
    """Create a running run with its session, save *events*, then end it with *end*.

    ``end=None`` leaves the run ``running``.
    """
    async with get_sessionmaker()() as session:
        row = await run_lifecycle.create_run(
            session,
            tenant_id=tenant_id,
            identity_sub=sub,
            trigger=trigger,
            model_tier="cheap",
        )
        await run_lifecycle.start_run(session, row, provider="test", model="test")
        add_run_session(
            session, run_id=row.id, tenant_id=tenant_id, started_by=sub, agent_name=agent_name
        )
        run_id = row.id
        await session.commit()
    log = RunEventLog(run_id, tenant_id)
    for kind, data in events:
        await log.save(kind, data)
    if end is not None:
        await end_hosted_run(run_id, end)
    return run_id


async def end_hosted_run(run_id: uuid.UUID, end: AgentRunStatus) -> None:
    """Move the run to the end status *end* through the lifecycle helpers."""
    async with get_sessionmaker()() as session:
        row = await run_lifecycle.get_run(session, run_id)
        assert row is not None
        if end is AgentRunStatus.SUCCEEDED:
            await run_lifecycle.succeed_run(session, row, output={"ok": True})
        elif end is AgentRunStatus.FAILED:
            await run_lifecycle.fail_run(session, row, error="boom")
        else:
            # A cancel goes through ``cancel_run``, which needs an operator.
            await run_lifecycle.transition(session, row, end)
            await end_run_session(session, run_id=run_id, outcome=end.value)
        await session.commit()
