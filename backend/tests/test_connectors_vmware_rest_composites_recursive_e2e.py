# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end recursive-composite test via production ``dispatch()``.

The unit tests in
:mod:`tests.test_connectors_vmware_rest_composites_write` mock
``dispatch_child`` directly. That proves the handler body wires the
right sub-op_ids + params, but it does **not** exercise the production
dispatcher's ``composite`` branch — which is where ``parent_audit_id``
linkage + the :data:`composite_depth_var` ramp + audit-tree persistence
actually live.

This module closes that gap for the first **production** recursive
composite, ``vmware.composite.host.evacuate`` (G3.1-T6 / #509):

1. Register a no-op vmware connector class against
   ``(product="vmware", version="9.0", impl_id="vmware-rest")``.
2. Register the **real** :func:`register_vmware_composite_operations`
   runner, which lands all 13 composite rows (including ``vm.migrate``
   and ``host.evacuate``).
3. Register the leaf typed sub-ops the recursive chain bottoms out on
   (``GET:/vcenter/vm`` listing, ``GET:/vcenter/cluster/{cluster}/drs/
   recommendations``, ``POST:/vcenter/vm/{vm}?action=relocate``,
   ``PATCH:/vcenter/host/{host}/maintenance?action=enter``) via real
   :func:`register_typed_operation` calls with stub handlers returning
   canned payloads.
4. Dispatch ``vmware.composite.host.evacuate`` against the production
   :func:`dispatch` entry point.
5. Assert the 3-level audit tree: ``host.evacuate`` (depth 0) →
   ``vm.migrate`` (depth 1) → typed leaf ops (depth 2) all share the
   expected ``parent_audit_id`` linkage, and the
   :data:`composite_depth_var` reached depth 2 mid-flight.

The test runs against the autouse SQLite migrations harness in
:mod:`tests.conftest` — no Docker / PG testcontainer required.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.composite import (
    COMPOSITE_DEPTH_TOP_LEVEL,
    composite_depth_var,
)
from meho_backplane.settings import get_settings

# The leaf typed sub-ops the recursive chain bottoms out on. Each is
# registered as a real ``source_kind="typed"`` row pointing at a
# module-level stub handler below.
_LEAF_OP_LIST_VMS = "GET:/vcenter/vm"
_LEAF_OP_GET_DRS_RECS = "GET:/vcenter/cluster/{cluster}/drs/recommendations"
_LEAF_OP_RELOCATE_VM = "POST:/vcenter/vm/{vm}?action=relocate"
_LEAF_OP_HOST_MAINTENANCE_ENTER = "PATCH:/vcenter/host/{host}/maintenance?action=enter"

# Captured ``composite_depth_var`` snapshots taken inside each leaf handler.
# Asserted after the dispatch returns so the test can prove the ramp
# 0 → 1 → 2 fired during the recursive run.
_DEPTH_OBSERVATIONS: list[int] = []


# ---------------------------------------------------------------------------
# Settings / fixtures
# ---------------------------------------------------------------------------


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
    _DEPTH_OBSERVATIONS.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _DEPTH_OBSERVATIONS.clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registrations skip ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub.

    Required because the dispatcher's audit path calls ``publish_event``
    on every audit-log row write; without the stub the real broadcast
    bus would attempt a write against a missing Postgres instance.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator() -> Operator:
    """Synthetic operator scoped to the canary tenant."""
    return Operator(
        sub="op-vmware-e2e",
        name="VMware Composite E2E Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a2"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for the resolver."""

    def __init__(self, version: str | None = "9.0") -> None:
        self.version = version


class _FakeVmwareTarget:
    """Minimal target shape the resolver / dispatcher reads from."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVmwareConnector(Connector):
    """Resolver-satisfying connector class — never actually called.

    The dispatcher resolves a connector instance only for
    ``source_kind="ingested"`` rows; ``typed`` and ``composite`` rows
    skip the instance lookup. The class is here so the registry
    resolver finds *something* under
    ``(vmware, 9.0, vmware-rest)`` — never invoked.
    """

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Leaf typed-op stub handlers
# ---------------------------------------------------------------------------
#
# Each handler records the live ``composite_depth_var`` so the test can
# assert the ramp 0 → 1 → 2. They are module-level ``async def`` so
# ``derive_handler_ref`` accepts them — the same constraint
# ``register_typed_operation()`` enforces in production.


async def _stub_list_vms_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stub for ``GET:/vcenter/vm`` — returns the canned VM listing.

    ``host.evacuate`` calls this at depth 1 (one composite frame above
    the typed handler).
    """
    _DEPTH_OBSERVATIONS.append(composite_depth_var.get())
    # Two VMs on the host with the same cluster moid — the per-VM
    # cluster resolution (M1 fix) reads ``cluster`` off each row.
    return {
        "value": [
            {"vm": "vm-aa", "cluster": "cluster-1"},
            {"vm": "vm-bb", "cluster": "cluster-1"},
        ]
    }


async def _stub_drs_recs_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stub for ``GET:/vcenter/cluster/{cluster}/drs/recommendations``.

    Recursive composite frames: ``host.evacuate`` (0) →
    ``vm.migrate`` (1) → this typed leaf (2). Depth observation here
    pins the ``composite_depth_var == 2`` reached at the bottom of the
    recursion.
    """
    _DEPTH_OBSERVATIONS.append(composite_depth_var.get())
    return {
        "value": [
            {"vm": "vm-aa", "target_host": "host-target"},
            {"vm": "vm-bb", "target_host": "host-target"},
        ]
    }


async def _stub_relocate_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stub for ``POST:/vcenter/vm/{vm}?action=relocate`` — succeeds."""
    _DEPTH_OBSERVATIONS.append(composite_depth_var.get())
    return {"value": {}}


async def _stub_host_maintenance_enter_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stub for ``PATCH:/vcenter/host/{host}/maintenance?action=enter``."""
    _DEPTH_OBSERVATIONS.append(composite_depth_var.get())
    return {"value": {}}


async def _register_leaf_typed_ops(stub_embedding_service: AsyncMock) -> None:
    """Land the 4 leaf typed-op descriptor rows the recursive chain hits.

    Each ``register_typed_operation`` call writes one
    ``source_kind="typed"`` row pointing at the module-level stub above.
    The descriptor's ``parameter_schema`` is permissive
    (``additionalProperties=True``) so the composite handlers can pass
    whatever params they like without per-op schema bookkeeping in this
    test.
    """
    permissive_schema = {"type": "object", "additionalProperties": True}
    leaf_specs: tuple[tuple[str, Any, str], ...] = (
        (
            _LEAF_OP_LIST_VMS,
            _stub_list_vms_handler,
            "Stub list-vms typed op for the recursive E2E test.",
        ),
        (
            _LEAF_OP_GET_DRS_RECS,
            _stub_drs_recs_handler,
            "Stub DRS-recommendations typed op for the recursive E2E test.",
        ),
        (
            _LEAF_OP_RELOCATE_VM,
            _stub_relocate_handler,
            "Stub relocate-VM typed op for the recursive E2E test.",
        ),
        (
            _LEAF_OP_HOST_MAINTENANCE_ENTER,
            _stub_host_maintenance_enter_handler,
            "Stub host-maintenance-enter typed op for the recursive E2E test.",
        ),
    )
    for op_id, handler, description in leaf_specs:
        await register_typed_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id=op_id,
            handler=handler,
            summary=description,
            description=description,
            parameter_schema=permissive_schema,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_evacuate_e2e_through_production_dispatch_builds_3_level_audit_tree(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """End-to-end: dispatch ``host.evacuate`` and assert the 3-level audit tree.

    Acceptance criterion (from #509's review iter-1 M2): the first
    production recursive composite must be exercised through the real
    :func:`dispatch` entry point, not just ``dispatch_child`` mocks.

    Tree shape under test:

    * ``vmware.composite.host.evacuate`` — depth 0, parent_audit_id NULL.
    * ``vmware.composite.vm.migrate`` (x 2 — one per listed VM) —
      depth 1, parent_audit_id = host.evacuate's audit row id.
    * Leaf typed sub-ops (``GET:/vcenter/vm``,
      ``GET:/vcenter/cluster/{cluster}/drs/recommendations``,
      ``POST:/vcenter/vm/{vm}?action=relocate``,
      ``PATCH:/vcenter/host/{host}/maintenance?action=enter``) —
      depth 2 where wrapped by vm.migrate, depth 1 where called
      directly by host.evacuate.

    Also asserts :data:`composite_depth_var` observed values cover the
    ramp 1 → 2 across leaf-handler invocations.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_NoOpVmwareConnector,
    )
    # Register all 13 composite rows (vm.migrate + host.evacuate are
    # the two the test exercises; the other 11 ride along harmlessly).
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    # Register the 4 leaf typed sub-ops the recursive chain bottoms on.
    await _register_leaf_typed_ops(stub_embedding_service)
    # Flip ``requires_approval`` off for the two composites under test
    # — v0.2 doesn't ship the approval workflow, so the production
    # ``policy_gate`` denies anything carrying ``requires_approval=True``
    # (see ``_validate.policy_gate``). The acceptance line under test
    # is the recursive-dispatch + audit-tree shape, not the approval-
    # gate behaviour itself (G10 territory). The full dangerous /
    # requires_approval=True posture stays asserted by the row-level
    # registration suite in
    # ``test_connectors_vmware_rest_composites_write_register.py``.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        await fresh.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.op_id.in_(
                    {
                        "vmware.composite.host.evacuate",
                        "vmware.composite.vm.migrate",
                    }
                )
            )
            .values(requires_approval=False)
        )
        await fresh.commit()
    # Cached descriptor lookups must drop the stale row state after the
    # UPDATE; otherwise the dispatcher reuses the pre-mutation copy and
    # the policy gate still denies on the cached ``requires_approval``.
    reset_dispatcher_caches()

    operator = _make_operator()
    target = _FakeVmwareTarget()

    # Pre-dispatch contextvar invariant: top-level is depth 0.
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL

    result = await dispatch(
        operator=operator,
        connector_id="vmware-rest-9.0",
        op_id="vmware.composite.host.evacuate",
        target=target,
        params={"host": "host-source"},
    )
    assert result.status == "ok", result.error
    # The composite returned the structured envelope with status=evacuated.
    assert isinstance(result.result, dict)
    assert result.result["status"] == "evacuated"
    assert result.result["maintenance_entered"] is True
    assert sorted(result.result["migrated_vms"]) == ["vm-aa", "vm-bb"]

    # Post-dispatch invariant: the contextvar is reset back to depth 0
    # — composite.get_dispatch_child uses tokens + finally to restore.
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL

    # ------------------------------------------------------------------
    # Depth ramp: leaf handlers were invoked at depths 1 (host.evacuate
    # frame) and 2 (host.evacuate → vm.migrate frame). The test ramps
    # 1 → 2 rather than 0 → 1 → 2 because depth is read *inside* the
    # leaf handler (one composite frame in already).
    # ------------------------------------------------------------------
    assert set(_DEPTH_OBSERVATIONS) >= {1, 2}, (
        f"expected depth ramp covering {{1, 2}}, observed {sorted(set(_DEPTH_OBSERVATIONS))}"
    )

    # ------------------------------------------------------------------
    # Audit-tree shape: load every audit row produced by the dispatch
    # and reconstruct the linkage by ``parent_audit_id``.
    # ------------------------------------------------------------------
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_(
                            {
                                "vmware.composite.host.evacuate",
                                "vmware.composite.vm.migrate",
                                _LEAF_OP_LIST_VMS,
                                _LEAF_OP_GET_DRS_RECS,
                                _LEAF_OP_RELOCATE_VM,
                                _LEAF_OP_HOST_MAINTENANCE_ENTER,
                            }
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    by_path: dict[str, list[AuditLog]] = {}
    for row in rows:
        by_path.setdefault(row.path, []).append(row)

    # Parent composite — exactly one row, no parent.
    host_evacuate_rows = by_path["vmware.composite.host.evacuate"]
    assert len(host_evacuate_rows) == 1
    parent = host_evacuate_rows[0]
    assert parent.parent_audit_id is None

    # vm.migrate composite — one row per listed VM (2 VMs in the stub).
    vm_migrate_rows = by_path["vmware.composite.vm.migrate"]
    assert len(vm_migrate_rows) == 2
    for vm_migrate in vm_migrate_rows:
        # Each vm.migrate row's parent is the host.evacuate row.
        assert vm_migrate.parent_audit_id == parent.id

    vm_migrate_ids = {r.id for r in vm_migrate_rows}

    # Typed leaf ops.
    list_vms_rows = by_path[_LEAF_OP_LIST_VMS]
    # host.evacuate calls GET:/vcenter/vm once at depth 1.
    assert len(list_vms_rows) == 1
    assert list_vms_rows[0].parent_audit_id == parent.id

    drs_rows = by_path[_LEAF_OP_GET_DRS_RECS]
    # vm.migrate calls DRS-recs once per VM (no target_host override).
    assert len(drs_rows) == 2
    for drs_row in drs_rows:
        # Each DRS row's parent is a vm.migrate row (depth 2).
        assert drs_row.parent_audit_id in vm_migrate_ids

    relocate_rows = by_path[_LEAF_OP_RELOCATE_VM]
    # vm.migrate calls relocate once per VM (after DRS produced a target).
    assert len(relocate_rows) == 2
    for relocate_row in relocate_rows:
        assert relocate_row.parent_audit_id in vm_migrate_ids

    maintenance_rows = by_path[_LEAF_OP_HOST_MAINTENANCE_ENTER]
    # host.evacuate calls maintenance-enter once at depth 1.
    assert len(maintenance_rows) == 1
    assert maintenance_rows[0].parent_audit_id == parent.id

    # ------------------------------------------------------------------
    # SQL-shape acceptance: a single query against (id, parent_audit_id,
    # path) round-trips the entire tree the operator-runbook query
    # uses.
    # ------------------------------------------------------------------
    audit_ids = {
        parent.id,
        *vm_migrate_ids,
        *(r.id for r in list_vms_rows),
        *(r.id for r in drs_rows),
        *(r.id for r in relocate_rows),
        *(r.id for r in maintenance_rows),
    }
    async with sessionmaker() as fresh:
        tree_rows = (
            await fresh.execute(
                select(
                    AuditLog.id,
                    AuditLog.parent_audit_id,
                    AuditLog.path,
                ).where(AuditLog.id.in_(audit_ids))
            )
        ).all()
    tree = {row.id: row for row in tree_rows}
    # The reconstructed tree has the right shape: 1 root, 2 depth-1
    # composites + 2 depth-1 typed leaves, 4 depth-2 typed leaves.
    roots = [r for r in tree.values() if r.parent_audit_id is None]
    assert len(roots) == 1
    assert roots[0].path == "vmware.composite.host.evacuate"
    depth_1_rows = [r for r in tree.values() if r.parent_audit_id == parent.id]
    # 2 vm.migrate composites + 1 list_vms typed + 1 maintenance typed = 4.
    assert len(depth_1_rows) == 4
    depth_2_rows = [r for r in tree.values() if r.parent_audit_id in vm_migrate_ids]
    # 2 DRS-recs typed + 2 relocate typed = 4.
    assert len(depth_2_rows) == 4
