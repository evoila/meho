# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres proof that two overlapping settings imports keep the stricter policy.

A settings import locks the rows of the tenants in its file before it reads
their policies, and holds the lock until its commit. A second import of the
same tenant waits, then reads what the first one wrote. So an import that
changes only the flight recorder retention never writes back an announce
gate that another import turned on.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import yaml

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.instance_settings.service import ImportResult, apply_import

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000a5e77")


def _admin() -> Operator:
    return Operator(
        sub="settings-admin",
        raw_jwt="<test>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _file(policies: dict[str, Any]) -> bytes:
    doc = {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {"tenants": [{"id": str(_TENANT), "policies": policies}]},
    }
    return yaml.safe_dump(doc, sort_keys=False).encode()


_RETENTION_ONLY = _file({"flight_recorder": {"retention_days": 30}})
_GATE_ON = _file({"announce_gate_enabled": True})


async def _seed() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT,
                slug="tenant-overlap",
                name="Tenant Overlap",
                announce_gate_enabled=False,
                flight_recorder_enabled=True,
                flight_recorder_retention_days=14,
            )
        )
        await session.commit()


async def _import_and_commit(content: bytes) -> ImportResult:
    async with get_sessionmaker()() as session:
        result = await apply_import(session, _admin(), content)
        await session.commit()
        return result


async def _overlap(first: bytes, second: bytes) -> ImportResult:
    """Run *first* up to its commit, start *second*, then commit *first*.

    Returns the result of *second*. It must wait for the commit of *first*.
    """
    async with get_sessionmaker()() as session:
        first_result = await apply_import(session, _admin(), first)
        assert first_result.applied
        second_task = asyncio.create_task(_import_and_commit(second))
        await asyncio.sleep(0.5)
        assert not second_task.done()
        await session.commit()
    return await asyncio.wait_for(second_task, timeout=10)


async def _tenant() -> Tenant:
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, _TENANT)
    assert tenant is not None
    return tenant


async def test_retention_import_first_then_gate_import_keeps_gate_on(
    pg_engine: None,
) -> None:
    await _seed()

    second = await _overlap(_RETENTION_ONLY, _GATE_ON)

    before = second.plan.before[str(_TENANT)]["policies"]
    assert before["flight_recorder"]["retention_days"] == 30
    assert before["announce_gate_enabled"] is False
    tenant = await _tenant()
    assert tenant.announce_gate_enabled is True
    assert tenant.flight_recorder_retention_days == 30


async def test_gate_import_first_then_retention_import_keeps_gate_on(
    pg_engine: None,
) -> None:
    await _seed()

    second = await _overlap(_GATE_ON, _RETENTION_ONLY)

    assert second.plan.before[str(_TENANT)]["policies"]["announce_gate_enabled"] is True
    tenant = await _tenant()
    assert tenant.announce_gate_enabled is True
    assert tenant.flight_recorder_retention_days == 30
