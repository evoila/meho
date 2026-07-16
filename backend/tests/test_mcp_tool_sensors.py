# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho.sensor.*`` MCP tools (#2503).

Initiative #2416 (parent goal #221), Task #2503. Covers:

* the three ``meho.sensor.*`` tools are registered and appear in a
  ``tools/list`` for a tenant_admin operator;
* ``meho.sensor.create`` over a safe op dispatches to the service;
* ``meho.sensor.create`` over a non-safe op surfaces the
  ``sensor_requires_safe_operation`` code as an invalid-params error;
* ``meho.sensor.delete`` against a cross-tenant id surfaces as
  ``sensor_not_found``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.service import SensorAdminService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.mcp.registry import all_tools_for, get_tool

# Importing the module runs its side-effect register_mcp_tool calls.
from meho_backplane.mcp.tools import sensors as _sensor_tools
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SAFE_CONNECTOR = "vmware-rest-9.0"
_SAFE_OP = "vmware.vm.list"
_DANGEROUS_OP = "vmware.vm.delete"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _admin(tenant_id: uuid.UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="mcp-admin",
        raw_jwt="dummy",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_descriptor(*, op_id: str, safety_level: str = "safe") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=op_id,
                source_kind="ingested",
                method="GET",
                path=f"/{op_id}",
                parameter_schema={"type": "object", "properties": {}},
                safety_level=safety_level,
            )
        )
        await session.commit()


def test_mcp_tools_registered_and_in_tools_list() -> None:
    """The three meho.sensor.* tools are registered and appear in tools/list."""
    assert _sensor_tools is not None
    for name in ("meho.sensor.list", "meho.sensor.create", "meho.sensor.delete"):
        assert get_tool(name) is not None

    listed = {t.name for t in all_tools_for(_admin())}
    assert {"meho.sensor.list", "meho.sensor.create", "meho.sensor.delete"} <= listed


@pytest.mark.asyncio
async def test_mcp_create_dispatches_to_service() -> None:
    """meho.sensor.create over a safe op returns the created sensor_id."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_descriptor(op_id=_SAFE_OP, safety_level="safe")
    result = await _sensor_tools._create_handler(
        _admin(),
        {
            "name": "disk-space",
            "connector_id": _SAFE_CONNECTOR,
            "op_id": _SAFE_OP,
            "assertion": _ASSERTION,
            "cadence_kind": "interval",
            "interval_seconds": 60,
        },
    )
    assert "sensor_id" in result
    assert result["sensor"]["cadence_kind"] == "interval"
    assert result["sensor"]["status"] == "active"


@pytest.mark.asyncio
async def test_mcp_create_over_non_safe_op_surfaces_code() -> None:
    """A non-safe op surfaces 'sensor_requires_safe_operation' as invalid-params."""
    from meho_backplane.mcp.server import McpInvalidParamsError

    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_descriptor(op_id=_DANGEROUS_OP, safety_level="dangerous")
    with pytest.raises(McpInvalidParamsError, match="sensor_requires_safe_operation"):
        await _sensor_tools._create_handler(
            _admin(),
            {
                "name": "risky",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _DANGEROUS_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
        )


@pytest.mark.asyncio
async def test_mcp_delete_cross_tenant_not_found() -> None:
    """meho.sensor.delete against a cross-tenant id surfaces as sensor_not_found."""
    from meho_backplane.mcp.server import McpInvalidParamsError

    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    await _seed_descriptor(op_id=_SAFE_OP, safety_level="safe")
    service = SensorAdminService()
    from meho_backplane.checks.schemas import SensorCreate

    entry_a = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="a-admin",
        payload=SensorCreate.model_validate(
            {
                "name": "a-sensor",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            }
        ),
    )
    with pytest.raises(McpInvalidParamsError, match="sensor_not_found"):
        await _sensor_tools._delete_handler(_admin(_TENANT_B), {"sensor_id": str(entry_a.id)})
