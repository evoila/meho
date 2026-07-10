# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end activation tests for the 8 vmware-rest write composites.

Post-#2256 the write composites dispatch every raw-REST sub-op **directly
on the connector session** (``connector._get_json`` / ``connector._post_json``
mounted through ``connector.mount_op_path``) rather than through
``dispatch_child``-routed ingested descriptor rows. This module proves the
migration holds through the **production**
:func:`~meho_backplane.operations.dispatch` entry point, with **zero**
ingested descriptor rows in the catalog (the fresh-boot / two-world DoD):

1. **Fresh-boot execution** -- each composite dispatched through
   :func:`dispatch` against a connector whose session is a recording double
   (seeded into the dispatcher's connector-instance cache) runs to a benign
   business status; no ``composite_l2_missing`` / ``unknown_op`` can arise
   because nothing is resolved through the catalog.
2. **Sub-op sequence + rollback branch** -- each composite's recorded
   ``(verb, path)`` chain is asserted against the documented orchestration
   workflow; ``vm.create`` additionally exercises the rollback branch
   (NIC-attach transport failure -> ``DELETE:/vcenter/vm/{vm}``).
3. **The human approval-queue path** -- a USER principal dispatching a
   ``requires_approval=True`` composite is parked at ``awaiting_approval``
   (G11.7-T1 #1401 routing) at the **top level**, a distinct human reviewer
   approves the parked request, and the ``_approved=True`` resume re-dispatch
   executes the composite. The per-sub-op governance seam
   (:func:`~meho_backplane.operations.composite.enforce_subop_policy`,
   ``requires_approval=False``) auto-executes for the approved human on the
   resume path, so the writes proceed without a second gate.

Determinism: the recording connector serves canned vSphere REST envelopes
in-process (no vcsim / testcontainer); the respx-transport parity proof
lives in :mod:`tests.integration.test_connectors_vmware_rest_vcsim`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import register_vmware_composite_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a3")


# ---------------------------------------------------------------------------
# Fixtures
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
    """Reset dispatcher caches + connector registry (incl. the instance cache)."""
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


def _make_operator(
    *,
    sub: str = "op-vmware-write-e2e",
    principal_kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: UUID = _TENANT_ID,
) -> Operator:
    """Synthetic operator scoped to the write-composite E2E tenant.

    Defaults to a USER (human) principal — the approval-queue path under
    test fires for human/service principals on a ``requires_approval`` op
    (G11.7-T1 #1401).
    """
    return Operator(
        sub=sub,
        name="VMware Write Composite E2E",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for the resolver."""

    def __init__(self, version: str | None = "9.0") -> None:
        self.version = version


class _FakeVmwareTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = target_id or uuid.uuid4()
        # Tenant-unique cache key component (#1642/#1672).
        self.tenant_id: UUID = _TENANT_ID
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


# ---------------------------------------------------------------------------
# Recording connector double (seeded as the dispatcher's resolved instance)
# ---------------------------------------------------------------------------


def _http_error(status: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{url}'", request=request, response=response
    )


class _RecordingVmwareConnector:
    """Records every direct sub-call the composites issue on the session.

    Stands in for the resolved :class:`VmwareRestConnector` instance the
    dispatcher injects into a composite handler (#2251). Sub-ops mount their
    spec-relative path onto ``/api`` via :meth:`mount_op_path`, then read /
    write it; this double records ``(verb, spec-relative path)`` and serves a
    canned envelope keyed by that spec path (default ``{"value": {}}``). Spec
    paths registered in ``failures`` raise :exc:`httpx.HTTPStatusError` to
    drive the rollback / partial-failure branches. The instance is shared
    across a composite's recursion (``host.evacuate`` -> ``vm.migrate``
    resolve the same class), so one call log captures the whole tree.
    """

    _MOUNT = "/api"

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.failures: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

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
        spec = self._spec(path)
        self.calls.append((verb, spec))
        if spec in self.failures:
            raise _http_error(500, f"https://vc{self._MOUNT}{spec}")
        return self.responses.get(spec, {"value": {}})


def _seed_connector(recorder: _RecordingVmwareConnector) -> None:
    """Register the connector class + seed its resolved instance as *recorder*.

    The dispatcher resolves the ``(vmware, 9.0, vmware-rest)`` class from the
    target then calls ``get_or_create_connector_instance`` — pre-seeding the
    cache makes that return the recording double instead of a real
    Vault-backed connector.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=VmwareRestConnector,
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]


async def _bootstrap(
    recorder: _RecordingVmwareConnector, stub_embedding_service: AsyncMock
) -> None:
    """Register the connector + all 14 composites and seed the recorder."""
    _seed_connector(recorder)
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)


async def _clear_requires_approval(op_ids: set[str], recorder: _RecordingVmwareConnector) -> None:
    """Flip ``requires_approval`` off for *op_ids* and drop stale caches.

    Used by the fresh-boot + sequence tests: the sub-op sequence + rollback
    branches are the contract there, not the top-level approval gate, so the
    composites auto-execute. The approval-queue tests keep
    ``requires_approval=True`` and route through park -> approve -> resume.

    ``reset_dispatcher_caches`` also clears the connector-instance cache, so
    the seeded recorder is re-activated afterwards or the dispatcher would
    instantiate a real (Vault-backed) connector on the next resolve.
    """
    async with get_sessionmaker()() as fresh:
        await fresh.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id.in_(op_ids))
            .values(requires_approval=False)
        )
        await fresh.commit()
    reset_dispatcher_caches()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Composite metadata
