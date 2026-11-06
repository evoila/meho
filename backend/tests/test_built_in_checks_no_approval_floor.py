# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The built-in network checks need no approval; connector operations do.

Two rules meet here:

* The nine built-in network checks (``net.*``) connect to an address the
  caller gives, but the probe allowlist guards them. Their address fields
  carry no ``x-meho-caller-address`` marker, so they stay ``safe`` with no
  approval, and a sensor can use them.
* A connector operation with a caller-address parameter keeps the marker and
  waits for a person's approval. ``pfsense.diag.ping`` is the example.

A check row stored with the old marker values (``caution`` with approval)
goes back to ``safe`` at the next registration. A restriction an operator
set by hand stays.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import SensorAdminService
from meho_backplane.connectors.net import http_probe, icmp, ntp, ops, ssh_keyscan, tls
from meho_backplane.connectors.pfsense import ops_diag
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER, has_caller_address
from meho_backplane.settings import get_settings
from tests._operation_contract_addresses import (
    BUILT_IN_CHECK_REASON,
    NOT_CALLER_ADDRESSES,
    address_fields,
)

_BUILT_IN_CHECKS = (
    "net.dns_lookup",
    "net.http_probe",
    "net.ntp_check",
    "net.path_mtu",
    "net.ping",
    "net.ssh_keyscan",
    "net.tcp_check",
    "net.tls_inspect",
    "net.trace",
)

_REGISTRARS = (
    (ops, ops.register_net_typed_operations),
    (tls, tls.register_net_tls_inspect_operation),
    (http_probe, http_probe.register_net_http_probe_operations),
    (ntp, ntp.register_net_ntp_check_operation),
    (icmp, icmp.register_net_icmp_operations),
    (ssh_keyscan, ssh_keyscan.register_net_ssh_keyscan_operation),
)

_TENANT = UUID("00000000-0000-0000-0000-00000000c4ec")


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for the database test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _captured_registrations() -> dict[str, dict[str, Any]]:
    """What each net registrar passes to ``register_typed_operation``, by op id."""
    captured: dict[str, dict[str, Any]] = {}

    async def _capture(**kwargs: Any) -> None:
        captured[str(kwargs["op_id"])] = kwargs

    for module, registrar in _REGISTRARS:
        with patch.object(module, "register_typed_operation", AsyncMock(side_effect=_capture)):
            await registrar(embedding_service=None)
    return captured


async def test_the_nine_built_in_checks_are_registered() -> None:
    assert sorted(await _captured_registrations()) == sorted(_BUILT_IN_CHECKS)


@pytest.mark.parametrize("op_id", _BUILT_IN_CHECKS)
async def test_built_in_check_is_safe_without_approval_or_marker(op_id: str) -> None:
    meta = (await _captured_registrations())[op_id]
    assert meta["safety_level"] == "safe"
    assert meta["requires_approval"] is False
    assert not has_caller_address(meta["parameter_schema"])


@pytest.mark.parametrize("op_id", _BUILT_IN_CHECKS)
async def test_built_in_check_address_fields_are_listed_as_allowlist_guarded(op_id: str) -> None:
    """The contract check lists each address field of a check, with the allowlist reason."""
    fields = address_fields((await _captured_registrations())[op_id]["parameter_schema"])
    assert fields, f"{op_id} has no address field"
    for path in fields:
        assert NOT_CALLER_ADDRESSES.get((op_id, path)) == BUILT_IN_CHECK_REASON


def test_connector_operation_with_a_caller_address_keeps_the_marker() -> None:
    """``pfsense.diag.ping`` is a vendor operation, so it still waits for approval."""
    assert has_caller_address(ops_diag._PING_PARAMETER_SCHEMA)


@pytest.mark.usefixtures("_settings_env")
async def test_a_new_sensor_on_a_built_in_check_is_accepted() -> None:
    for _module, registrar in _REGISTRARS:
        await registrar(embedding_service=None)
    async with get_sessionmaker()() as session:
        if (await session.execute(select(Tenant).where(Tenant.id == _TENANT))).first() is None:
            session.add(Tenant(id=_TENANT, slug="checks", name="Tenant checks"))
            await session.commit()

    entry = await SensorAdminService().create(
        tenant_id=_TENANT,
        created_by_sub="op-admin",
        payload=SensorCreate.model_validate(
            {
                "name": "port-443",
                "connector_id": "net-probe-1.x",
                "op_id": "net.tcp_check",
                "params": {"host": "192.0.2.1", "port": 443},
                "assertion": {
                    "select": {"path": "$.connected"},
                    "compare": {"type": "bool", "expect": True},
                },
                "cadence_kind": "interval",
                "interval_seconds": 60,
            }
        ),
    )
    assert entry.op_id == "net.tcp_check"
    assert entry.status.value == "active"


async def _register_all_checks() -> None:
    for _module, registrar in _REGISTRARS:
        await registrar(embedding_service=None)


async def _store_old_values(
    op_id: str, *, safety_level: str = "caution", enabled: bool = True
) -> None:
    """Store *op_id* the way the caller-address marker wrote it: marker, caution, approval."""
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "net", EndpointDescriptor.op_id == op_id
                )
            )
        ).scalar_one()
        schema = copy.deepcopy(row.parameter_schema)
        for path in address_fields(schema):
            schema["properties"][path][CALLER_ADDRESS_MARKER] = True
        row.parameter_schema = schema
        row.safety_level = safety_level
        row.requires_approval = True
        row.is_enabled = enabled
        await session.commit()


async def _stored(op_id: str) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "net", EndpointDescriptor.op_id == op_id
                )
            )
        ).scalar_one()


@pytest.mark.usefixtures("_settings_env")
async def test_old_stored_rows_are_safe_again_after_registration() -> None:
    """Rows stored with the old caution values go back to safe with no approval."""
    await _register_all_checks()
    for op_id in _BUILT_IN_CHECKS:
        await _store_old_values(op_id)
        assert (await _stored(op_id)).safety_level == "caution"

    await _register_all_checks()

    for op_id in _BUILT_IN_CHECKS:
        row = await _stored(op_id)
        assert row.safety_level == "safe", op_id
        assert row.requires_approval is False, op_id
        assert not has_caller_address(row.parameter_schema), op_id


@pytest.mark.usefixtures("_settings_env")
async def test_an_operator_restriction_stays_after_registration() -> None:
    """A stricter level, a disabled row, or other values are kept."""
    await _register_all_checks()
    await _store_old_values("net.tcp_check", safety_level="dangerous")
    await _store_old_values("net.ping", enabled=False)
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "net",
                    EndpointDescriptor.op_id == "net.tls_inspect",
                )
            )
        ).scalar_one()
        row.safety_level = "caution"
        row.requires_approval = True
        await session.commit()

    await _register_all_checks()

    stricter = await _stored("net.tcp_check")
    assert (stricter.safety_level, stricter.requires_approval) == ("dangerous", True)
    disabled = await _stored("net.ping")
    assert (disabled.safety_level, disabled.requires_approval) == ("caution", True)
    assert disabled.is_enabled is False
    by_hand = await _stored("net.tls_inspect")
    assert (by_hand.safety_level, by_hand.requires_approval) == ("caution", True)
