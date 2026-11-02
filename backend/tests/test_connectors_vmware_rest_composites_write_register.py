# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the vmware-rest composites.

The tests check rules on the rows in ``_COMPOSITES``, not a copy of every
operation. So a new composite changes nothing in this file. The rules:

* Every op id is unique.
* Every handler exists: a module-level ``async def`` whose dotted path
  (``handler_ref``) imports back to the same function.
* Every group key is known: it has a "when to use" text in
  ``_WHEN_TO_USE_BY_GROUP``.
* The risk level fits the approval flag: a ``safe`` op never waits for
  approval, a ``dangerous`` or ``destructive`` op always does.
* Every parameter schema refuses unknown keys
  (``additionalProperties: false``).
* Every row is tagged ``composite``; a row tagged ``write`` waits for
  approval.

The registrar must write each row to ``endpoint_descriptor`` as it is
(handler path, group, risk level, approval flag, tags), and a second run
must not embed again. ``test_each_rule_fails_on_a_row_that_breaks_it``
proves each rule fails on a row that breaks it.

The schema tests at the end still pin the schemas of some older
composites. Pin a new composite's own schemas in its own feature test,
not here.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    _WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import import_handler
from meho_backplane.operations.typed_register import HandlerRefError, derive_handler_ref
from meho_backplane.settings import get_settings

_OP_IDS: tuple[str, ...] = tuple(spec.op_id for spec in _COMPOSITES)


def _check_registration_rules(specs: Iterable[CompositeSpec]) -> None:
    """Fail when a row breaks one of the registration rules."""
    seen: set[str] = set()
    for spec in specs:
        assert spec.op_id not in seen, f"{spec.op_id}: op id is not unique"
        seen.add(spec.op_id)
        assert inspect.iscoroutinefunction(spec.handler), f"{spec.op_id}: handler is not async"
        # derive_handler_ref refuses lambdas, closures and partials.
        handler_ref = derive_handler_ref(spec.handler)
        assert import_handler(handler_ref) is spec.handler, (
            f"{spec.op_id}: {handler_ref} does not import back to the handler"
        )
        assert spec.group_key in _WHEN_TO_USE_BY_GROUP, (
            f"{spec.op_id}: unknown group key {spec.group_key!r}"
        )
        if spec.safety_level == "safe":
            assert not spec.requires_approval, f"{spec.op_id}: a safe op waits for approval"
        if spec.safety_level in ("dangerous", "destructive"):
            assert spec.requires_approval, (
                f"{spec.op_id}: a {spec.safety_level} op must wait for approval"
            )
        assert spec.parameter_schema.get("additionalProperties") is False, (
            f"{spec.op_id}: parameter_schema missing additionalProperties:False"
        )
        assert "composite" in spec.tags, f"{spec.op_id}: missing composite tag"
        if "write" in spec.tags:
            assert spec.requires_approval, f"{spec.op_id}: a write op must wait for approval"


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