# ---------------------------------------------------------------------------

_WRITE_COMPOSITES: dict[str, str] = {
    "vmware.composite.vm.create": "vm.create",
    "vmware.composite.vm.clone": "vm.clone",
    "vmware.composite.vm.snapshot.revert": "vm.snapshot.revert",
    "vmware.composite.vm.migrate": "vm.migrate",
    "vmware.composite.vm.power": "vm.power",
    "vmware.composite.vm.power.bulk": "vm.power.bulk",
    "vmware.composite.host.evacuate": "host.evacuate",
    "vmware.composite.host.detach_from_vds": "host.detach_from_vds",
    "vmware.composite.cluster.patch": "cluster.patch",
}


def _benign_params_for(composite_op_id: str) -> dict[str, Any]:
    """Minimal schema-valid params for a benign (empty-listing) dispatch."""
    return {
        "vmware.composite.vm.create": {
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
        },
        "vmware.composite.vm.clone": {
            "source_vm": "vm-1",
            "target_name": "vm-clone",
            "library_item": "lib-1",
            "wait_for_completion": False,
        },
        "vmware.composite.vm.snapshot.revert": {
            "vm": "vm-1",
            "snapshot_name": "snap-a",
        },
        "vmware.composite.vm.migrate": {
            "vm": "vm-1",
            "cluster": "domain-c1",
        },
        "vmware.composite.vm.power": {"vm": "vm-1", "verb": "on"},
        "vmware.composite.vm.power.bulk": {"action": "start"},
        "vmware.composite.host.evacuate": {"host": "host-1"},
        "vmware.composite.host.detach_from_vds": {
            "host": "host-1",
            "dvs": "dvs-1",
            "fallback_network": "net-fallback",
        },
        "vmware.composite.cluster.patch": {"cluster": "domain-c1"},
    }[composite_op_id]


def _benign_responses_for(composite_op_id: str) -> dict[str, Any]:
    """Per-op spec-path reads that steer each composite to a no-work status.

    Composites whose first sub-op is a listing read unwrap ``value`` and
    expect a *list*; an empty *list* envelope lets the composite short-circuit
    to a benign no-work business status. ``vm.clone`` is fire-and-forget here
    (``wait_for_completion=False``): it reads the source VM then deploys, so it
    needs a deploy envelope carrying a task id to reach ``pending``.
    """
    empty: dict[str, Any] = {"value": []}
    per_composite: dict[str, dict[str, Any]] = {
        "vmware.composite.vm.create": {"/vcenter/folder": empty},
        "vmware.composite.vm.clone": {
            "/vcenter/vm-template/library-items?action=deploy": {"value": {"task": "task-benign"}},
        },
        "vmware.composite.vm.snapshot.revert": {"/vcenter/vm/vm-1/snapshot": empty},
        "vmware.composite.vm.migrate": {"/vcenter/cluster/domain-c1/drs/recommendations": empty},
        "vmware.composite.vm.power": {},
        "vmware.composite.vm.power.bulk": {"/vcenter/vm": empty},
        "vmware.composite.host.evacuate": {"/vcenter/vm": empty},
        "vmware.composite.host.detach_from_vds": {
            "/vcenter/network/distributed-portgroup": empty,
            "/vcenter/vm": empty,
        },
        "vmware.composite.cluster.patch": {"/vcenter/cluster/domain-c1/host": empty},
    }
    return per_composite[composite_op_id]


