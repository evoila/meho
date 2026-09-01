# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the 29 vmware-rest write composites.

Coverage matrix (G3.1-T6 / #509 acceptance criteria, plus single-VM
``vm.power`` / #2301, the vim writes ``vm.disk.grow`` / #2893 +
``cluster.drs_rule.create`` / ``folder.create`` / #2895, the #2891
hardware writes ``vm.resize`` / ``vm.nic.repoint`` / ``vm.device.cdrom``,
and the GOSC composites ``guest.customization_spec.create`` /
``vm.customize`` / #2892):

* All 28 expected write ``op_id`` rows land in ``endpoint_descriptor``
  with ``source_kind="composite"``, ``requires_approval=True``, and
  ``safety_level="dangerous"`` (T4's defaults intentionally inherited) —
  except the destructive-tier ``vm.destroy`` / #3198, which is
  ``safety_level="destructive"``.
* Each row's ``handler_ref`` resolves to the module-level dotted path
  in ``composites/_write``.
* Each row's ``group_key`` resolves to ``vm`` / ``host`` / ``cluster`` /
  ``guest`` / ``networking`` per the canary's stub-LLM taxonomy.
* Combined with the 9 read composites (#508's 5 + the 4 guest-ops
  reads / #3100), the registrar produces **37 rows** total. (The former
  host.network_uplinks / host.vsan_health reads were re-shipped as typed
  ops in #2258.)
* Per-composite ``parameter_schema`` + ``response_schema`` persist
  with the documented required keys.
* Module-level handler shape (no closures / partials / lambdas).
* Idempotent re-registration is a no-op on the embedding pipeline
  (body-hash skip path).

Mirrors :mod:`tests.test_connectors_vmware_rest_composites_register`
for the per-connector contract on the write side. Substrate-level
coverage (composite recursion guard) lives in
:mod:`tests.test_operations_composite`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest.composites import (
    cluster_patch_composite,
    guest_customization_spec_create_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    register_vmware_composite_operations,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_customize_composite,
    vm_deploy_from_library_composite,
    vm_device_cdrom_composite,
    vm_disk_attach_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
    vm_nic_repoint_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_resize_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

# 29 write composites (T6 / #509, single-VM vm.power / #2301, the
# mutating VI-JSON vm.disk.grow / #2893 + WSFC/FCI vm.disk.attach / #3256,
# the folder-template
# vm.clone_from_template / #2894, the vim cluster/inventory writes
# cluster.drs_rule.create + folder.create / #2895, the #2891
# hardware writes vm.resize / vm.nic.repoint / vm.device.cdrom, the
# GOSC composites guest.customization_spec.create + vm.customize / #2892,
# the destructive-tier vm.destroy / #3198, the vim distributed-portgroup
# writes network.portgroup.create + network.portgroup.security.set / #3091,
# the content-library import vm.import_from_library / #3229, and the
# guest-ops writes vm.guest.file.write / #3100 + vm.guest.program.run / #3255).
_WRITE_OP_IDS: tuple[str, ...] = (
    "vmware.composite.vm.create",
    "vmware.composite.vm.clone",
    "vmware.composite.vm.deploy_from_library",
    "vmware.composite.vm.import_from_library",
    "vmware.composite.vm.clone_from_template",
    "vmware.composite.vm.snapshot.revert",
    "vmware.composite.vm.destroy",
    "vmware.composite.vm.migrate",
    "vmware.composite.vm.power",
    "vmware.composite.vm.power.bulk",
    "vmware.composite.vm.disk.grow",
    "vmware.composite.vm.disk.attach",
    "vmware.composite.vm.resize",
    "vmware.composite.vm.nic.repoint",
    "vmware.composite.vm.device.cdrom",
    "vmware.composite.host.evacuate",
    "vmware.composite.host.detach_from_vds",
    "vmware.composite.network.portgroup.create",
    "vmware.composite.network.portgroup.security.set",
    "vmware.composite.cluster.patch",
    "vmware.composite.cluster.drs_rule.create",
    "vmware.composite.folder.create",
    "vmware.composite.guest.customization_spec.create",
    "vmware.composite.vm.customize",
    "vmware.composite.host.datastore_mount_nfs",
    "vmware.composite.host.disk_mark_flash",
    "vmware.composite.host.service_control",
    # Guest-ops channel writes (#3100 / #3255).
    "vmware.composite.vm.guest.file.write",
    "vmware.composite.vm.guest.program.run",
)

# 5 reads (T5 / #508) -- carried over so the combined-count assertion
# does not have to import _read constants. (The former
# host.network_uplinks / host.vsan_health reads were re-shipped as typed
# ops in #2258.)
_READ_OP_IDS: tuple[str, ...] = (
    "vmware.composite.cluster.drs_recommendations",
    "vmware.composite.event.tail",
    "vmware.composite.performance.summary",
    "vmware.composite.datastore.usage",
    "vmware.composite.network.portgroup.audit",
    # Guest-ops channel reads (#3100).
    "vmware.composite.vm.guest.process.list",
    "vmware.composite.vm.guest.env.read",
    "vmware.composite.vm.guest.net.show",
    "vmware.composite.vm.guest.file.read",
)

# 38 total -- 9 read (T5 / #508 + 4 guest-ops reads / #3100) + 29 write
# (T6 / #509 + vm.power / #2301 + vm.disk.grow / #2893 +
# vm.clone_from_template / #2894 + vim cluster/inventory writes
# cluster.drs_rule.create + folder.create / #2895 + #2891 hardware
# writes vm.resize / vm.nic.repoint / vm.device.cdrom + GOSC create/apply / #2892
# + the destructive-tier vm.destroy / #3198 + the #3091 vim portgroup writes
# network.portgroup.create + network.portgroup.security.set + the
# content-library import vm.import_from_library / #3229 + the guest-ops
# program-exec write vm.guest.program.run / #3255 + the WSFC/FCI shared-attach
# vm.disk.attach / #3256).
_ALL_OP_IDS: tuple[str, ...] = _READ_OP_IDS + _WRITE_OP_IDS


_EXPECTED_HANDLER_REF_BY_OP: dict[str, str] = {
    "vmware.composite.vm.create": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_create_composite"
    ),
    "vmware.composite.vm.clone": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_clone_composite"
    ),
    "vmware.composite.vm.deploy_from_library": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_deploy_from_library_composite"
    ),
    "vmware.composite.vm.import_from_library": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_import_from_library_composite"
    ),
    "vmware.composite.vm.clone_from_template": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_clone_from_template_composite"
    ),
    "vmware.composite.vm.snapshot.revert": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_snapshot_revert_composite"
    ),
    "vmware.composite.vm.destroy": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_destroy_composite"
    ),
    "vmware.composite.vm.migrate": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_migrate_composite"
    ),
    "vmware.composite.vm.power": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_power_composite"
    ),
    "vmware.composite.vm.power.bulk": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_power_bulk_composite"
    ),
    "vmware.composite.vm.disk.grow": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_disk_grow_composite"
    ),
    "vmware.composite.vm.disk.attach": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_disk_attach_composite"
    ),
    "vmware.composite.host.evacuate": (
        "meho_backplane.connectors.vmware_rest.composites._write.host_evacuate_composite"
    ),
    "vmware.composite.host.detach_from_vds": (
        "meho_backplane.connectors.vmware_rest.composites._write.host_detach_from_vds_composite"
    ),
    "vmware.composite.network.portgroup.create": (
        "meho_backplane.connectors.vmware_rest.composites._write.network_portgroup_create_composite"
    ),
    "vmware.composite.network.portgroup.security.set": (
        "meho_backplane.connectors.vmware_rest.composites._write."
        "network_portgroup_security_set_composite"
    ),
    "vmware.composite.cluster.patch": (
        "meho_backplane.connectors.vmware_rest.composites._write.cluster_patch_composite"
    ),
    "vmware.composite.cluster.drs_rule.create": (
        "meho_backplane.connectors.vmware_rest.composites._write.cluster_drs_rule_create_composite"
    ),
    "vmware.composite.folder.create": (
        "meho_backplane.connectors.vmware_rest.composites._write.folder_create_composite"
    ),
    "vmware.composite.vm.resize": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_resize_composite"
    ),
    "vmware.composite.vm.nic.repoint": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_nic_repoint_composite"
    ),
    "vmware.composite.vm.device.cdrom": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_device_cdrom_composite"
    ),
    "vmware.composite.guest.customization_spec.create": (
        "meho_backplane.connectors.vmware_rest.composites._write."
        "guest_customization_spec_create_composite"
    ),
    "vmware.composite.vm.customize": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_customize_composite"
    ),
    "vmware.composite.host.datastore_mount_nfs": (
        "meho_backplane.connectors.vmware_rest.composites._host.datastore_mount_nfs_composite"
    ),
    "vmware.composite.host.disk_mark_flash": (
        "meho_backplane.connectors.vmware_rest.composites._host.disk_mark_flash_composite"
    ),
    "vmware.composite.host.service_control": (
        "meho_backplane.connectors.vmware_rest.composites._host.service_control_composite"
    ),
    "vmware.composite.vm.guest.file.write": (
        "meho_backplane.connectors.vmware_rest.composites._guest.guest_file_write_composite"
    ),
    "vmware.composite.vm.guest.program.run": (
        "meho_backplane.connectors.vmware_rest.composites._guest.guest_program_run_composite"
    ),
}