async def _registered_rows(stub_embedding_service: AsyncMock) -> dict[str, EndpointDescriptor]:
    """Run the registrar and return the composite rows by op id."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    return {row.op_id: row for row in rows}


# ---------------------------------------------------------------------------
# Rules on the rows
# ---------------------------------------------------------------------------


def test_every_composite_row_follows_the_registration_rules() -> None:
    """Every row in ``_COMPOSITES`` follows the rules in the module docstring."""
    assert _COMPOSITES
    _check_registration_rules(_COMPOSITES)


def _a_row(**changes: Any) -> CompositeSpec:
    """A valid write row, with *changes* applied."""
    row = next(spec for spec in _COMPOSITES if spec.op_id == "vmware.composite.vm.create")
    return row._replace(**changes)


def _closure_handler() -> Any:
    async def inner() -> dict[str, Any]:
        return {}

    return inner


def _handler_named(qualname: str) -> Any:
    """A module-level handler whose dotted path points at *qualname* in the vm module."""

    async def handler() -> dict[str, Any]:
        return {}

    handler.__module__ = _a_row().handler.__module__
    handler.__qualname__ = qualname
    return handler


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([_a_row(), _a_row()], id="op-id-not-unique"),
        pytest.param([_a_row(handler=lambda: None)], id="handler-is-a-lambda"),
        pytest.param([_a_row(handler=_closure_handler())], id="handler-is-a-closure"),
        pytest.param([_a_row(handler=_COMPOSITES[0].op_id)], id="handler-is-not-a-function"),
        pytest.param([_a_row(handler=_handler_named("no_such_handler"))], id="handler-not-found"),
        pytest.param(
            [_a_row(handler=_handler_named(_a_row().handler.__qualname__))],
            id="handler-path-names-another-function",
        ),
        pytest.param([_a_row(group_key="no-such-group")], id="group-key-unknown"),
        pytest.param([_a_row(safety_level="safe")], id="safe-op-waits-for-approval"),
        pytest.param(
            [_a_row(safety_level="dangerous", requires_approval=False, tags=["composite"])],
            id="dangerous-op-without-approval",
        ),
        pytest.param(
            [_a_row(safety_level="destructive", requires_approval=False, tags=["composite"])],
            id="destructive-op-without-approval",
        ),
        pytest.param(
            [_a_row(parameter_schema={"type": "object"})], id="schema-allows-unknown-keys"
        ),
        pytest.param([_a_row(tags=["write"])], id="composite-tag-missing"),
        pytest.param(
            [_a_row(safety_level="caution", requires_approval=False)],
            id="write-tag-without-approval",
        ),
    ],
)
def test_each_rule_fails_on_a_row_that_breaks_it(rows: list[CompositeSpec]) -> None:
    """Each registration rule is not empty: a row that breaks it fails."""
    with pytest.raises((AssertionError, HandlerRefError, ImportError)):
        _check_registration_rules(rows)


# ---------------------------------------------------------------------------
# The registrar writes each row as it is
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registrar_writes_every_composite_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row lands in ``endpoint_descriptor`` with the fields of its spec."""
    by_op = await _registered_rows(stub_embedding_service)
    assert set(by_op) == set(_OP_IDS)
    for spec in _COMPOSITES:
        row = by_op[spec.op_id]
        assert row.source_kind == "composite"
        assert row.tenant_id is None
        assert row.is_enabled is True
        assert row.method is None
        assert row.path is None
        assert row.handler_ref == derive_handler_ref(spec.handler), spec.op_id
        assert row.safety_level == spec.safety_level, spec.op_id
        assert row.requires_approval is spec.requires_approval, spec.op_id
        assert list(row.tags) == list(spec.tags), spec.op_id


@pytest.mark.asyncio
async def test_registrar_puts_each_composite_in_the_group_of_its_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's group is the row's ``group_key`` on the 9.0 catalog."""
    by_op = await _registered_rows(stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        group_rows = (await fresh.execute(select(OperationGroup))).scalars().all()
    groups_by_id = {g.id: g for g in group_rows}
    for spec in _COMPOSITES:
        desc = by_op[spec.op_id]
        assert desc.group_id is not None, f"{desc.op_id} has no group_id"
        group = groups_by_id[desc.group_id]
        assert group.group_key == spec.group_key, (
            f"{desc.op_id}: expected {spec.group_key!r}, got {group.group_key!r}"
        )
        assert group.product == "vmware"
        assert group.version == "9.0"
        assert group.impl_id == "vmware-rest"


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_is_idempotent(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar twice embeds each row once and keeps one row per op."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == len(_COMPOSITES)

    # Body-hash skip path -> the second run is a no-op for the embedding pipeline.
    by_op = await _registered_rows(stub_embedding_service)
    assert stub_embedding_service.encode_one.call_count == first_count
    assert set(by_op) == set(_OP_IDS)


@pytest.mark.asyncio
async def test_namespace_status_registration_preserves_poll_scalars_after_jsonflux_reduction(
    stub_embedding_service: AsyncMock,
) -> None:
    """Persist the descriptor hint the dispatcher forwards to JSONFlux.

    ``messages`` is the response's one real list, so a wide projected message
    can materialize a handle even at its 25-row default.  These scalar fields
    are the asynchronous create/delete poll contract and must remain inline.
    The dispatcher-to-reducer wiring itself is covered by the end-to-end
    result-scalars regression in ``test_operations_jsonflux_reducer``.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = await fresh.scalar(
            select(EndpointDescriptor).where(
                EndpointDescriptor.op_id == "vmware.composite.namespace.status"
            )
        )
    assert row is not None
    assert row.llm_instructions is not None
    assert row.llm_instructions["result_scalars"] == {
        "keys": [
            "namespace",
            "exists",
            "config_status",
            "ready",
            "description",
            "message_count",
        ]
    }
    assert row.llm_instructions["result_objects"] == {
        "objects": {"stats": ["cpu_used", "memory_used", "storage_used"]}
    }