# ===========================================================================
# Guard: the write set is exactly the expected nine
# ===========================================================================


def test_write_composite_set_is_the_expected_nine() -> None:
    """Pins the op_id set so a renamed / dropped composite can't shrink coverage."""
    registrar_write_op_ids = {f"vmware.composite.{name}" for name in _WRITE_COMPOSITES.values()}
    assert set(_WRITE_COMPOSITES) == registrar_write_op_ids
    assert len(_WRITE_COMPOSITES) == 9


# ===========================================================================
# Fresh-boot: every composite executes through dispatch with ZERO ingested rows
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("composite_op_id", sorted(_WRITE_COMPOSITES))
async def test_write_composite_executes_through_dispatch_without_ingest(
    composite_op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Each composite runs to a benign business status on the direct session.

    No ingested ``endpoint_descriptor`` rows exist in the catalog here — only
    the 14 composite rows the registrar upserts. Reaching a business status
    (``created`` / ``no_recommendation`` / ``detached`` / ...) rather than a
    generic execution error proves every raw-REST sub-op resolved via the
    connector session, not a catalog lookup (the two-world / fresh-boot DoD).
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(_benign_responses_for(composite_op_id))
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval(set(_WRITE_COMPOSITES), recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=composite_op_id,
        target=_FakeVmwareTarget(),
        params=_benign_params_for(composite_op_id),
    )

    assert "composite_l2_missing" not in (result.error or ""), result.error
    assert result.status != "error", result.error
    assert result.status in {"ok", "pending"}, (result.status, result.error)


# ===========================================================================
# Sub-op sequence + rollback branch (through production dispatch)
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_create_happy_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: folder GET -> create POST -> NIC PATCH -> power POST."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/folder": {"value": [{"folder": "group-v1"}]},
            "/vcenter/vm": {"value": "vm-123"},
        }
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.create"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=_FakeVmwareTarget(),
        params={
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
            "nics": [{"network": "net-1"}],
            "power_on_after_create": True,
        },
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "created"
    assert result.result["vm_id"] == "vm-123"
    assert recorder.calls == [
        ("GET", "/vcenter/folder"),
        ("POST", "/vcenter/vm"),
        ("PATCH", "/vcenter/vm/vm-123/network"),
        ("POST", "/vcenter/vm/vm-123/power?action=start"),
    ]


@pytest.mark.asyncio
async def test_vm_create_rollback_on_nic_failure(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: a NIC-attach transport error rolls back via DELETE:/vcenter/vm/{vm}."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/folder": {"value": [{"folder": "group-v1"}]},
            "/vcenter/vm": {"value": "vm-123"},
        }
    )
    recorder.failures["/vcenter/vm/vm-123/network"] = "nic backend rejected the attach"
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.create"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=_FakeVmwareTarget(),
        params={
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
            "nics": [{"network": "net-1"}],
        },
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "rolled_back"
    assert result.result["failed_step"] == "nic_attach"
    assert result.result["vm_id"] is None
    assert recorder.calls == [
        ("GET", "/vcenter/folder"),
        ("POST", "/vcenter/vm"),
        ("PATCH", "/vcenter/vm/vm-123/network"),
        ("DELETE", "/vcenter/vm/vm-123"),
    ]