_EXPECTED_GROUP_KEY_BY_OP: dict[str, str] = {
    "vmware.composite.vm.create": "vm",
    "vmware.composite.vm.clone": "vm",
    "vmware.composite.vm.deploy_from_library": "vm",
    "vmware.composite.vm.import_from_library": "vm",
    "vmware.composite.vm.clone_from_template": "vm",
    "vmware.composite.vm.snapshot.revert": "vm",
    "vmware.composite.vm.destroy": "vm",
    "vmware.composite.vm.migrate": "vm",
    "vmware.composite.vm.power": "vm",
    "vmware.composite.vm.power.bulk": "vm",
    "vmware.composite.vm.disk.grow": "vm",
    "vmware.composite.vm.disk.attach": "vm",
    "vmware.composite.vm.resize": "vm",
    "vmware.composite.vm.nic.repoint": "vm",
    "vmware.composite.vm.device.cdrom": "vm",
    "vmware.composite.host.evacuate": "host",
    "vmware.composite.host.detach_from_vds": "host",
    "vmware.composite.network.portgroup.create": "networking",
    "vmware.composite.network.portgroup.security.set": "networking",
    "vmware.composite.cluster.patch": "cluster",
    "vmware.composite.cluster.drs_rule.create": "cluster",
    "vmware.composite.folder.create": "vm",
    "vmware.composite.guest.customization_spec.create": "guest",
    "vmware.composite.vm.customize": "guest",
    "vmware.composite.host.datastore_mount_nfs": "host",
    "vmware.composite.host.disk_mark_flash": "host",
    "vmware.composite.host.service_control": "host",
    "vmware.composite.vm.guest.file.write": "guest_ops",
    "vmware.composite.vm.guest.program.run": "guest_ops",
}


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
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the upsert doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# 29 write composites land alongside the 9 reads (38 total)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_inserts_all_write_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar lands all 29 write op_ids in ``endpoint_descriptor``."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)


