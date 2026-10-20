# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vmware-rest-8.0 catalog carries the composites and typed ops (#3789).

A fingerprinted vCenter 8.0.x target resolves to the ``vmware-rest-8.0``
catalog. These tests check that:

* every composite and typed op on the 9.0 catalog is also on the 8.0
  catalog, or is listed in ``COMPOSITES_NOT_ON_8_0`` with a reason;
* ``vm.disk.grow`` runs end to end through the 8.0 catalog for an 8.0.3
  target;
* the catalog boundary error names the catalog to use for the target.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from packaging.version import Version
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import resolve_connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import (
    VmwareRest80Connector,
    VmwareRestConnector,
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites._register import COMPOSITES_NOT_ON_8_0
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write import _DiskGrowConnector, _virtual_disk

_DISK_GROW = "vmware.composite.vm.disk.grow"
_TEN_GIB = 10 * 1024**3
_TWENTY_GIB = 20 * 1024**3


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches and the connector registry around each test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the upsert does not load ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _code_shipped_op_ids(version: str) -> set[str]:
    """op_ids of the typed and composite rows on ``(vmware, version, vmware-rest)``."""
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.product == "vmware",
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == "vmware-rest",
                EndpointDescriptor.source_kind.in_(("typed", "composite")),
            )
        )
        return set(rows.scalars().all())


async def test_every_9_0_op_is_on_8_0_or_excluded_with_a_reason(
    stub_embedding_service: AsyncMock,
) -> None:
    """9.0 to 8.0 parity: reachable on 8.0, or excluded with a documented reason."""
    for registrar in (
        register_vmware_composite_operations,
        register_vmware_composite_operations_8_0,
        register_vmware_typed_operations,
        register_vmware_typed_operations_8_0,
    ):
        await registrar(embedding_service=stub_embedding_service)

    on_9_0 = await _code_shipped_op_ids("9.0")
    on_8_0 = await _code_shipped_op_ids("8.0")

    assert on_9_0
    assert _DISK_GROW in on_8_0
    assert "vmware.object.collect" in on_8_0
    # Every exclusion names a real 9.0 op and says why.
    assert set(COMPOSITES_NOT_ON_8_0) <= on_9_0
    for op_id, reason in COMPOSITES_NOT_ON_8_0.items():
        assert reason.strip(), f"{op_id} is excluded from 8.0 without a reason"
    # Nothing on 9.0 is missing from 8.0 unless it is excluded, and an
    # excluded op is really absent from 8.0.
    assert on_9_0 - on_8_0 == set(COMPOSITES_NOT_ON_8_0)
    assert on_8_0 <= on_9_0


async def test_8_0_rows_keep_the_9_0_policy_and_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """A composite on the 8.0 catalog has the same handler and approval rule."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    await register_vmware_composite_operations_8_0(embedding_service=stub_embedding_service)

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id == _DISK_GROW)
                )
            )
            .scalars()
            .all()
        )
    by_version = {row.version: row for row in rows}
    assert set(by_version) == {"9.0", "8.0"}
    row_9_0, row_8_0 = by_version["9.0"], by_version["8.0"]
    assert row_8_0.source_kind == "composite"
    assert row_8_0.handler_ref == row_9_0.handler_ref
    assert row_8_0.safety_level == row_9_0.safety_level == "dangerous"
    assert row_8_0.requires_approval is row_9_0.requires_approval is True
    assert row_8_0.parameter_schema == row_9_0.parameter_schema


class _FakeFingerprint:
    def __init__(self, version: str) -> None:
        self.version = version


class _FakeVmwareTarget:
    """A fingerprinted vCenter 8.0.3 target, as the dispatcher reads it."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint("8.0.3")
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.tenant_id = uuid.UUID("00000000-0000-0000-0000-0000000037a9")
        self.name = "vcenter-8"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-vmware-8-0-parity",
        name="VMware 8.0 parity",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000037a9"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def test_vm_disk_grow_runs_on_the_8_0_catalog_for_an_8_0_3_target(
    stub_embedding_service: AsyncMock,
) -> None:
    """``vm.disk.grow`` via ``vmware-rest-8.0`` grows the disk of an 8.0.3 target."""
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    register_connector_v2(
        product="vmware", version="8.0", impl_id="vmware-rest", cls=VmwareRest80Connector
    )
    register_connector_v2(product="vmware", version="", impl_id="", cls=VmwareRestConnector)
    await register_vmware_composite_operations_8_0(embedding_service=stub_embedding_service)
    # The approval path is covered elsewhere; here the 8.0 lookup and the
    # run are the contract, so the top-level approval is switched off.
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _DISK_GROW)
            .values(requires_approval=False)
        )
        await session.commit()
    reset_dispatcher_caches()

    target = _FakeVmwareTarget()
    assert resolve_connector(target) is VmwareRest80Connector
    recorder = _DiskGrowConnector(devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)])
    _CONNECTOR_INSTANCE_CACHE[VmwareRest80Connector] = recorder  # type: ignore[assignment]

    result = await dispatch(
        operator=_operator(),
        connector_id="vmware-rest-8.0",
        op_id=_DISK_GROW,
        target=target,
        params={"vm": "vm-1", "disk": 2000, "capacity_bytes": _TWENTY_GIB},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "grown"
    assert result.result["disk"] == "2000"
    assert len(recorder.reconfig_bodies) == 1


@pytest.mark.parametrize(
    ("catalog", "target_version", "hint"),
    [
        (VmwareRestConnector, "8.0.3", "use connector vmware-rest-8.0 for this target"),
        (VmwareRestConnector, "8.0", "use connector vmware-rest-8.0 for this target"),
        (VmwareRest80Connector, "9.1", "use connector vmware-rest-9.0 for this target"),
    ],
)
def test_boundary_error_names_the_catalog_for_the_target(
    catalog: type[VmwareRestConnector], target_version: str, hint: str
) -> None:
    """The refused catalog names the sibling catalog that covers the target."""
    reason = catalog.catalog_target_incompatibility(
        descriptor_source_kind="ingested",
        target_product="vmware",
        target_version=target_version,
        selected_target_connector=None,
    )
    assert reason is not None
    assert reason.startswith("target version is outside the supported ")
    assert reason.endswith(hint)


@pytest.mark.parametrize("target_version", ["10.0", "8.5", "7.0.3"])
def test_boundary_error_names_no_catalog_when_none_covers_the_target(
    target_version: str,
) -> None:
    """No catalog covers the version, so the reason names none."""
    reason = VmwareRestConnector.catalog_target_incompatibility(
        descriptor_source_kind="ingested",
        target_product="vmware",
        target_version=target_version,
        selected_target_connector=None,
    )
    assert reason == "target version is outside the supported 9.x catalog boundary"
    assert VmwareRestConnector._catalog_covers(Version("9.1"))
