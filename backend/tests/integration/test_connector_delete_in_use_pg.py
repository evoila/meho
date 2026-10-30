# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector delete and a new sensor cannot both win on PostgreSQL (#5027).

The unit suite runs on SQLite, which has one writer at a time, so it cannot
hold two transactions open at once. These tests do, against the real
``pgvector/pgvector:pg16`` container:

* **writer first** -- a transaction that saves a sensor holds the shared
  connector-usage lock. The connector delete waits for it, then sees the
  committed sensor and is refused, naming the sensor.
* **delete first** -- the delete holds the exclusive lock and has removed
  the rows. A sensor create waits for it, then reads the operation again,
  finds it gone and fails cleanly. No sensor is left for the deleted
  connector.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.repository import create_sensor
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import SensorAdminService, SensorOperationNotFoundError
from meho_backplane.db.connector_usage_lock import (
    lock_connector_usage_exclusive,
    lock_connector_usage_shared,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Sensor, SensorCadenceKind
from meho_backplane.operations.ingest import ConnectorInUseError, ReviewService
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Matches the tenant row the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP = "GET:/api/v1/disks"
_ASSERTION: dict[str, object] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


async def _seed_connector() -> str:
    """Seed one tenant-A connector with a safe, enabled op; return its id.

    ``endpoint_descriptor`` is not truncated between tests, so each test
    uses its own connector name.
    """
    impl_id = f"usecheck{uuid.uuid4().hex[:8]}-rest"
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=TENANT_A_ID,
                product=impl_id.split("-", 1)[0],
                version="1.0",
                impl_id=impl_id,
                op_id=_OP,
                source_kind="ingested",
                method="GET",
                path="/api/v1/disks",
                parameter_schema={"type": "object", "properties": {}},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
            )
        )
        await session.commit()
    return f"{impl_id}-1.0"


def _admin() -> Operator:
    return Operator(
        sub="op-admin",
        name="Admin",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=TENANT_A_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _sensor_count(connector_id: str) -> int:
    async with get_sessionmaker()() as session:
        return int(
            await session.scalar(
                select(func.count(Sensor.id)).where(Sensor.connector_id == connector_id)
            )
            or 0
        )


@_skip_no_docker
async def test_delete_waits_for_a_sensor_writer_then_is_refused(pg_engine: None) -> None:
    connector_id = await _seed_connector()
    session_w = get_sessionmaker()()
    try:
        await lock_connector_usage_shared(session_w)
        await create_sensor(
            session_w,
            tenant_id=TENANT_A_ID,
            name="disk-free",
            connector_id=connector_id,
            op_id=_OP,
            target=None,
            params={},
            assertion=_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=60,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            retry_times=0,
            retry_backoff_seconds=0,
            identity_sub="__sensor__",
            created_by_sub="op-admin",
        )

        delete_task = asyncio.create_task(
            ReviewService(_admin()).delete_connector(connector_id, tenant_id=TENANT_A_ID)
        )
        await asyncio.sleep(0.5)
        assert not delete_task.done(), "the delete did not wait for the sensor writer"

        await session_w.commit()
    finally:
        await session_w.close()

    with pytest.raises(ConnectorInUseError) as caught:
        await asyncio.wait_for(delete_task, timeout=10)
    assert [(u.kind, u.name) for u in caught.value.users] == [("sensor", "disk-free")]

    async with get_sessionmaker()() as session:
        left = await session.scalar(
            select(func.count(EndpointDescriptor.id)).where(
                EndpointDescriptor.op_id == _OP,
                EndpointDescriptor.impl_id == connector_id.rsplit("-", 1)[0],
            )
        )
    assert left == 1


@_skip_no_docker
async def test_sensor_create_waits_for_the_delete_then_fails(pg_engine: None) -> None:
    connector_id = await _seed_connector()
    impl_id = connector_id.rsplit("-", 1)[0]
    session_d = get_sessionmaker()()
    try:
        await lock_connector_usage_exclusive(session_d)
        await session_d.execute(
            delete(EndpointDescriptor).where(EndpointDescriptor.impl_id == impl_id)
        )

        create_task = asyncio.create_task(
            SensorAdminService().create(
                tenant_id=TENANT_A_ID,
                created_by_sub="op-admin",
                payload=SensorCreate.model_validate(
                    {
                        "name": "disk-free",
                        "connector_id": connector_id,
                        "op_id": _OP,
                        "assertion": _ASSERTION,
                        "cadence_kind": "interval",
                        "interval_seconds": 60,
                    }
                ),
            )
        )
        await asyncio.sleep(0.5)
        assert not create_task.done(), "the sensor create did not wait for the delete"

        await session_d.commit()
    finally:
        await session_d.close()

    with pytest.raises(SensorOperationNotFoundError):
        await asyncio.wait_for(create_task, timeout=10)
    assert await _sensor_count(connector_id) == 0
