# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho_sensor_results`` MCP tool (#2756).

Initiative #2780 (parent goal #221), Task #2756. Covers:

* the tool is registered and appears in a ``tools/list`` for an operator;
* the handler returns the ``{items, next_cursor}`` envelope for the caller's
  tenant, oldest-first;
* a cross-tenant sensor id surfaces as ``sensor_not_found``;
* an unknown filter param is rejected (schema refusal -- "no aggregation
  knobs"), as is a malformed cursor.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.repository import record_sensor_result
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import SensorAdminService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.mcp.registry import all_tools_for, get_tool
from meho_backplane.mcp.server import McpInvalidParamsError

# Importing the module runs its side-effect register_mcp_tool calls.
from meho_backplane.mcp.tools import sensors as _sensor_tools
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SAFE_CONNECTOR = "vmware-rest-9.0"
_SAFE_OP = "vmware.vm.list"
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


def _operator(tenant_id: uuid.UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="mcp-op",
        raw_jwt="dummy",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _seed_descriptor() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=_SAFE_OP,
                source_kind="ingested",
                method="GET",
                path=f"/{_SAFE_OP}",
                parameter_schema={"type": "object", "properties": {}},
                safety_level="safe",
            )
        )
        await session.commit()


async def _seed_sensor_with_results(
    tenant_id: uuid.UUID = _TENANT_A, *, count: int = 3
) -> uuid.UUID:
    await _seed_tenant(tenant_id, slug=str(tenant_id)[:8])
    await _seed_descriptor()
    created = await SensorAdminService().create(
        tenant_id=tenant_id,
        created_by_sub="op-admin",
        payload=SensorCreate.model_validate(
            {
                "name": "disk-space",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            }
        ),
    )
    base = datetime(2026, 8, 1, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for i in range(count):
            await record_sensor_result(
                session,
                sensor_id=created.id,
                state="ok",
                value=i,
                evidence={"observed": i},
                evaluated_at=base + timedelta(minutes=i),
                record_history=True,
            )
        await session.commit()
    return created.id


def test_results_tool_registered() -> None:
    """meho_sensor_results is registered and appears in tools/list."""
    assert _sensor_tools is not None
    assert get_tool("meho_sensor_results") is not None
    listed = {t.name for t in all_tools_for(_operator())}
    assert "meho_sensor_results" in listed


@pytest.mark.asyncio
async def test_results_handler_returns_items() -> None:
    """The handler returns the {items, next_cursor} envelope, oldest-first."""
    sensor_id = await _seed_sensor_with_results(count=3)
    result = await _sensor_tools._results_handler(_operator(), {"sensor_id": str(sensor_id)})
    assert set(result.keys()) == {"items", "next_cursor"}
    assert [item["value"] for item in result["items"]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_results_handler_cross_tenant_not_found() -> None:
    """A caller in another tenant cannot read the sensor's history."""
    sensor_id = await _seed_sensor_with_results(_TENANT_A, count=1)
    await _seed_tenant(_TENANT_B, "tenant-b")
    with pytest.raises(McpInvalidParamsError) as exc:
        await _sensor_tools._results_handler(_operator(_TENANT_B), {"sensor_id": str(sensor_id)})
    assert "sensor_not_found" in str(exc.value)


@pytest.mark.asyncio
async def test_results_handler_rejects_unknown_param() -> None:
    """An unknown filter param is a schema refusal (no aggregation knobs)."""
    sensor_id = await _seed_sensor_with_results(count=1)
    with pytest.raises(McpInvalidParamsError):
        await _sensor_tools._results_handler(
            _operator(), {"sensor_id": str(sensor_id), "smoothing": "ewma"}
        )


@pytest.mark.asyncio
async def test_results_handler_rejects_invalid_cursor() -> None:
    """A malformed cursor surfaces as an invalid-params error."""
    sensor_id = await _seed_sensor_with_results(count=1)
    with pytest.raises(McpInvalidParamsError):
        await _sensor_tools._results_handler(
            _operator(), {"sensor_id": str(sensor_id), "cursor": "!!bad!!"}
        )
