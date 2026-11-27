# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO reads the set-aside notices of one post entry by entry.

With ``RUNNER_CHECKS_ENABLED`` on, an entry of ``set_aside`` that is not a
valid notice (not an object, a field missing, or a field of another type) is
skipped. The other entries are recorded, the post answers 200, and one log
line names the runner and how many entries were skipped.

The helpers come from :mod:`tests.test_runner_set_aside_save`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from structlog.testing import capture_logs

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor
from meho_backplane.runner import wire

from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import _seed_identities
from .test_runner_checks_count import _seed_sensor, _sensor
from .test_runner_set_aside_save import (
    _RUNNER,
    _add_row,
    _notice,
    _post_batch,
    _seed_many_sensors,
    client,  # noqa: F401 (fixture)
    sent,  # noqa: F401 (fixture)
    switch_on,  # noqa: F401 (fixture)
)

pytestmark = pytest.mark.asyncio


async def _unknown_count() -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count()).select_from(Sensor).where(Sensor.last_state == "unknown")
        )
    return int(count or 0)


def _skips(logs: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [
        (e["runner"], e["skipped"])
        for e in logs
        if e["event"] == "runner_set_aside_notices_skipped"
    ]


async def test_999_notices_and_one_empty_object_record_the_999(
    client: TestClient,  # noqa: F811 (fixture)
    switch_on: None,  # noqa: F811 (fixture)
    sent: dict[str, list[Any]],  # noqa: F811 (fixture)
) -> None:
    await _seed_identities()
    await _add_row()
    ids = await _seed_many_sensors(wire.SET_ASIDE_NOTICES_MAX - 1)

    with capture_logs() as logs:
        resp = _post_batch(client, *(_notice(sensor_id) for sensor_id in ids), {})

    assert resp.status_code == 200, resp.text
    assert await _unknown_count() == 999
    assert _skips(logs) == [(_RUNNER, 1)]


async def test_a_notice_whose_status_is_text_is_skipped_and_the_valid_one_is_recorded(
    client: TestClient,  # noqa: F811 (fixture)
    switch_on: None,  # noqa: F811 (fixture)
    sent: dict[str, list[Any]],  # noqa: F811 (fixture)
) -> None:
    await _seed_identities()
    await _add_row()
    valid_id = await _seed_sensor(name="valid")
    text_id = await _seed_sensor(name="text")
    with_text = {**_notice(text_id), "status": "not a number"}
    with_digits = {**_notice(text_id), "status": "422"}

    with capture_logs() as logs:
        resp = _post_batch(client, with_text, _notice(valid_id), with_digits)

    assert resp.status_code == 200, resp.text
    assert (await _sensor(valid_id)).last_state == "unknown"
    assert (await _sensor(text_id)).last_state == "ok"
    assert _skips(logs) == [(_RUNNER, 2)]


@pytest.mark.parametrize("entry", ["text", 7, None, [], {"check_ref": "x"}])
async def test_an_entry_that_is_no_notice_is_skipped_among_valid_notices(
    client: TestClient,  # noqa: F811 (fixture)
    switch_on: None,  # noqa: F811 (fixture)
    sent: dict[str, list[Any]],  # noqa: F811 (fixture)
    entry: Any,
) -> None:
    await _seed_identities()
    await _add_row()
    first = await _seed_sensor(name="first")
    second = await _seed_sensor(name="second")

    with capture_logs() as logs:
        resp = _post_batch(client, _notice(first), entry, _notice(second))

    assert resp.status_code == 200, resp.text
    assert (await _sensor(first)).last_state == "unknown"
    assert (await _sensor(second)).last_state == "unknown"
    assert _skips(logs) == [(_RUNNER, 1)]


async def test_1000_notices_and_one_empty_object_record_all_1000(
    client: TestClient,  # noqa: F811 (fixture)
    switch_on: None,  # noqa: F811 (fixture)
    sent: dict[str, list[Any]],  # noqa: F811 (fixture)
) -> None:
    await _seed_identities()
    await _add_row()
    ids = await _seed_many_sensors(wire.SET_ASIDE_NOTICES_MAX)

    resp = _post_batch(client, *(_notice(sensor_id) for sensor_id in ids), {})

    assert resp.status_code == 200, resp.text
    assert await _unknown_count() == 1000
