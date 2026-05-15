# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the 8 vmware-rest write composites.

Coverage matrix (G3.1-T6 / #509 acceptance criteria):

* All 8 expected write ``op_id`` rows land in ``endpoint_descriptor``
  with ``source_kind="composite"``, ``safety_level="dangerous"``,
  ``requires_approval=True`` (T4's defaults intentionally inherited).
* Each row's ``handler_ref`` resolves to the module-level dotted path
  in ``composites/_write``.
* Each row's ``group_key`` resolves to ``vm`` / ``host`` / ``cluster``
  per the canary's stub-LLM taxonomy.
* Combined with #508's 5 reads, the registrar produces **13 rows**
  total -- the Definition-of-done line in #227's body.
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
    host_detach_from_vds_composite,
    host_evacuate_composite,
    register_vmware_composite_operations,
    vm_clone_composite,
    vm_create_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

# 8 write composites (T6 / #509).
_WRITE_OP_IDS: tuple[str, ...] = (
    "vmware.composite.vm.create",
    "vmware.composite.vm.clone",
    "vmware.composite.vm.snapshot.revert",
    "vmware.composite.vm.migrate",
    "vmware.composite.vm.power.bulk",
    "vmware.composite.host.evacuate",
    "vmware.composite.host.detach_from_vds",
    "vmware.composite.cluster.patch",
)

# 5 reads (T5 / #508) -- carried over so the combined-count assertion
# does not have to import _read constants.
_READ_OP_IDS: tuple[str, ...] = (
    "vmware.composite.cluster.drs_recommendations",
    "vmware.composite.event.tail",
    "vmware.composite.performance.summary",
    "vmware.composite.datastore.usage",
    "vmware.composite.network.portgroup.audit",
)

# 13 total -- the #227 G3.1 Definition-of-done line.
_ALL_OP_IDS: tuple[str, ...] = _READ_OP_IDS + _WRITE_OP_IDS


_EXPECTED_HANDLER_REF_BY_OP: dict[str, str] = {
    "vmware.composite.vm.create": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_create_composite"
    ),
    "vmware.composite.vm.clone": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_clone_composite"
    ),
    "vmware.composite.vm.snapshot.revert": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_snapshot_revert_composite"
    ),
    "vmware.composite.vm.migrate": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_migrate_composite"
    ),
    "vmware.composite.vm.power.bulk": (
        "meho_backplane.connectors.vmware_rest.composites._write.vm_power_bulk_composite"
    ),
    "vmware.composite.host.evacuate": (
        "meho_backplane.connectors.vmware_rest.composites._write.host_evacuate_composite"
    ),
    "vmware.composite.host.detach_from_vds": (
        "meho_backplane.connectors.vmware_rest.composites._write.host_detach_from_vds_composite"
    ),
    "vmware.composite.cluster.patch": (
        "meho_backplane.connectors.vmware_rest.composites._write.cluster_patch_composite"
    ),
}


_EXPECTED_GROUP_KEY_BY_OP: dict[str, str] = {
    "vmware.composite.vm.create": "vm",
    "vmware.composite.vm.clone": "vm",
    "vmware.composite.vm.snapshot.revert": "vm",
    "vmware.composite.vm.migrate": "vm",
    "vmware.composite.vm.power.bulk": "vm",
    "vmware.composite.host.evacuate": "host",
    "vmware.composite.host.detach_from_vds": "host",
    "vmware.composite.cluster.patch": "cluster",
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
# 8 write composites land alongside the 5 reads (13 total)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_inserts_eight_write_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar lands all 8 write op_ids in ``endpoint_descriptor``."""
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
async def test_full_registration_produces_thirteen_composite_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """5 reads (#508) + 8 writes (#509) = 13 composite rows. Definition-of-done bar."""
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
    assert len(rows) == 13


@pytest.mark.asyncio
async def test_every_write_composite_row_uses_dangerous_requires_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each write row carries T4's defaults: dangerous + requires_approval=True.

    Load-bearing: write composites should pop the approval queue on
    every dispatch. A misconfigured read-override would silently
    permit unauthenticated mutation; pinning the policy here means CI
    catches a regression before lifespan-startup.
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
    # Prove the query actually returned all 8 write rows before iterating —
    # otherwise the loop is vacuous when the set is empty / partial.
    assert {row.op_id for row in rows} == set(_WRITE_OP_IDS)
    for row in rows:
        assert row.safety_level == "dangerous", (
            f"{row.op_id}: expected dangerous, got {row.safety_level!r}"
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
    """5 vm.* in ``vm``, 2 host.* in ``host``, 1 cluster.* in ``cluster``."""
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
    # vm.create requires folder_name / name / guest_os.
    create_schema: dict[str, Any] = dict(by_op["vmware.composite.vm.create"].parameter_schema)
    assert set(create_schema["required"]) == {"folder_name", "name", "guest_os"}
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
        "vmware.composite.vm.clone": {"completed", "pending", "timeout"},
        "vmware.composite.vm.snapshot.revert": {"reverted", "ambiguous", "not_found"},
        "vmware.composite.vm.migrate": {"migrated", "no_recommendation"},
        "vmware.composite.host.evacuate": {"evacuated", "partial", "aborted"},
        "vmware.composite.host.detach_from_vds": {"detached", "incomplete"},
        "vmware.composite.cluster.patch": {"completed", "stopped"},
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
async def test_register_vmware_composite_operations_is_idempotent_across_thirteen(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar twice -> 13 rows total, embedding called 13x once."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == 13

    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    # Body-hash skip path -> second run is a no-op for the embedding
    # pipeline; the row count stays at 13.
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
    assert len(rows) == 13


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
        vm_snapshot_revert_composite,
        vm_migrate_composite,
        vm_power_bulk_composite,
        host_evacuate_composite,
        host_detach_from_vds_composite,
        cluster_patch_composite,
    ):
        assert inspect.iscoroutinefunction(handler), f"{handler!r} is not a coroutine function"
        assert "<locals>" not in handler.__qualname__
        assert handler.__qualname__ != "<lambda>"