@pytest.mark.asyncio
async def test_vm_clone_pending_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.clone (fire-and-forget): source read -> deploy -> return task id."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "src"}},
            "/vcenter/vm-template/library-items?action=deploy": {"value": {"task": "task-9"}},
        }
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.clone"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.clone",
        target=_FakeVmwareTarget(),
        params={
            "source_vm": "vm-1",
            "target_name": "vm-clone",
            "library_item": "lib-1",
            "wait_for_completion": False,
        },
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "pending"
    assert result.result["task_id"] == "task-9"
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1"),
        ("POST", "/vcenter/vm-template/library-items?action=deploy"),
    ]


@pytest.mark.asyncio
async def test_vm_snapshot_revert_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.snapshot.revert: list -> match-by-name -> revert."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm/vm-1/snapshot"] = {
        "value": [{"name": "snap-a", "snapshot": "snap-moid-1"}]
    }
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.snapshot.revert"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.snapshot.revert",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "snapshot_name": "snap-a"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "reverted"
    assert result.result["snapshot_id"] == "snap-moid-1"
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1/snapshot"),
        ("POST", "/vcenter/vm/vm-1/snapshot/snap-moid-1?action=revert"),
    ]


@pytest.mark.asyncio
async def test_vm_migrate_drs_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.migrate: DRS recommendation -> relocate."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/cluster/domain-c1/drs/recommendations"] = {
        "value": [{"vm": "vm-1", "target_host": "host-target"}]
    }
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.migrate"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.migrate",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "cluster": "domain-c1"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "migrated"
    assert result.result["target_host"] == "host-target"
    assert recorder.calls == [
        ("GET", "/vcenter/cluster/domain-c1/drs/recommendations"),
        ("POST", "/vcenter/vm/vm-1?action=relocate"),
    ]


@pytest.mark.asyncio
async def test_vm_power_bulk_fan_out_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.power.bulk: list -> per-VM power action fan-out."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm"] = {"value": [{"vm": "vm-a"}, {"vm": "vm-b"}]}
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.power.bulk"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.power.bulk",
        target=_FakeVmwareTarget(),
        params={"action": "stop"},
    )

    assert result.status == "ok", result.error
    assert result.result["summary"] == {"ok": 2, "error": 0}
    assert recorder.calls == [
        ("GET", "/vcenter/vm"),
        ("POST", "/vcenter/vm/vm-a/power?action=stop"),
        ("POST", "/vcenter/vm/vm-b/power?action=stop"),
    ]