@pytest.mark.asyncio
async def test_full_registration_produces_thirty_three_composite_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """9 reads (#508 + guest-ops reads #3100) + 29 writes (#509 + vm.power #2301 +
    vm.disk.grow #2893 + vm.disk.attach #3256 + vm.clone_from_template #2894 +
    cluster.drs_rule.create + folder.create #2895 + #2891 hardware writes
    vm.resize / vm.nic.repoint / vm.device.cdrom + GOSC create/apply #2892 + OVF
    deploy #2909 + host-domain writes #3182 + guest-ops writes
    vm.guest.file.write #3100 + vm.guest.program.run #3255 + destructive-tier
    vm.destroy #3198 + #3091 portgroup writes network.portgroup.create /
    network.portgroup.security.set + content-library import
    vm.import_from_library #3229) = 38 rows. DoD bar."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_ALL_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_ALL_OP_IDS)
    assert len(rows) == 38


@pytest.mark.asyncio
async def test_every_write_composite_row_uses_dangerous_requires_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each write row carries T4's defaults: dangerous + requires_approval=True.

    Load-bearing: write composites should pop the approval queue on
    every dispatch. A misconfigured read-override would silently
    permit unauthenticated mutation; pinning the policy here means CI
    catches a regression before lifespan-startup.

    The one exception is the destructive-tier ``vm.destroy`` (#3198): it is
    ``safety_level="destructive"`` (a strictly harder tier than
    ``dangerous``), still ``requires_approval=True``.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    # Prove the query actually returned all 29 write rows before iterating —
    # otherwise the loop is vacuous when the set is empty / partial.
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)
    for row in rows:
        expected_level = (
            "destructive" if row.op_id == "vmware.composite.vm.destroy" else "dangerous"
        )
        assert row.safety_level == expected_level, (
            f"{row.op_id}: expected {expected_level}, got {row.safety_level!r}"
        )
        assert row.requires_approval is True, (
            f"{row.op_id}: expected requires_approval=True, got {row.requires_approval!r}"
        )


@pytest.mark.asyncio
async def test_every_write_composite_row_carries_composite_source_kind(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row has ``source_kind="composite"`` and the expected enabled defaults."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)
    for row in rows:
        assert row.source_kind == "composite"
        assert row.tenant_id is None
        assert row.is_enabled is True
        assert row.method is None
        assert row.path is None


@pytest.mark.asyncio
async def test_write_handler_ref_round_trips_to_module_level_dotted_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's ``handler_ref`` is the canonical module-level dotted path."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)
    by_op = {row.op_id: row for row in rows}
    for op_id, expected_ref in _EXPECTED_HANDLER_REF_BY_OP.items():
        assert by_op[op_id].handler_ref == expected_ref


@pytest.mark.asyncio
async def test_write_composites_land_in_vm_host_cluster_groups(
    stub_embedding_service: AsyncMock,
) -> None:
    """Group distribution: 12 in ``vm`` (11 ``vm.*`` + ``folder.create``),
    2 ``host.*`` in ``host``, 2 ``cluster.*`` in ``cluster``, 2 GOSC in ``guest``,
    2 portgroup writes in ``networking``.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        descriptor_rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
        group_rows = (await fresh.execute(select(OperationGroup))).scalars().all()
    groups_by_id = {g.id: g for g in group_rows}
    for desc in descriptor_rows:
        assert desc.group_id is not None, f"{desc.op_id} has no group_id"
        group = groups_by_id[desc.group_id]
        expected_key = _EXPECTED_GROUP_KEY_BY_OP[desc.op_id]
        assert group.group_key == expected_key, (
            f"{desc.op_id}: expected {expected_key!r}, got {group.group_key!r}"
        )
        assert group.product == "vmware"
        assert group.version == "9.0"
        assert group.impl_id == "vmware-rest"


