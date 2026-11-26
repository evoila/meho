# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: the limits of the count and time of a set-aside notice.

The count column is a 32-bit integer. SQLite (the unit-suite driver) also
stores a larger number, so only a real Postgres shows that MEHO drops such a
count and still keeps the notice of the same post.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from tests.test_gateway_assignment_api import (
    _RUNNER_A_ID,
    _TENANT,
    _build_app,
    _runner_token,
    _seed_identities,
)
from tests.test_runner_checks_count import _KEY, _seed_sensor, _sensor

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON),
]

_STORED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _post(sensor_id: uuid.UUID, *, total: int, last_at: str) -> httpx.Response:
    body: dict[str, Any] = {
        "runner_id": "runner-a",
        "results": [],
        "set_aside": [
            {
                "check_ref": str(sensor_id),
                "result_uid": uuid.uuid4().hex,
                "status": 422,
                "code": "results_refused",
                "produced_at": datetime.now(UTC).isoformat(),
            }
        ],
        "set_aside_total": total,
        "set_aside_last_at": last_at,
    }
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://testserver"
    ) as client:
        with respx.mock(assert_all_mocked=False) as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            return await client.post(
                "/api/v1/checks/results",
                json=body,
                headers={"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"},
            )


async def _seed() -> uuid.UUID:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=_TENANT,
                runner_name="runner-a",
                items=[],
                set_aside_count=5,
                set_aside_last_at=_STORED_AT,
            )
        )
        await session.commit()
    return sensor_id


async def _stored() -> tuple[int, datetime | None]:
    async with get_sessionmaker()() as session:
        row = (
            (
                await session.execute(
                    select(RunnerAssignmentRow).where(RunnerAssignmentRow.tenant_id == _TENANT)
                )
            )
            .scalars()
            .one()
        )
    return row.set_aside_count, row.set_aside_last_at


@pytest.mark.parametrize("total", [2_147_483_648, -1])
async def test_a_count_outside_the_column_leaves_the_stored_count(
    pg_engine: None,  # noqa: F811 (fixture)
    total: int,
) -> None:
    sensor_id = await _seed()

    resp = await _post(sensor_id, total=total, last_at="2026-11-20T10:00:00+00:00")

    assert resp.status_code == 200, resp.text
    assert await _stored() == (5, datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    assert (await _sensor(sensor_id)).last_state == "unknown"


@pytest.mark.parametrize(
    "last_at",
    ["2026-11-20T10:00:00", "0001-01-01T00:00:00+12:00", "9999-12-31T23:59:59-12:00"],
)
async def test_a_time_outside_the_limits_leaves_the_stored_time(
    pg_engine: None,  # noqa: F811 (fixture)
    last_at: str,
) -> None:
    sensor_id = await _seed()

    resp = await _post(sensor_id, total=2_147_483_647, last_at=last_at)

    assert resp.status_code == 200, resp.text
    assert await _stored() == (2_147_483_647, _STORED_AT)
    assert (await _sensor(sensor_id)).last_state == "unknown"