@pytest.mark.asyncio
async def test_host_evacuate_recursive_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """host.evacuate: list VMs -> recursive vm.migrate per VM -> maintenance enter.

    Exercises the only composite-to-composite recursion in the write set
    through production dispatch: the recursive ``vm.migrate`` (still routed
    via ``dispatch_child``, #2248) resolves the same connector instance and
    runs its DRS read + relocate write on the direct session before the host
    enters maintenance.
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/vm": {"value": [{"vm": "vm-a", "cluster": "domain-c1"}]},
            "/vcenter/cluster/domain-c1/drs/recommendations": {
                "value": [{"vm": "vm-a", "target_host": "host-target"}]
            },
        }
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval(
        {"vmware.composite.host.evacuate", "vmware.composite.vm.migrate"}, recorder
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.host.evacuate",
        target=_FakeVmwareTarget(),
        params={"host": "host-1"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "evacuated"
    assert result.result["maintenance_entered"] is True
    assert result.result["migrated_vms"] == ["vm-a"]
    assert recorder.calls == [
        ("GET", "/vcenter/vm"),
        ("GET", "/vcenter/cluster/domain-c1/drs/recommendations"),
        ("POST", "/vcenter/vm/vm-a?action=relocate"),
        ("PATCH", "/vcenter/host/host-1/maintenance?action=enter"),
    ]


@pytest.mark.asyncio
async def test_host_detach_from_vds_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """host.detach_from_vds: portgroups -> VMs -> per-VM NIC migrate -> DVS detach."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/network/distributed-portgroup": {"value": []},
            "/vcenter/vm": {"value": [{"vm": "vm-a"}]},
        }
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.host.detach_from_vds"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.host.detach_from_vds",
        target=_FakeVmwareTarget(),
        params={"host": "host-1", "dvs": "dvs-1", "fallback_network": "net-fallback"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "detached"
    assert result.result["vms_migrated"] == ["vm-a"]
    assert recorder.calls == [
        ("GET", "/vcenter/network/distributed-portgroup"),
        ("GET", "/vcenter/vm"),
        ("PATCH", "/vcenter/vm/vm-a/network"),
        ("POST", "/vcenter/network/dvs/dvs-1?action=remove_host"),
    ]


@pytest.mark.asyncio
async def test_cluster_patch_sequential_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """cluster.patch: list hosts -> per-host maintenance enter -> patch -> exit."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/cluster/domain-c1/host"] = {"value": [{"host": "host-1"}]}
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.cluster.patch"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.cluster.patch",
        target=_FakeVmwareTarget(),
        params={"cluster": "domain-c1"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "completed"
    assert result.result["patched_hosts"] == ["host-1"]
    assert recorder.calls == [
        ("GET", "/vcenter/cluster/domain-c1/host"),
        ("PATCH", "/vcenter/host/host-1/maintenance?action=enter"),
        ("POST", "/vcenter/host/host-1?action=patch"),
        ("PATCH", "/vcenter/host/host-1/maintenance?action=exit"),
    ]


# ===========================================================================
# Human approval-queue path (queue -> approve -> resume -> execute)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("composite_op_id", sorted(_WRITE_COMPOSITES))
async def test_write_composite_human_dispatch_parks_for_approval(
    composite_op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A USER principal dispatching a write composite is parked at the top level.

    Every write composite ships ``requires_approval=True``; G11.7-T1 (#1401)
    routes a human/service principal to the approval queue
    (``awaiting_approval``) at the top-level gate — before the handler (and
    thus any sub-op) runs. Proves the park half for all 8; the recorder stays
    empty because the composite never executed.
    """
    recorder = _RecordingVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=composite_op_id,
        target=_FakeVmwareTarget(),
        params=_benign_params_for(composite_op_id),
    )

    assert result.status == "awaiting_approval", result.error
    approval_request_id = UUID(result.extras["approval_request_id"])
    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.status == ApprovalRequestStatus.PENDING.value
    assert pending.op_id == composite_op_id
    # The composite itself never executed: no *write* hit the session. (The
    # four fan-out composites' park-time preview builders issue one read-only
    # listing GET to resolve the blast radius — that is expected, and now
    # works on a fresh boot via the direct session; only writes are barred.)
    assert all(verb == "GET" for verb, _ in recorder.calls)


@pytest.mark.asyncio
async def test_vm_create_full_queue_approve_resume_execute(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: full queued -> approve -> resume -> execute cycle.

    1. A USER principal dispatches the ``requires_approval=True`` composite ->
       parked at ``awaiting_approval`` at the top level; nothing runs.
    2. A distinct human reviewer approves the parked request.
    3. The ``_approved=True`` resume re-dispatch executes the composite — the
       top-level gate is bypassed (the approval *is* the authorization) and
       the per-sub-op governance seam auto-executes for the approved human, so
       the create chain runs on the direct session and returns
       ``status='created'``.
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/folder": {"value": [{"folder": "group-v1"}]},
            "/vcenter/vm": {"value": "vm-789"},
        }
    )
    await _bootstrap(recorder, stub_embedding_service)

    # Persist a real Target row so the resume path can re-hydrate it by id.
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name="prod-vcenter",
                product="vmware",
                host="vcenter.prod.invalid",
                aliases=[],
            )
        )
        await s.commit()

    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)
    target = _FakeVmwareTarget(target_id=target_id)
    params = {"folder_name": "prod", "name": "vm-approved", "guest_os": "UBUNTU_64"}

    # Step 1: human dispatch -> awaiting_approval; the op did not run.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.calls == [], "the composite must not execute before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
        assert pending is not None
        assert pending.target_id == target_id

    # Step 2: a distinct human reviewer approves the parked request.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume re-dispatch with the gate bypass -> the op executes.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "created"
    assert result2.result["vm_id"] == "vm-789"
    assert recorder.calls == [("GET", "/vcenter/folder"), ("POST", "/vcenter/vm")]
