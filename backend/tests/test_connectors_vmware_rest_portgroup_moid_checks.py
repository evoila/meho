# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Port group and switch fields take a vCenter moid, not a name (#5073).

``network.portgroup.create`` (``vds``), ``network.portgroup.security.set``
(``portgroup``), ``network.portgroup.vlan.set`` (``portgroup``) and
``host.detach_from_vds`` (``dvs``) use the value as a moid directly. A name
must be refused at preview and at call time with ``invalid_params``, before
an approval request is made and before vCenter is contacted. A real moid
still passes preview and parks the call for approval as before.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000005073")
_TARGET = "test-vcenter"
_AUDIT_OP = "vmware.composite.network.portgroup.audit"

#: (op_id, field, a name that must be refused, a moid that must pass, the other params)
_CASES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    (
        "vmware.composite.network.portgroup.create",
        "vds",
        "lab-switch",
        "dvs-31",
        {"name": "new-pg", "vlan_id": 10},
    ),
    (
        "vmware.composite.network.portgroup.security.set",
        "portgroup",
        "prod-pg",
        "dvportgroup-42",
        {"allow_promiscuous": True},
    ),
    (
        "vmware.composite.network.portgroup.vlan.set",
        "portgroup",
        "prod-pg",
        "dvportgroup-42",
        {"vlan_id": 10},
    ),
    (
        "vmware.composite.host.detach_from_vds",
        "dvs",
        "lab-switch",
        "dvs-31",
        {"host": "host-21", "fallback_network": "network-13"},
    ),
]
_IDS = [case[0].rsplit("composite.", 1)[1] for case in _CASES]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _NoCallConnector:
    """A connector double that fails the test on any call to vCenter."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"vCenter was contacted: {name}")


def _operator() -> Operator:
    return Operator(
        sub="op-portgroup",
        name="Portgroup Moid Checks",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _setup(stub_embedding_service: AsyncMock) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = _NoCallConnector()  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET,
                aliases=[],
                product="vmware",
                host="vcenter.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_portgroup_moid_checks",
            )
        )
        await s.commit()


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _assert_moid_pattern_error(result: dict[str, Any], field: str, name: str) -> None:
    assert result["status"] == "error", result
    extras = result["extras"]
    assert extras["error_code"] == "invalid_params"
    (entry,) = extras["validation_errors"]
    assert (entry["path"], entry["validator"]) == (f"$.{field}", "pattern")
    assert "dvs-" in entry["expected"] or "dvportgroup-" in entry["expected"]
    assert _AUDIT_OP in entry["remediation"]
    assert name not in json.dumps(extras["validation_errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "field", "name", "moid", "other"), _CASES, ids=_IDS)
async def test_name_is_refused_at_preview_and_call(
    stub_embedding_service: AsyncMock,
    op_id: str,
    field: str,
    name: str,
    moid: str,
    other: dict[str, Any],
) -> None:
    await _setup(stub_embedding_service)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET,
        "params": {**other, field: name},
    }

    preview = await preview_operation(_operator(), args)
    _assert_moid_pattern_error(preview, field, name)

    call = await call_operation(_operator(), {**args, "preview_hash": "0" * 64})
    _assert_moid_pattern_error(call, field, name)
    assert await _pending_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "field", "name", "moid", "other"), _CASES, ids=_IDS)
async def test_moid_passes_preview_and_call_parks(
    stub_embedding_service: AsyncMock,
    op_id: str,
    field: str,
    name: str,
    moid: str,
    other: dict[str, Any],
) -> None:
    await _setup(stub_embedding_service)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET,
        "params": {**other, field: moid},
    }

    preview = await preview_operation(_operator(), args)
    assert preview["status"] == "ok", preview

    call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert await _pending_count() == 1


@pytest.mark.parametrize("value", ["dvs-31", "dvs-1001"])
def test_dvs_moid_pattern_accepts_moids(value: str) -> None:
    import re

    from meho_backplane.connectors.vmware_rest.composites.schemas import DVS_MOID_PATTERN

    assert re.fullmatch(DVS_MOID_PATTERN, value)


@pytest.mark.parametrize("value", ["lab-switch", "dvs-", "dvs-abc", "DVS-31", "dvportgroup-42"])
def test_dvs_moid_pattern_rejects_non_moids(value: str) -> None:
    import re

    from meho_backplane.connectors.vmware_rest.composites.schemas import DVS_MOID_PATTERN

    assert re.search(DVS_MOID_PATTERN, value) is None
