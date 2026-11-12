# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: two overlapping retries of a failed step keep both attempts.

A retry reads the step's ``attempts`` list and writes it back with one more
entry. When two retries of the same step write at the same time, each could
read the same list, and the last write would drop the other attempt. The
write phase of :meth:`RunbookRunService.next_step` locks the run row
(``SELECT ... FOR UPDATE``), so the second write waits and then reads the
list the first one wrote.

SQLite (the unit-suite driver) turns ``FOR UPDATE`` into a no-op, so the
unit suite only checks that the lock is asked for
(``tests/test_runbooks_run_service.py``). This suite runs the two retries
on two Postgres connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.runbooks.run_service as run_service_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, RunbookRunStepState
from meho_backplane.runbooks.run_service import PreviousStepFailedError, RunbookRunService
from meho_backplane.runbooks.runs_schemas import (
    ConfirmVerifyResponse,
    CurrentStepResponse,
    NextStepRequest,
    StartRunRequest,
)
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DraftTemplateRequest,
    ManualStep,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import RunbookTemplateService

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OPERATOR = "operator-retry-race"


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _confirm(answer: str, *, retry: bool = False) -> NextStepRequest:
    return NextStepRequest(
        last_verified=True,
        verify_response=ConfirmVerifyResponse(type="confirm", answer=answer),  # type: ignore[arg-type]
        retry=retry,
    )


async def _start_failed_confirm_run() -> uuid.UUID:
    """Publish a one-step confirm template, start a run, and fail its step."""
    slug = f"retry-race-{uuid.uuid4().hex[:8]}"
    body = RunbookTemplateBody(
        title="Retry race",
        description="for the overlapping retry test",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="step-1",
                title="Step 1",
                body="do the thing",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="done?"),
            )
        ],
    )
    templates = RunbookTemplateService()
    await templates.create_draft(_TENANT, _OPERATOR, DraftTemplateRequest(slug=slug, body=body))
    await templates.publish(_TENANT, PublishTemplateRequest(slug=slug, version=1))
    runs = RunbookRunService()
    start = await runs.start_run(
        _TENANT, _OPERATOR, StartRunRequest(template_slug=slug, target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    with pytest.raises(PreviousStepFailedError):
        await runs.next_step(_TENANT, _OPERATOR, start.run_id, _confirm("no"))
    return start.run_id


@_skip_no_docker
async def test_two_overlapping_retries_keep_both_attempts(
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both retries fail again; the step ends with all three attempts.

    Both retries finish their first read before either writes. In the write
    phase, each one waits up to one second for the other after it read the
    step states. Without the run row lock, both would read the same list
    there and the last commit would drop one attempt. With the lock, the
    second one cannot read until the first one commits, so the first one
    stops waiting after the second and writes alone.
    """
    run_id = await _start_failed_confirm_run()

    both_read = asyncio.Event()
    read_counter = {"dispatched": 0, "write_reads": 0}
    real_load_step_states = run_service_module._load_step_states
    real_resolve = RunbookRunService._resolve_verify_response

    async def _resolve_then_wait(self: RunbookRunService, **kwargs: Any) -> Any:
        read_counter["dispatched"] += 1
        if read_counter["dispatched"] == 2:
            both_read.set()
        await both_read.wait()
        return await real_resolve(self, **kwargs)

    both_in_write = asyncio.Event()

    async def _load_then_wait(*args: Any, **kwargs: Any) -> Any:
        rows = await real_load_step_states(*args, **kwargs)
        if both_read.is_set():
            read_counter["write_reads"] += 1
            if read_counter["write_reads"] == 2:
                both_in_write.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_in_write.wait(), timeout=1.0)
        return rows

    monkeypatch.setattr(RunbookRunService, "_resolve_verify_response", _resolve_then_wait)
    monkeypatch.setattr(run_service_module, "_load_step_states", _load_then_wait)

    runs = RunbookRunService()
    results = await asyncio.gather(
        runs.next_step(_TENANT, _OPERATOR, run_id, _confirm("no", retry=True)),
        runs.next_step(_TENANT, _OPERATOR, run_id, _confirm("no", retry=True)),
        return_exceptions=True,
    )

    assert all(isinstance(r, PreviousStepFailedError) for r in results), results
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(RunbookRunStepState).where(
                    RunbookRunStepState.run_id == run_id,
                    RunbookRunStepState.step_id == "step-1",
                )
            )
        ).scalar_one()
        assert row.state == "failed"
        assert [a["passed"] for a in row.attempts] == [False, False, False]
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.run_id == run_id, AuditLog.path == "runbook.confirm"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 3