@pytest.mark.asyncio
async def test_write_composite_parameter_schemas_persist_with_required_fields(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's parameter_schema has the documented required keys + additionalProperties=False."""
    by_op = await _registered_rows(stub_embedding_service)
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
    # vm.snapshot.revert requires vm + one of snapshot_name / snapshot_id.
    revert_schema: dict[str, Any] = dict(
        by_op["vmware.composite.vm.snapshot.revert"].parameter_schema
    )
    assert set(revert_schema["required"]) == {"vm"}
    assert revert_schema["oneOf"] == [
        {"required": ["snapshot_name"]},
        {"required": ["snapshot_id"]},
    ]
    # vm.snapshot.create requires vm + snapshot_name; delete requires vm + snapshot_id.
    snap_create: dict[str, Any] = dict(
        by_op["vmware.composite.vm.snapshot.create"].parameter_schema
    )
    assert set(snap_create["required"]) == {"vm", "snapshot_name"}
    snap_delete: dict[str, Any] = dict(
        by_op["vmware.composite.vm.snapshot.delete"].parameter_schema
    )
    assert set(snap_delete["required"]) == {"vm", "snapshot_id"}
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
    # host.maintenance.exit requires host (#4967).
    exit_schema: dict[str, Any] = dict(
        by_op["vmware.composite.host.maintenance.exit"].parameter_schema
    )
    assert exit_schema["required"] == ["host"]
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
    by_op = await _registered_rows(stub_embedding_service)
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
        "vmware.composite.vm.snapshot.create": {
            "created",
            "unchanged",
            "not_found",
            "invalid_request",
            "timeout",
        },
        "vmware.composite.vm.snapshot.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "still_present",
            "timeout",
        },
        "vmware.composite.vm.migrate": {"migrated", "no_recommendation"},
        "vmware.composite.vm.power": {"ok", "error", "tools_unavailable"},
        "vmware.composite.vm.disk.grow": {"grown", "invalid_shrink", "disk_not_found", "timeout"},
        # #3880: set / idempotent unchanged + pre-write refusals + a
        # read-back mismatch + poll timeout (a task fault raises).
        "vmware.composite.vm.resource_allocation.set": {
            "set",
            "unchanged",
            "invalid_request",
            "vm_not_found",
            "partial",
            "timeout",
        },
        "vmware.composite.vm.disk.attach": {
            "attached",
            "invalid_vmdk_path",
            "invalid_unit",
            "controller_not_found",
            "unit_in_use",
            "timeout",
        },
        # #4895: a maintenance-task poll timeout is a status (the task still
        # runs), and a host outside a cluster is refused before any change.
        "vmware.composite.host.evacuate": {
            "evacuated",
            "partial",
            "aborted",
            "timeout",
            "not_in_cluster",
        },
        # #4967: ``unchanged`` (not in maintenance mode) and ``not_connected``
        # are pre-change refusals; ``timeout`` is a status with the task id.
        "vmware.composite.host.maintenance.exit": {
            "exited",
            "unchanged",
            "not_connected",
            "timeout",
        },
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
        # vlan.set reconfigures an existing portgroup's VLAN; idempotent
        # unchanged + a pre-write invalid_vlan_spec refusal + poll timeout.
        "vmware.composite.network.portgroup.vlan.set": {
            "set",
            "unchanged",
            "invalid_vlan_spec",
            "timeout",
        },
        "vmware.composite.cluster.patch": {"completed", "stopped"},
        "vmware.composite.vm.resize": {"resized", "requires_power_off", "no_change", "partial"},
        "vmware.composite.vm.nic.repoint": {
            "repointed",
            "not_found",
            "ambiguous",
            "invalid_request",
        },
        "vmware.composite.vm.device.cdrom": {
            "removed",
            "updated",
            "disconnected",
            "invalid_request",
            "result_unknown",
        },
        # #3494: governed NFS tag-based SPBM storage-policy create/delete.
        # #3826 adds the resolve-before-create idempotency statuses: adopted
        # (same-named policy + matching rule set reused) and the two terminal
        # conflicts (category / policy of the same name, incompatible shape).
        "vmware.composite.storage_policy.create": {
            "created",
            "adopted",
            "datastore_not_found",
            "category_conflict",
            "policy_conflict",
            "policy_create_failed",
        },
        "vmware.composite.storage_policy.delete": {
            "deleted",
            "delete_failed",
            "still_present",
        },
        # #3502: governed vSphere Namespace create/delete.
        "vmware.composite.namespace.create": {"created"},
        "vmware.composite.namespace.delete": {
            "deleted",
            "removing",
            "still_present",
        },
        # #3339 teardown: verified delete / already absent / refused before any
        # write / still reads back (+ a task-poll timeout for the *_Task deletes,
        # + invalid_request for a bad reference). A vSphere fault raises.
        "vmware.composite.network.portgroup.delete": {
            "deleted",
            "unchanged",
            "precondition_failed",
            "still_present",
            "timeout",
        },
        "vmware.composite.host.standard_portgroup.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
        },
        "vmware.composite.folder.delete": {
            "deleted",
            "unchanged",
            "precondition_failed",
            "still_present",
            "timeout",
        },
        "vmware.composite.datastore.file.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
            "timeout",
        },
        "vmware.composite.datastore.dir.create": {
            "created",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "not_verified",
        },
        "vmware.composite.content_library.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
        },
        "vmware.composite.content_library.item.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
        },
        # DRS rule delete: the teardown statuses + a reconfigure poll timeout;
        # invalid_request for an unknown cluster.
        "vmware.composite.cluster.drs_rule.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
            "timeout",
        },
        # DRS group delete: the same statuses as the rule delete.
        "vmware.composite.cluster.drs_group.delete": {
            "deleted",
            "unchanged",
            "invalid_request",
            "precondition_failed",
            "still_present",
            "timeout",
        },
        "vmware.composite.cluster.drs_group.members.set": {
            "updated",
            "unchanged",
            "invalid_request",
            "group_not_found",
            "names_not_found",
            "mismatch",
            "timeout",
        },
        # Local content-library create + add a file from a URL.
        "vmware.composite.content_library.create": {
            "created",
            "unchanged",
            "precondition_failed",
            "datastore_not_found",
            "ambiguous_datastore",
            "refused",
            "not_verified",
        },
        "vmware.composite.content_library.item.upload": {
            "uploaded",
            "unchanged",
            "in_progress",
            "upload_failed",
            "invalid_request",
            "library_not_found",
            "precondition_failed",
            "refused",
            "not_verified",
        },
        # Licence assign: the write, the no-op, and three refusals before any change.
        "vmware.composite.license.assign": {
            "assigned",
            "unchanged",
            "license_not_found",
            "license_ambiguous",
            "asset_not_found",
        },
        # Install a new vCenter (#4394): the start answers accepted at once.
        "vmware.composite.vcenter.install": {
            "accepted",
            "already_running",
            "waiting_for_answer",
            "already_done",
            "failed_before",
            "not_ready",
            "unknown_state",
            "not_reachable",
            "check_failed",
            "start_failed",
            "start_unknown",
        },
        "vmware.composite.vcenter.deployment.answer": {
            "accepted",
            "no_question",
            "unknown_question",
            "answer_not_allowed",
            "answer_failed",
            "answer_unknown",
        },
        # Host membership (#4349): the add answers accepted at once.
        "vmware.composite.host.add": {
            "accepted",
            "already_added",
            "already_running",
            "parent_not_found",
            "needs_vcenter",
            "start_failed",
            "start_unknown",
        },
        "vmware.composite.host.remove": {
            "removed",
            "unchanged",
            "precondition_failed",
            "needs_vcenter",
            "still_present",
            "timeout",
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
