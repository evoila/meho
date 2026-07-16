# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end recursive-composite test via production ``dispatch()``.

The unit tests in
:mod:`tests.test_connectors_vmware_rest_composites_write` mock the
connector session directly. That proves the handler body wires the right
sub-ops + params, but it does **not** exercise the production dispatcher's
``composite`` branch -- which is where ``parent_audit_id`` linkage + the
:data:`composite_depth_var` ramp + audit-tree persistence actually live.

This module closes that gap for the first **production** recursive
composite, ``vmware.composite.host.evacuate`` (G3.1-T6 / #509), under the
post-#2256 direct-session model:

* ``host.evacuate`` (depth 0) still recurses into
  ``vmware.composite.vm.migrate`` via ``dispatch_child`` -- a
  registrar-guaranteed ``source_kind="composite"`` row, the #2248 carve-out
  the migration deliberately keeps on the catalog-routed path -- so the
  recursion produces one ``vm.migrate`` audit row (depth 1) per VM, each
  parented to the ``host.evacuate`` row.
* Every **raw-REST leaf** the two composites touch (the VM listing, the DRS
  read, the relocate write, the maintenance-enter write) now runs
  **directly on the connector session**, so it writes **no** audit row of
  its own -- the top-level composite's row is the audit anchor (the
  documented direct-session behaviour: the per-sub-op child rows disappear).

The test therefore asserts the new two-level audit shape (root +
per-VM ``vm.migrate``), the absence of any leaf-op audit rows, and the
``composite_depth_var`` ramp 0 -> 1 observed across the direct sub-calls
(host.evacuate's own reads at depth 0, vm.migrate's reads/writes at
depth 1). The recording connector is seeded as the dispatcher's resolved
instance; no Docker / PG testcontainer required.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    dispatch,
    reset_dispatcher_caches,
)
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.composite import (
    COMPOSITE_DEPTH_TOP_LEVEL,
    composite_depth_var,
)
from meho_backplane.settings import get_settings

# The raw-REST leaf paths the recursive chain bottoms out on. Post-#2256
# each runs directly on the connector session (no descriptor row, no audit
# row) -- the test asserts these paths produce ZERO ``AuditLog`` rows.
_LEAF_OP_LIST_VMS = "GET:/vcenter/vm"
_LEAF_OP_GET_DRS_RECS = "GET:/vcenter/cluster/{cluster}/drs/recommendations"
_LEAF_OP_RELOCATE_VM = "POST:/vcenter/vm/{vm}?action=relocate"
_LEAF_OP_HOST_MAINTENANCE_ENTER = "PATCH:/vcenter/host/{host}/maintenance?action=enter"


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
    yield
    reset_dispatcher_caches()
    clear_registry()


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
    """Replace :func:`publish_event` with a recording stub."""
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
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


class _DepthRecordingConnector:
    """Recording connector double that snapshots the composite depth per call.

    Seeded as the dispatcher's resolved instance so the migrated composites
    issue their raw-REST sub-ops on it directly. Each sub-call records the
    live :data:`composite_depth_var` so the test can prove the ramp: the
    ``host.evacuate`` frame's own reads run at depth 0, the ``vm.migrate``
    frame's reads/writes (reached via ``dispatch_child``) at depth 1.
    """

    _MOUNT = "/api"

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.depths: list[int] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    def _spec(self, path: str) -> str:
        return path[len(self._MOUNT) :] if path.startswith(self._MOUNT) else path

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.depths.append(composite_depth_var.get())
        spec = self._spec(path)
        self.calls.append(("GET", spec))
        return self.responses.get(spec, {"value": {}})

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        data: Any = None,
        extra_headers: Any = None,
    ) -> Any:
        self.depths.append(composite_depth_var.get())
        spec = self._spec(path)
        self.calls.append((verb, spec))
        return self.responses.get(spec, {"value": {}})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_evacuate_e2e_through_production_dispatch_audit_tree(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Dispatch ``host.evacuate`` and assert the post-migration audit shape.

    * ``host.evacuate`` (root, ``parent_audit_id`` NULL).
    * one ``vm.migrate`` audit row per VM (depth 1, parent = host.evacuate),
      produced by the ``dispatch_child`` recursion the migration keeps.
    * ZERO audit rows for the raw-REST leaves -- they run directly on the
      session, so the top-level composite's row is the only audit anchor.

    Also asserts the ``composite_depth_var`` ramp 0 -> 1 across the direct
    sub-calls.
    """
    recorder = _DepthRecordingConnector()
    recorder.responses.update(
        {
            "/vcenter/vm": {
                "value": [
                    {"vm": "vm-aa", "cluster": "cluster-1"},
                    {"vm": "vm-bb", "cluster": "cluster-1"},
                ]
            },
            "/vcenter/cluster/cluster-1/drs/recommendations": {
                "value": [
                    {"vm": "vm-aa", "target_host": "host-target"},
                    {"vm": "vm-bb", "target_host": "host-target"},
                ]
            },
        }
    )

    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=VmwareRestConnector,
    )
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)

    # Flip ``requires_approval`` off for the two composites under test so the
    # top-level policy gate auto-executes (the recursion + audit-tree shape is
    # the contract here, not the approval gate). Re-seed the recorder after
    # the cache reset -- ``reset_dispatcher_caches`` also clears the instance
    # cache.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        await fresh.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.op_id.in_(
                    {"vmware.composite.host.evacuate", "vmware.composite.vm.migrate"}
                )
            )
            .values(requires_approval=False)
        )
        await fresh.commit()
    reset_dispatcher_caches()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]

    operator = _make_operator()
    target = _FakeVmwareTarget()

    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL

    result = await dispatch(
        operator=operator,
        connector_id="vmware-rest-9.0",
        op_id="vmware.composite.host.evacuate",
        target=target,
        params={"host": "host-source"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "evacuated"
    assert result.result["maintenance_entered"] is True
    assert sorted(result.result["migrated_vms"]) == ["vm-aa", "vm-bb"]

    # Post-dispatch invariant: the contextvar is reset back to depth 0.
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL

    # Depth ramp: host.evacuate's own reads/writes ran at depth 0; the
    # vm.migrate frames (reached via dispatch_child) ran their reads/writes
    # at depth 1.
    assert set(recorder.depths) >= {0, 1}, (
        f"expected depth ramp covering {{0, 1}}, observed {sorted(set(recorder.depths))}"
    )

    # ------------------------------------------------------------------
    # Audit-tree shape (post-migration): root + per-VM vm.migrate only.
    # ------------------------------------------------------------------
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_(
                            {
                                "vmware.composite.host.evacuate",
                                "vmware.composite.vm.migrate",
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

    host_evacuate_rows = by_path["vmware.composite.host.evacuate"]
    assert len(host_evacuate_rows) == 1
    parent = host_evacuate_rows[0]
    assert parent.parent_audit_id is None

    vm_migrate_rows = by_path["vmware.composite.vm.migrate"]
    assert len(vm_migrate_rows) == 2
    for vm_migrate in vm_migrate_rows:
        assert vm_migrate.parent_audit_id == parent.id

    # The raw-REST leaves ran directly on the session -> no audit rows.
    async with sessionmaker() as fresh:
        leaf_count = await fresh.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.path.in_(
                    {
                        _LEAF_OP_LIST_VMS,
                        _LEAF_OP_GET_DRS_RECS,
                        _LEAF_OP_RELOCATE_VM,
                        _LEAF_OP_HOST_MAINTENANCE_ENTER,
                    }
                )
            )
        )
    assert leaf_count == 0, "direct-session leaf sub-ops must write no audit rows"