@pytest.mark.asyncio
async def test_write_composite_parameter_schemas_persist_with_required_fields(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's parameter_schema has the documented required keys + additionalProperties=False."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows}
    # vm.create requires name / guest_os plus one of folder_name / folder
    # (the #3115 at-least-one-of anyOf shape).
    create_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.create"].parameter_schema)
    assert set(create_schema["required"]) == {"name", "guest_os"}
    assert create_schema["anyOf"] == [
        {"required": ["folder_name"]},
        {"required": ["folder"]},
    ]
    # vm.clone requires source_vm / target_name / library_item.
    clone_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.clone"].parameter_schema)
    assert set(clone_schema["required"]) == {"source_vm", "target_name", "library_item"}
    # vm.snapshot.revert requires vm + snapshot_name.
    revert_schema: dict[str, Any] = dict(
        by_op["vmware.composite.vm.snapshot.revert"].parameter_schema
    )
    assert set(revert_schema["required"]) == {"vm", "snapshot_name"}
    # vm.migrate requires vm + cluster (target_host is optional).
    migrate_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.migrate"].parameter_schema)
    assert set(migrate_schema["required"]) == {"vm", "cluster"}
    # vm.power (single VM) requires vm + verb.
    power_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.power"].parameter_schema)
    assert set(power_schema["required"]) == {"vm", "verb"}
    # vm.power.bulk requires action.
    bulk_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.power.bulk"].parameter_schema)
    assert "action" in bulk_schema["required"]
    # host.evacuate requires host.
    evac_schema: dict[str, Any] = dict(by_op["vmware.composite.host.evacuate"].parameter_schema)
    assert evac_schema["required"] == ["host"]
    # host.detach_from_vds requires host + dvs + fallback_network.
    detach_schema: dict[str, Any] = dict(
        by_op["vmware.composite.host.detach_from_vds"].parameter_schema
    )
    assert set(detach_schema["required"]) == {"host", "dvs", "fallback_network"}
    # cluster.patch requires cluster.
    patch_schema: dict[str, Any] = dict(by_op["vmware.composite.cluster.patch"].parameter_schema)
    assert patch_schema["required"] == ["cluster"]
    # vm.resize requires vm (at least one sizing field via anyOf).
    resize_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.resize"].parameter_schema)
    assert set(resize_schema["required"]) == {"vm"}
    assert "anyOf" in resize_schema
    # vm.nic.repoint requires vm + nic + portgroup_name.
    repoint_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.nic.repoint"].parameter_schema)
    assert set(repoint_schema["required"]) == {"vm", "nic", "portgroup_name"}
    # vm.device.cdrom requires vm + cdrom + action.
    cdrom_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.device.cdrom"].parameter_schema)
    assert set(cdrom_schema["required"]) == {"vm", "cdrom", "action"}

    # All write schemas pin additionalProperties=False.
    for op_id in _WRITE_OP_IDS:
        schema: dict[str, Any] = dict(by_op[op_id].parameter_schema)
        assert schema["additionalProperties"] is False, (
            f"{op_id}: parameter_schema missing additionalProperties:False"
        )


@pytest.mark.asyncio
async def test_write_composite_response_schemas_persist_with_status_enums(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each write composite's response_schema persists with the documented status enum.

    Lesson from #524's iter-2 fix-loop -- the read composites needed
    response_schemas added after the fact. The write composites ship
    with response_schemas upfront. The schema's ``status`` enum drives
    caller branch logic; if a composite's status alphabet changes, the
    schema needs to update too, and that update should be visible in
    code review.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows}
    # Every row has a non-empty response_schema with a status enum.
    expected_status_values: dict[str, set[str]] = {
        "vmware.composite.vm.create": {"created", "rolled_back"},
        # #2970: the pinned deploy operation is synchronous, so the
        # pending/timeout task-wait statuses are gone.
        "vmware.composite.vm.clone": {"completed"},
        # #2909: OVF/OVA content-library deploy — resolution + deploy-report
        # statuses; #3071 adds resolve_error (a content-library find fault).
        "vmware.composite.vm.deploy_from_library": {
            "deployed",
            "deploy_failed",
            "deploy_error",
            "invalid_reference",
            "library_not_found",
            "ambiguous_library",
            "item_not_found",
            "ambiguous_item",
            "resolve_error",
        },
        # #2970: the revert is a polled vim *_Task -> a poll timeout is a
        # legible status.
        "vmware.composite.vm.snapshot.revert": {"reverted", "ambiguous", "not_found", "timeout"},
        "vmware.composite.vm.migrate": {"migrated", "no_recommendation"},
        "vmware.composite.vm.power": {"ok", "error", "tools_unavailable"},
        "vmware.composite.vm.disk.grow": {"grown", "invalid_shrink", "disk_not_found", "timeout"},
        "vmware.composite.vm.disk.attach": {
            "attached",
            "invalid_vmdk_path",
            "invalid_unit",
            "controller_not_found",
            "unit_in_use",
            "timeout",
        },
        "vmware.composite.host.evacuate": {"evacuated", "partial", "aborted"},
        # #2970: the DVS detach is a polled vim ReconfigureDvs_Task.
        "vmware.composite.host.detach_from_vds": {"detached", "incomplete", "timeout"},
        # #3091: the portgroup writes are polled vim CreateDVPortgroup_Task /
        # ReconfigureDVPortgroup_Task; a pre-write refusal + poll timeout are
        # legible statuses.
        "vmware.composite.network.portgroup.create": {"created", "invalid_vlan_spec", "timeout"},
        "vmware.composite.network.portgroup.security.set": {
            "updated",
            "no_change_requested",
            "timeout",
        },
        "vmware.composite.cluster.patch": {"completed", "stopped"},
        "vmware.composite.vm.resize": {"resized", "requires_power_off", "no_change", "partial"},
        "vmware.composite.vm.nic.repoint": {"repointed", "not_found", "ambiguous"},
        "vmware.composite.vm.device.cdrom": {
            "removed",
            "updated",
            "disconnected",
            "invalid_request",
        },
    }
    for op_id, expected_values in expected_status_values.items():
        schema: dict[str, Any] = dict(by_op[op_id].response_schema)
        status_schema = dict(schema["properties"]["status"])
        assert set(status_schema["enum"]) == expected_values, (
            f"{op_id}: expected status enum {expected_values}, got {set(status_schema['enum'])}"
        )
    # vm.power.bulk has no top-level status; its response_schema
    # encodes `results` + `summary` + `aborted_on_failure` instead.
    bulk_resp: dict[str, Any] = dict(by_op["vmware.composite.vm.power.bulk"].response_schema)
    bulk_props = dict(bulk_resp["properties"])
    assert {"results", "summary", "aborted_on_failure"} <= set(bulk_props)


@pytest.mark.asyncio
async def test_write_composite_tags_include_composite_and_write(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every write composite row carries ``composite`` + ``write`` tags."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_WRITE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)
    for row in rows:
        assert "composite" in row.tags, f"{row.op_id}: missing composite tag"
        assert "write" in row.tags, f"{row.op_id}: missing write tag"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_is_idempotent_across_thirty_three(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar twice -> 38 rows total, embedding called 38x once."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == 38

    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    # Body-hash skip path -> second run is a no-op for the embedding
    # pipeline; the row count stays at 38.
    assert stub_embedding_service.encode_one.call_count == first_count

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_ALL_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 38


# ---------------------------------------------------------------------------
# Module-level handler identity (no closures, partials, lambdas)
# ---------------------------------------------------------------------------


def test_all_write_handlers_are_module_level_coroutine_functions() -> None:
    """Each write handler is a plain module-level ``async def``.

    ``derive_handler_ref()`` rejects closures / partials / lambdas at
    registration time; a regression wrapping a handler in
    ``functools.partial`` would surface here before the registrar
    even runs.
    """
    import inspect

    for handler in (
        vm_create_composite,
        vm_clone_composite,
        vm_deploy_from_library_composite,
        vm_clone_from_template_composite,
        vm_snapshot_revert_composite,
        vm_migrate_composite,
        vm_power_composite,
        vm_power_bulk_composite,
        vm_disk_grow_composite,
        vm_disk_attach_composite,
        vm_resize_composite,
        vm_nic_repoint_composite,
        vm_device_cdrom_composite,
        host_evacuate_composite,
        host_detach_from_vds_composite,
        cluster_patch_composite,
        guest_customization_spec_create_composite,
        vm_customize_composite,
    ):
        assert inspect.iscoroutinefunction(handler), f"{handler!r} is not a coroutine function"
        assert "<locals>" not in handler.__qualname__
        assert handler.__qualname__ != "<lambda>"
