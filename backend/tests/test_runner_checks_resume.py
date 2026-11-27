# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The resume of a runner check while its runner is stale.

With ``RUNNER_CHECKS_ENABLED`` on, a resumed check of a stale runner reads
``unknown``. When that ``unknown`` reading fails, the resume still answers
with the saved state and its audit values, and the log names the check.
Every read of the check in that step filters by tenant.

The helpers come from :mod:`tests.test_runner_checks_count`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from structlog.testing import capture_logs

import meho_backplane.gateway.runner_checks as runner_checks
from meho_backplane.checks import sensor_pause
from meho_backplane.checks.sensor_pause import set_sensor_status
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow, SensorStatus
from meho_backplane.settings import get_settings

from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import _TENANT, _seed_identities
from .test_runner_checks_count import _OTHER_TENANT, _seed_sensor, _sensor

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _stale_runner(tenant_id: uuid.UUID = _TENANT) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=tenant_id, runner_name="runner-a", items=[], stale_at=datetime.now(UTC)
            )
        )
        await session.commit()


async def test_a_resume_whose_unknown_reading_fails_answers_with_the_saved_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor(status="paused")
    await _stale_runner()

    async def fails(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("write failed")

    monkeypatch.setattr(runner_checks, "mark_sensor_unknown", fails)

    with capture_logs() as logs:
        result = await set_sensor_status(_TENANT, sensor_id, SensorStatus.ACTIVE)

    assert result is not None
    assert result.sensor.status == "active"
    assert result.before == {"status": "paused", "status_reason": None}
    assert result.after == {"status": "active", "status_reason": None}
    assert (await _sensor(sensor_id)).status == "active"
    failed = [e for e in logs if e["event"] == "sensor_resume_stale_mark_failed"]
    assert [(e["sensor_id"], e["log_level"]) for e in failed] == [(str(sensor_id), "warning")]


async def test_every_read_of_the_check_in_the_unknown_step_filters_by_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reads of the check before the ``unknown`` reading is written."""
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _stale_runner()
    reads: list[str] = []
    writing = {"on": False}
    real_mark = runner_checks.mark_sensor_unknown

    async def mark(*args: Any, **kwargs: Any) -> Any:
        writing["on"] = True
        return await real_mark(*args, **kwargs)

    monkeypatch.setattr(runner_checks, "mark_sensor_unknown", mark)

    def keep(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        is_read = statement.lstrip().upper().startswith("SELECT") and "FROM sensor" in statement
        if is_read and not writing["on"]:
            reads.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", keep)
    try:
        await sensor_pause._mark_if_runner_stale(_TENANT, sensor_id, datetime.now(UTC))
    finally:
        event.remove(engine, "before_cursor_execute", keep)

    assert (await _sensor(sensor_id)).last_state == "unknown"
    assert len(reads) == 2
    assert all("sensor.tenant_id" in statement.split("WHERE", 1)[1] for statement in reads)


async def test_a_check_of_another_tenant_is_not_read_by_the_unknown_step() -> None:
    await _seed_identities()
    other = await _seed_sensor(tenant_id=_OTHER_TENANT)
    await _stale_runner()
    await _stale_runner(_OTHER_TENANT)

    await sensor_pause._mark_if_runner_stale(_TENANT, other, datetime.now(UTC))

    assert (await _sensor(other)).last_state == "ok"
