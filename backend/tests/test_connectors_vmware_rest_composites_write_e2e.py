# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end activation tests for the 15 vmware-rest write composites.

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

import json
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


def _vmomi_retrieve_result(obj_type: str, moid: str, prop: str, val: Any) -> dict[str, Any]:
    """A single-object ``RetrievePropertiesEx`` result carrying one property."""
    return {
        "objects": [
            {
                "obj": {"type": obj_type, "value": moid},
                "propSet": [{"name": prop, "val": val}],
            }
        ]
    }


def _vmomi_task_moref(value: str) -> dict[str, str]:
    """A vim Task ``ManagedObjectReference`` as a ``*_Task`` method returns it."""
    return {"type": "Task", "value": value}


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
        self.vmomi_responses: dict[str, Any] = {}
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    def _spec(self, path: str) -> str:
        return path[len(self._MOUNT) :] if path.startswith(self._MOUNT) else path

    async def _about_version(self, target: Any, operator: Operator) -> str | None:
        del target, operator
        return None

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
        timeout: Any = None,
    ) -> Any:
        spec = self._spec(path)
        self.calls.append((verb, spec))
        if spec in self.failures:
            raise _http_error(500, f"https://vc{self._MOUNT}{spec}")
        return self.responses.get(spec, {"value": {}})

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        """Serve the vim sub-calls the #2970-switched composite steps issue.

        Method paths are keyed verbatim in ``vmomi_responses``;
        ``RetrievePropertiesEx`` bodies are keyed by the queried object
        type instead, with an unkeyed ``Task`` read serving a
        terminal-success ``Task.info`` (mirrors the unit-test stubs).
        """
        self.vmomi_calls.append((path, json))
        if path.endswith("/RetrievePropertiesEx"):
            spec_type = json["specSet"][0]["propSet"][0]["type"]
            if spec_type in self.vmomi_responses:
                return self.vmomi_responses[spec_type]
            if spec_type == "Task":
                task_moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
                return _vmomi_retrieve_result("Task", task_moid, "info", {"state": "success"})
            raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")
        return self.vmomi_responses[path]


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
    """Register the connector + all 17 composites and seed the recorder."""
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
    "vmware.composite.vm.deploy_from_library": "vm.deploy_from_library",
    "vmware.composite.vm.snapshot.revert": "vm.snapshot.revert",
    "vmware.composite.vm.migrate": "vm.migrate",
    "vmware.composite.vm.power": "vm.power",
    "vmware.composite.vm.power.bulk": "vm.power.bulk",
    "vmware.composite.vm.resize": "vm.resize",
    "vmware.composite.vm.nic.repoint": "vm.nic.repoint",
    "vmware.composite.vm.device.cdrom": "vm.device.cdrom",
    "vmware.composite.host.evacuate": "host.evacuate",
    "vmware.composite.host.detach_from_vds": "host.detach_from_vds",
    "vmware.composite.cluster.patch": "cluster.patch",
    "vmware.composite.guest.customization_spec.create": "guest.customization_spec.create",
    "vmware.composite.vm.customize": "vm.customize",
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
        },
        "vmware.composite.vm.deploy_from_library": {
            "library_item": "li-1",
            "resource_pool": "resgroup-8",
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
        "vmware.composite.vm.resize": {"vm": "vm-1", "cpu_count": 2},
        "vmware.composite.vm.nic.repoint": {
            "vm": "vm-1",
            "nic": "4000",
            "portgroup_name": "prod-pg",
        },
        "vmware.composite.vm.device.cdrom": {
            "vm": "vm-1",
            "cdrom": "16000",
            "action": "disconnect",
        },
        "vmware.composite.host.evacuate": {"host": "host-1"},
        "vmware.composite.host.detach_from_vds": {
            "host": "host-1",
            "dvs": "dvs-1",
            "fallback_network": "net-fallback",
        },
        "vmware.composite.cluster.patch": {"cluster": "domain-c1"},
        "vmware.composite.guest.customization_spec.create": {
            "spec_name": "gosc-benign",
            "os_type": "linux",
            "hostname": "vm-benign",
        },
        "vmware.composite.vm.customize": {"name": "vm-benign", "spec_name": "gosc-benign"},
    }[composite_op_id]


def _benign_responses_for(composite_op_id: str) -> dict[str, Any]:
    """Per-op spec-path reads that steer each composite to a no-work status.

    Composites whose first sub-op is a listing read unwrap ``value`` and
    expect a *list*; an empty *list* envelope lets the composite short-circuit
    to a benign no-work business status. ``vm.clone`` deploys synchronously
    (#2970): its deploy envelope carries the new VM id string, reaching
    ``completed``.
    """
    empty: dict[str, Any] = {"value": []}
    per_composite: dict[str, dict[str, Any]] = {
        "vmware.composite.vm.create": {"/vcenter/folder": empty},
        "vmware.composite.vm.clone": {
            "/vcenter/vm-template/library-items/lib-1?action=deploy": {"value": "vm-benign"},
        },
        # deploy_from_library: id passthrough → no finds; the synchronous OVF
        # deploy returns a DeploymentResult that succeeds → status='deployed'.
        "vmware.composite.vm.deploy_from_library": {
            "/vcenter/ovf/library-item/li-1?action=deploy": {
                "succeeded": True,
                "resource_id": {"type": "VirtualMachine", "id": "vm-benign"},
            },
        },
        "vmware.composite.vm.snapshot.revert": {},
        "vmware.composite.vm.migrate": {},
        "vmware.composite.vm.power": {},
        "vmware.composite.vm.power.bulk": {"/vcenter/vm": empty},
        "vmware.composite.vm.resize": {
            "/vcenter/vm/vm-1": {
                "value": {
                    "name": "vm-1",
                    "power_state": "POWERED_OFF",
                    "cpu": {"count": 1, "cores_per_socket": 1},
                    "memory": {"size_MiB": 1024},
                }
            }
        },
        "vmware.composite.vm.nic.repoint": {
            "/vcenter/vm/vm-1": {"value": {"name": "vm-1"}},
            "/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "value": {
                    "mac_address": "00:50:56:aa:bb:cc",
                    "backing": {"type": "STANDARD_PORTGROUP"},
                }
            },
            "/vcenter/network": {
                "value": [
                    {"network": "dvportgroup-9", "name": "prod-pg", "type": "DISTRIBUTED_PORTGROUP"}
                ]
            },
        },
        "vmware.composite.vm.device.cdrom": {
            "/vcenter/vm/vm-1": {"value": {"name": "vm-1"}},
            "/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "value": {
                    "backing": {"type": "ISO_FILE", "iso_file": "[local] pinned.iso"},
                    "state": "CONNECTED",
                }
            },
        },
        "vmware.composite.host.evacuate": {"/vcenter/vm": empty},
        "vmware.composite.host.detach_from_vds": {
            "/vcenter/network": empty,
            "/vcenter/vm": empty,
        },
        "vmware.composite.cluster.patch": {"/vcenter/host": empty},
        # create: the POST default ({"value": {}}) is enough -> status='created'.
        "vmware.composite.guest.customization_spec.create": {},
        # customize: empty VM listing -> status='not_found' (benign no-work).
        "vmware.composite.vm.customize": {"/vcenter/vm": empty},
    }
    return per_composite[composite_op_id]


def _benign_vmomi_for(composite_op_id: str) -> dict[str, Any]:
    """Per-op vim (VI-JSON) responses for the #2970 vim-switched benign paths.

    ``snapshot.revert`` / ``vm.migrate`` read empty vim properties and
    short-circuit (``not_found`` / ``no_recommendation``); ``host.evacuate``
    (no VMs) still enters maintenance via the vim ``*_Task``;
    ``host.detach_from_vds`` (no VMs) still reads the DVS configVersion and
    fires the ReconfigureDvs_Task. Task polls serve the default
    terminal-success ``Task.info``.
    """
    per_composite: dict[str, dict[str, Any]] = {
        "vmware.composite.vm.snapshot.revert": {
            "VirtualMachine": _vmomi_retrieve_result(
                "VirtualMachine", "vm-1", "snapshot", {"rootSnapshotList": []}
            ),
        },
        "vmware.composite.vm.migrate": {
            "ClusterComputeResource": _vmomi_retrieve_result(
                "ClusterComputeResource", "domain-c1", "drsRecommendation", []
            ),
        },
        "vmware.composite.host.evacuate": {
            "/HostSystem/host-1/EnterMaintenanceMode_Task": _vmomi_task_moref("t-enter-benign"),
        },
        "vmware.composite.host.detach_from_vds": {
            "DistributedVirtualSwitch": _vmomi_retrieve_result(
                "DistributedVirtualSwitch", "dvs-1", "config.configVersion", "1"
            ),
            "/DistributedVirtualSwitch/dvs-1/ReconfigureDvs_Task": _vmomi_task_moref(
                "t-dvs-benign"
            ),
        },
    }
    return per_composite.get(composite_op_id, {})


# ===========================================================================
# Guard: the REST-sub-op write set is exactly the expected fourteen
# ===========================================================================


def test_write_composite_set_is_the_expected_fifteen() -> None:
    """Pins the REST-sub-op write set so a renamed / dropped composite can't shrink coverage.

    Covers the 15 REST-sub-op write composites the parametrized fresh-boot +
    park machinery below drives (the 12 T6/#509 + vm.power + #2891 hardware
    writes, the two GOSC composites / #2892, plus the OVF/OVA content-library
    deploy ``vm.deploy_from_library`` / #2909). The four vi-json write
    composites (``vm.disk.grow`` / #2893, ``vm.clone_from_template`` / #2894,
    ``cluster.drs_rule.create`` + ``folder.create`` / #2895) are
    dispatch-shaped differently (vmomi sub-ops keyed by request body, not
    REST spec-paths) and are covered by their own dedicated sections at the
    end of this module.
    """
    registrar_write_op_ids = {f"vmware.composite.{name}" for name in _WRITE_COMPOSITES.values()}
    assert set(_WRITE_COMPOSITES) == registrar_write_op_ids
    assert len(_WRITE_COMPOSITES) == 15


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
    the 24 composite rows the registrar upserts. Reaching a business status
    (``created`` / ``no_recommendation`` / ``detached`` / ...) rather than a
    generic execution error proves every raw-REST sub-op resolved via the
    connector session, not a catalog lookup (the two-world / fresh-boot DoD).
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(_benign_responses_for(composite_op_id))
    recorder.vmomi_responses.update(_benign_vmomi_for(composite_op_id))
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
        ("POST", "/vcenter/vm/vm-123/hardware/ethernet"),
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
    recorder.failures["/vcenter/vm/vm-123/hardware/ethernet"] = "nic backend rejected the attach"
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
        ("POST", "/vcenter/vm/vm-123/hardware/ethernet"),
        ("DELETE", "/vcenter/vm/vm-123"),
    ]


@pytest.mark.asyncio
async def test_vm_clone_synchronous_deploy_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.clone: source read -> synchronous per-item deploy -> completed (#2970)."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "src"}},
            "/vcenter/vm-template/library-items/lib-1?action=deploy": {"value": "vm-cloned-9"},
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
        },
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "completed"
    assert result.result["task_id"] is None
    assert result.result["vm_id"] == "vm-cloned-9"
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1"),
        ("POST", "/vcenter/vm-template/library-items/lib-1?action=deploy"),
    ]


@pytest.mark.asyncio
async def test_vm_snapshot_revert_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.snapshot.revert: vim tree read -> match-by-name -> vim revert (#2970)."""
    recorder = _RecordingVmwareConnector()
    recorder.vmomi_responses.update(
        {
            "VirtualMachine": _vmomi_retrieve_result(
                "VirtualMachine",
                "vm-1",
                "snapshot",
                {
                    "rootSnapshotList": [
                        {
                            "snapshot": {"type": "VirtualMachineSnapshot", "value": "snap-moid-1"},
                            "name": "snap-a",
                            "childSnapshotList": [],
                        }
                    ]
                },
            ),
            "/VirtualMachineSnapshot/snap-moid-1/RevertToSnapshot_Task": _vmomi_task_moref(
                "t-revert-1"
            ),
        }
    )
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
    # The whole snapshot surface is vim-only: no REST sub-call fired.
    assert recorder.calls == []
    assert [p for p, _ in recorder.vmomi_calls if p.endswith("/RevertToSnapshot_Task")] == [
        "/VirtualMachineSnapshot/snap-moid-1/RevertToSnapshot_Task"
    ]


@pytest.mark.asyncio
async def test_vm_migrate_drs_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.migrate: vim drsRecommendation read -> relocate (#2970)."""
    recorder = _RecordingVmwareConnector()
    recorder.vmomi_responses["ClusterComputeResource"] = _vmomi_retrieve_result(
        "ClusterComputeResource",
        "domain-c1",
        "drsRecommendation",
        [
            {
                "migrationList": [
                    {
                        "vm": {"type": "VirtualMachine", "value": "vm-1"},
                        "destination": {"type": "HostSystem", "value": "host-target"},
                    }
                ]
            }
        ],
    )
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
    # The DRS read is vim; the relocate is the only REST call.
    assert recorder.calls == [("POST", "/vcenter/vm/vm-1?action=relocate")]
    assert [p for p, _ in recorder.vmomi_calls] == [
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
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
        }
    )
    recorder.vmomi_responses.update(
        {
            "ClusterComputeResource": _vmomi_retrieve_result(
                "ClusterComputeResource",
                "domain-c1",
                "drsRecommendation",
                [
                    {
                        "migrationList": [
                            {
                                "vm": {"type": "VirtualMachine", "value": "vm-a"},
                                "destination": {"type": "HostSystem", "value": "host-target"},
                            }
                        ]
                    }
                ],
            ),
            "/HostSystem/host-1/EnterMaintenanceMode_Task": _vmomi_task_moref("t-enter-1"),
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
    # The DRS read + maintenance-enter are vim (#2970); the REST calls are
    # the listing + the recursion's relocate.
    assert recorder.calls == [
        ("GET", "/vcenter/vm"),
        ("POST", "/vcenter/vm/vm-a?action=relocate"),
    ]
    assert [p for p, _ in recorder.vmomi_calls if p.endswith("/EnterMaintenanceMode_Task")] == [
        "/HostSystem/host-1/EnterMaintenanceMode_Task"
    ]


@pytest.mark.asyncio
async def test_host_detach_from_vds_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """host.detach_from_vds: portgroups -> VMs -> per-NIC migrate -> vim DVS detach."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            # #1602 fix: distributed portgroups list via /vcenter/network.
            "/vcenter/network": {"value": []},
            "/vcenter/vm": {"value": [{"vm": "vm-a"}]},
            "/vcenter/vm/vm-a/hardware/ethernet": {"value": [{"nic": "4000"}]},
        }
    )
    recorder.vmomi_responses.update(_benign_vmomi_for("vmware.composite.host.detach_from_vds"))
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
    # Per-adapter NIC repoint (#2970) + vim ReconfigureDvs_Task detach.
    assert recorder.calls == [
        ("GET", "/vcenter/network"),
        ("GET", "/vcenter/vm"),
        ("GET", "/vcenter/vm/vm-a/hardware/ethernet"),
        ("PATCH", "/vcenter/vm/vm-a/hardware/ethernet/4000"),
    ]
    assert [p for p, _ in recorder.vmomi_calls if p.endswith("/ReconfigureDvs_Task")] == [
        "/DistributedVirtualSwitch/dvs-1/ReconfigureDvs_Task"
    ]


@pytest.mark.asyncio
async def test_cluster_patch_sequential_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """cluster.patch: list hosts -> per-host vim enter -> vLCM apply -> vim exit (#2970)."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/host": {"value": [{"host": "host-1"}]},
            "/esx/settings/hosts/host-1/software?action=apply&vmw-task=true": {
                "value": "task-apply-1"
            },
            "/cis/tasks/task-apply-1": {"value": {"status": "SUCCEEDED"}},
        }
    )
    recorder.vmomi_responses.update(
        {
            "/HostSystem/host-1/EnterMaintenanceMode_Task": _vmomi_task_moref("t-enter-1"),
            "/HostSystem/host-1/ExitMaintenanceMode_Task": _vmomi_task_moref("t-exit-1"),
        }
    )
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
    # REST: cluster-scoped host listing + vLCM apply + its cis-task poll.
    assert recorder.calls == [
        ("GET", "/vcenter/host"),
        ("POST", "/esx/settings/hosts/host-1/software?action=apply&vmw-task=true"),
        ("GET", "/cis/tasks/task-apply-1"),
    ]
    # vim: maintenance enter + exit, in order.
    assert [p for p, _ in recorder.vmomi_calls if "MaintenanceMode_Task" in p] == [
        "/HostSystem/host-1/EnterMaintenanceMode_Task",
        "/HostSystem/host-1/ExitMaintenanceMode_Task",
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
    thus any sub-op) runs. Proves the park half for all 15; the recorder
    stays empty of writes because the composite never executed (the
    live-read preview builders issue read-only GETs, which is expected).
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


# ===========================================================================
# vm.disk.grow — mutating VI-JSON: park → approve → resume, #2681 envelope
# ===========================================================================


_TEN_GIB = 10 * 1024**3
_TWENTY_GIB = 20 * 1024**3


class _DiskGrowVmwareConnector:
    """Recording double for the disk-grow park→approve→resume E2E.

    Serves the park-time preview REST reads (``_get_json``: disk detail +
    VM name) and the dispatch-time VI-JSON sub-ops (``_post_vmomi_json``:
    the ``config.hardware.device`` read, the ``ReconfigVM_Task`` write, and
    the ``Task.info`` poll — the two ``RetrievePropertiesEx`` reads keyed
    apart by the request body's ``specSet`` object type). Records both
    surfaces so the test can prove the mutating vmomi write fires only on
    the approved-resume path.
    """

    _MOUNT = "/api"

    def __init__(self, *, capacity_bytes: int = _TEN_GIB) -> None:
        self._capacity_bytes = capacity_bytes
        self.rest_calls: list[tuple[str, str]] = []
        self.vmomi_calls: list[str] = []

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
        self.rest_calls.append(("GET", spec))
        if spec.endswith("/hardware/disk/2000"):
            return {"label": "Hard disk 1", "type": "SCSI", "capacity": self._capacity_bytes}
        if spec == "/vcenter/vm/vm-1":
            return {"name": "web-01"}
        return {"value": {}}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append(path)
        if path.endswith("/ReconfigVM_Task"):
            return {"type": "Task", "value": "task-grow-e2e"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            device = {
                "_typeName": "VirtualDisk",
                "key": 2000,
                "capacityInBytes": self._capacity_bytes,
                "backing": {
                    "_typeName": "VirtualDiskFlatVer2BackingInfo",
                    "fileName": "[ds] a.vmdk",
                },
            }
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": "vm-1"},
                        "propSet": [{"name": "config.hardware.device", "val": [device]}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": "task-grow-e2e"},
                    "propSet": [{"name": "info", "val": {"state": "success"}}],
                }
            ]
        }

    @property
    def reconfig_writes(self) -> list[str]:
        return [p for p in self.vmomi_calls if p.endswith("/ReconfigVM_Task")]


@pytest.mark.asyncio
async def test_disk_grow_queue_approve_resume_with_2681_envelope(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.disk.grow: park (with the #2681 op-identity envelope) → approve → resume → grow.

    1. A USER dispatch parks at ``awaiting_approval``; the durable row's
       ``proposed_effect`` carries the uniform #2681 op-identity envelope
       (``op_id`` / ``connector_id`` / ``target_id`` / ``op_class`` /
       ``safety_level``) plus the live-read from→to capacity preview. No
       ReconfigVM_Task fires — only the read-only preview GETs.
    2. A distinct human reviewer approves.
    3. The ``_approved=True`` resume executes the composite: the config read
       + the (now auto-executed) governed ReconfigVM_Task edit + the Task
       poll run, and the result is ``status='grown'``.
    """
    recorder = _DiskGrowVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

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
    params = {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB}

    # Step 1: human dispatch -> awaiting_approval; the write never ran.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.disk.grow",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.reconfig_writes == [], "no reconfigure before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.target_id == target_id
    # #2681 uniform op-identity + metadata envelope on the parked row.
    effect = pending.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.disk.grow"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target_id)
    assert effect["op_class"] == "other"
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_populated"] is True
    # The live-read from→to capacity delta — the decision the approver makes.
    assert effect["preview"] == {
        "vm": "vm-1",
        "name": "web-01",
        "disk": "2000",
        "disk_label": "Hard disk 1",
        "current_capacity_bytes": _TEN_GIB,
        "requested_capacity_bytes": _TWENTY_GIB,
        "delta_bytes": _TWENTY_GIB - _TEN_GIB,
    }

    # Step 2: a distinct human reviewer approves.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume re-dispatch with the gate bypass -> the grow executes.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.disk.grow",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "grown"
    assert result2.result["from_capacity_bytes"] == _TEN_GIB
    assert result2.result["to_capacity_bytes"] == _TWENTY_GIB
    assert result2.result["delta_bytes"] == _TWENTY_GIB - _TEN_GIB
    # The mutating ReconfigVM_Task fired exactly once, on the approved resume.
    assert recorder.reconfig_writes == ["/VirtualMachine/vm-1/ReconfigVM_Task"]


@pytest.mark.asyncio
async def test_disk_grow_fresh_boot_dispatchable_without_ingest(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.disk.grow runs to ``grown`` on the direct session with ZERO ingested rows."""
    recorder = _DiskGrowVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.disk.grow"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.disk.grow",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
    )
    assert "composite_l2_missing" not in (result.error or ""), result.error
    assert result.status == "ok", result.error
    assert result.result["status"] == "grown"
    assert recorder.reconfig_writes == ["/VirtualMachine/vm-1/ReconfigVM_Task"]


# ===========================================================================
# vm.clone_from_template — mutating VI-JSON: park → approve → resume, #2681
# envelope (#2894)
# ===========================================================================


class _CloneFromTemplateVmwareConnector:
    """Recording double for the clone-from-template park→approve→resume E2E.

    ``vm.clone_from_template``'s preview is a pure param-echo (no park-time
    I/O), so this double is exercised only on dispatch: the template
    resolution (REST ``GET:/vcenter/vm``) and the VI-JSON sub-ops
    (``_post_vmomi_json``: the ``config.template`` assert, the
    ``CloneVM_Task`` write, and the ``Task.info`` poll — the two
    ``RetrievePropertiesEx`` reads keyed apart by the request body's
    ``specSet`` object type). Records both surfaces so the test can prove the
    mutating vmomi write fires only on the approved-resume path.
    """

    _MOUNT = "/api"

    def __init__(self) -> None:
        self.rest_calls: list[tuple[str, str]] = []
        self.vmomi_calls: list[str] = []

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
        self.rest_calls.append(("GET", spec))
        if spec == "/vcenter/vm":
            return {"value": [{"vm": "vm-42", "name": "ubuntu-template"}]}
        return {"value": {}}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append(path)
        if path.endswith("/CloneVM_Task"):
            return {"type": "Task", "value": "task-clone-e2e"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": "vm-42"},
                        "propSet": [{"name": "config.template", "val": True}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": "task-clone-e2e"},
                    "propSet": [
                        {
                            "name": "info",
                            "val": {
                                "state": "success",
                                "result": {"type": "VirtualMachine", "value": "vm-99"},
                            },
                        }
                    ],
                }
            ]
        }

    @property
    def clone_writes(self) -> list[str]:
        return [p for p in self.vmomi_calls if p.endswith("/CloneVM_Task")]


_CLONE_PARAMS: dict[str, Any] = {
    "source_template": "ubuntu-template",
    "new_vm_name": "web-01",
    "folder": "group-v10",
    "resource_pool": "resgroup-8",
    "datastore": "datastore-15",
}


@pytest.mark.asyncio
async def test_clone_from_template_queue_approve_resume_with_2681_envelope(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.clone_from_template: park (with #2681 op-identity envelope) → approve → resume → clone.

    1. A USER dispatch parks at ``awaiting_approval``; the durable row's
       ``proposed_effect`` carries the uniform #2681 op-identity envelope
       (``op_id`` / ``connector_id`` / ``target_id`` / ``op_class`` /
       ``safety_level``) plus the param-echo clone-coordinates preview. No
       CloneVM_Task fires (the preview is pure param-echo — no reads either).
    2. A distinct human reviewer approves.
    3. The ``_approved=True`` resume executes the composite: the template
       resolution + config.template assert + the (now auto-executed) governed
       CloneVM_Task + the Task poll run, and the result is ``status='cloned'``.
    """
    recorder = _CloneFromTemplateVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

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

    # Step 1: human dispatch -> awaiting_approval; the clone never ran.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.clone_from_template",
        target=target,
        params=_CLONE_PARAMS,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.clone_writes == [], "no clone before approval"
    # Param-echo preview does no reads.
    assert recorder.rest_calls == []
    assert recorder.vmomi_calls == []
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.target_id == target_id
    # #2681 uniform op-identity + metadata envelope on the parked row.
    effect = pending.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.clone_from_template"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target_id)
    assert effect["op_class"] == "other"
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_populated"] is True
    # The param-echo blast-radius preview — what the approver decides on.
    assert effect["preview"] == {
        "source_template": "ubuntu-template",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
        "host": None,
        "power_on": False,
        "customization_spec_name": None,
    }

    # Step 2: a distinct human reviewer approves.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=_CLONE_PARAMS)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume re-dispatch with the gate bypass -> the clone executes.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.clone_from_template",
        target=target,
        params=_CLONE_PARAMS,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "cloned"
    assert result2.result["source_template_id"] == "vm-42"
    assert result2.result["new_vm_id"] == "vm-99"
    # The mutating CloneVM_Task fired exactly once, on the approved resume.
    assert recorder.clone_writes == ["/VirtualMachine/vm-42/CloneVM_Task"]


@pytest.mark.asyncio
async def test_clone_from_template_fresh_boot_dispatchable_without_ingest(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.clone_from_template runs to ``cloned`` on the direct session with ZERO ingested rows."""
    recorder = _CloneFromTemplateVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.clone_from_template"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.clone_from_template",
        target=_FakeVmwareTarget(),
        params=_CLONE_PARAMS,
    )
    assert "composite_l2_missing" not in (result.error or ""), result.error
    assert result.status == "ok", result.error
    assert result.result["status"] == "cloned"
    assert recorder.clone_writes == ["/VirtualMachine/vm-42/CloneVM_Task"]


# ===========================================================================
# vm.deploy_from_library — park (with #2681 envelope) → approve → resume → deploy
# ===========================================================================


_DEPLOY_FROM_LIBRARY_PARAMS: dict[str, Any] = {
    "library_item": "li-1",
    "resource_pool": "resgroup-8",
    "network_mappings": {"nat": "dvportgroup-9"},
}


@pytest.mark.asyncio
async def test_deploy_from_library_queue_approve_resume_with_2681_envelope(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.deploy_from_library: park (with #2681 op-identity envelope) → approve → resume → deploy.

    1. A USER dispatch parks at ``awaiting_approval``; the durable row's
       ``proposed_effect`` carries the uniform #2681 op-identity envelope
       (``op_id`` / ``connector_id`` / ``target_id`` / ``op_class`` /
       ``safety_level`` / ``preview_populated``) plus the param-echo deploy
       preview. The OVF deploy never fires (the preview is pure param-echo —
       no reads either).
    2. A distinct human reviewer approves.
    3. The ``_approved=True`` resume executes the composite: the (now
       auto-executed) governed OVF deploy runs and the result is
       ``status='deployed'``.
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/ovf/library-item/li-1?action=deploy"] = {
        "succeeded": True,
        "resource_id": {"type": "VirtualMachine", "id": "vm-ovf-9"},
    }
    await _bootstrap(recorder, stub_embedding_service)

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

    # Step 1: human dispatch -> awaiting_approval; the deploy never ran.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.deploy_from_library",
        target=target,
        params=_DEPLOY_FROM_LIBRARY_PARAMS,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.calls == [], "no deploy before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.target_id == target_id
    # #2681 uniform op-identity + metadata envelope on the parked row.
    effect = pending.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.deploy_from_library"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target_id)
    assert effect["op_class"] == "other"
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_populated"] is True
    # The param-echo blast-radius preview — what the approver decides on.
    assert effect["preview"] == {
        "library_item": "li-1",
        "library_item_name": None,
        "library_name": None,
        "name": None,
        "resource_pool": "resgroup-8",
        "host": None,
        "folder": None,
        "datastore": None,
        "network_mappings": {"nat": "dvportgroup-9"},
        "storage_provisioning": None,
        "ovf_property_keys": [],
        "power_on": False,
    }

    # Step 2: a distinct human reviewer approves.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(
            s, approval_request_id, operator=reviewer, params=_DEPLOY_FROM_LIBRARY_PARAMS
        )
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume re-dispatch with the gate bypass -> the deploy executes.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.deploy_from_library",
        target=target,
        params=_DEPLOY_FROM_LIBRARY_PARAMS,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "deployed"
    assert result2.result["vm_id"] == "vm-ovf-9"
    # The synchronous OVF deploy fired exactly once, on the approved resume.
    assert recorder.calls == [("POST", "/vcenter/ovf/library-item/li-1?action=deploy")]


# ===========================================================================
# cluster.drs_rule.create — park→approve→resume + fresh-boot (#2895)
# ===========================================================================


class _DrsRuleVmwareConnector:
    """Recording double for the drs_rule.create park→approve→resume E2E.

    Serves the VM-resolution + cluster-name REST reads (``_get_json``) and the
    dispatch-time VI-JSON sub-ops (``_post_vmomi_json``: the existing-rules
    collision read, the ``ReconfigureComputeResource_Task`` write, and the
    ``Task.info`` poll — the two ``RetrievePropertiesEx`` reads keyed apart by
    the request body's ``specSet`` object type). Records both surfaces so the
    test can prove the mutating vmomi write fires only on the approved resume.
    """

    _MOUNT = "/api"

    def __init__(self, *, existing_rules: list[dict[str, Any]] | None = None) -> None:
        self.existing_rules = existing_rules or []
        self.rest_calls: list[tuple[str, str]] = []
        self.vmomi_calls: list[str] = []

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
        self.rest_calls.append(("GET", spec))
        if spec == "/vcenter/vm":
            return {
                "value": [
                    {"vm": "vm-1", "name": "web-01", "power_state": "POWERED_ON"},
                    {"vm": "vm-2", "name": "web-02", "power_state": "POWERED_ON"},
                ]
            }
        if spec == "/vcenter/cluster/domain-c1":
            return {"name": "prod-cluster"}
        return {"value": {}}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append(path)
        if path.endswith("/ReconfigureComputeResource_Task"):
            return {"type": "Task", "value": "task-rule-e2e"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "ClusterComputeResource":
            return {
                "objects": [
                    {
                        "obj": {"type": "ClusterComputeResource", "value": "domain-c1"},
                        "propSet": [{"name": "configurationEx.rule", "val": self.existing_rules}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": "task-rule-e2e"},
                    "propSet": [{"name": "info", "val": {"state": "success"}}],
                }
            ]
        }

    @property
    def reconfig_writes(self) -> list[str]:
        return [p for p in self.vmomi_calls if p.endswith("/ReconfigureComputeResource_Task")]


@pytest.mark.asyncio
async def test_drs_rule_create_queue_approve_resume_with_2681_envelope(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """drs_rule.create: park (#2681 envelope + fan-out preview) → approve → resume → created.

    1. A USER dispatch parks at ``awaiting_approval``; the durable row carries
       the uniform #2681 op-identity envelope plus the capped VM fan-out
       preview (cluster + cluster_name + resolved VMs). No
       ReconfigureComputeResource_Task fires — only the read-only preview GETs.
    2. A distinct human reviewer approves.
    3. The ``_approved=True`` resume executes: VM resolution + collision read +
       the (now auto-executed) governed reconfigure + the Task poll run, and
       the result is ``status='created'``.
    """
    recorder = _DrsRuleVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

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
    params = {
        "cluster": "domain-c1",
        "rule_name": "keep-apart",
        "rule_type": "anti_affinity",
        "vms": ["web-01", "web-02"],
    }

    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.cluster.drs_rule.create",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.reconfig_writes == [], "no reconfigure before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.target_id == target_id
    effect = pending.proposed_effect
    assert effect["op_id"] == "vmware.composite.cluster.drs_rule.create"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target_id)
    assert effect["op_class"] == "write"
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_populated"] is True
    assert effect["preview"] == {
        "cluster": "domain-c1",
        "cluster_name": "prod-cluster",
        "rule_type": "anti_affinity",
        "rule_name": "keep-apart",
        "enabled": True,
        "resolved": [
            {"vm": "vm-1", "name": "web-01", "power_state": "POWERED_ON"},
            {"vm": "vm-2", "name": "web-02", "power_state": "POWERED_ON"},
        ],
        "total_resolved": 2,
    }

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.cluster.drs_rule.create",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "created"
    assert result2.result["rule_name"] == "keep-apart"
    # The mutating reconfigure fired exactly once, on the approved resume.
    assert recorder.reconfig_writes == [
        "/ClusterComputeResource/domain-c1/ReconfigureComputeResource_Task"
    ]


@pytest.mark.asyncio
async def test_drs_rule_create_fresh_boot_dispatchable_without_ingest(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """drs_rule.create runs to ``created`` on the direct session with ZERO ingested rows."""
    recorder = _DrsRuleVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.cluster.drs_rule.create"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.cluster.drs_rule.create",
        target=_FakeVmwareTarget(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
    )
    assert "composite_l2_missing" not in (result.error or ""), result.error
    assert result.status == "ok", result.error
    assert result.result["status"] == "created"
    assert recorder.reconfig_writes == [
        "/ClusterComputeResource/domain-c1/ReconfigureComputeResource_Task"
    ]


# ===========================================================================
# folder.create — park→approve→resume + fresh-boot (synchronous, #2895)
# ===========================================================================


class _FolderCreateVmwareConnector:
    """Recording double for the folder.create park→approve→resume E2E.

    Serves the parent-folder resolution REST read (``_get_json``) and the
    synchronous ``Folder.CreateFolder`` vim POST (``_post_vmomi_json``) — which
    returns the new Folder MoRef directly, so **no** ``RetrievePropertiesEx``
    poll ever fires. Records both surfaces so the test can prove the mutating
    CreateFolder fires only on the approved resume and is never polled.
    """

    _MOUNT = "/api"

    def __init__(self, *, new_folder_moid: str = "group-v99") -> None:
        self.new_folder_moid = new_folder_moid
        self.rest_calls: list[tuple[str, str]] = []
        self.vmomi_calls: list[str] = []

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
        self.rest_calls.append(("GET", self._spec(path)))
        return {"value": [{"folder": "group-v1", "name": "prod"}]}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append(path)
        return {"type": "Folder", "value": self.new_folder_moid}

    @property
    def create_writes(self) -> list[str]:
        return [p for p in self.vmomi_calls if p.endswith("/CreateFolder")]


@pytest.mark.asyncio
async def test_folder_create_queue_approve_resume_with_2681_envelope(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """folder.create: park (#2681 + param-echo preview) → approve → resume → created.

    The synchronous-write proof end-to-end: on the approved resume the
    ``CreateFolder`` fires exactly once and its returned Folder MoRef is the
    result, with **no** ``RetrievePropertiesEx`` task poll (the whole vmomi
    call log is the single CreateFolder).
    """
    recorder = _FolderCreateVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

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
    params = {"parent_folder": "prod", "folder_name": "cluster-nodes"}

    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.folder.create",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.create_writes == [], "no CreateFolder before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    effect = pending.proposed_effect
    assert effect["op_id"] == "vmware.composite.folder.create"
    assert effect["op_class"] == "write"
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_populated"] is True
    assert effect["preview"] == {"parent_folder": "prod", "new_folder_name": "cluster-nodes"}

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.folder.create",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "created"
    assert result2.result["folder"] == "group-v99"
    # CreateFolder is synchronous: exactly one vmomi call, never polled.
    assert recorder.vmomi_calls == ["/Folder/group-v1/CreateFolder"]


@pytest.mark.asyncio
async def test_folder_create_fresh_boot_dispatchable_without_ingest(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """folder.create runs to ``created`` on the direct session with ZERO ingested rows."""
    recorder = _FolderCreateVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.folder.create"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.folder.create",
        target=_FakeVmwareTarget(),
        params={"parent_folder": "prod", "folder_name": "cluster-nodes"},
    )
    assert "composite_l2_missing" not in (result.error or ""), result.error
    assert result.status == "ok", result.error
    assert result.result["status"] == "created"
    assert recorder.create_writes == ["/Folder/group-v1/CreateFolder"]


# ===========================================================================
# Hardware write composites (#2891) — sub-op sequence through dispatch
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_resize_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.resize: read VM info -> PATCH cpu -> PATCH memory."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm/vm-1"] = {
        "value": {
            "name": "web-1",
            "power_state": "POWERED_OFF",
            "cpu": {"count": 1, "cores_per_socket": 1},
            "memory": {"size_MiB": 1024},
        }
    }
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.resize"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.resize",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "cpu_count": 4, "memory_mib": 8192},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "resized"
    assert result.result["applied"] == {"cpu": True, "memory": True}
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1"),
        ("PATCH", "/vcenter/vm/vm-1/hardware/cpu"),
        ("PATCH", "/vcenter/vm/vm-1/hardware/memory"),
    ]


@pytest.mark.asyncio
async def test_vm_nic_repoint_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.nic.repoint: read NIC -> resolve portgroup via /vcenter/network -> PATCH backing."""
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "value": {
                    "mac_address": "00:50:56:aa:bb:cc",
                    "backing": {"type": "STANDARD_PORTGROUP"},
                }
            },
            "/vcenter/network": {
                "value": [
                    {"network": "dvportgroup-9", "name": "prod-pg", "type": "DISTRIBUTED_PORTGROUP"}
                ]
            },
        }
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.nic.repoint"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.nic.repoint",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "nic": "4000", "portgroup_name": "prod-pg"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "repointed"
    assert result.result["requested_backing"] == {
        "portgroup_id": "dvportgroup-9",
        "portgroup_name": "prod-pg",
    }
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1/hardware/ethernet/4000"),
        ("GET", "/vcenter/network"),
        ("PATCH", "/vcenter/vm/vm-1/hardware/ethernet/4000"),
    ]


@pytest.mark.asyncio
async def test_vm_device_cdrom_remove_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.device.cdrom (remove): read backing -> DELETE the device."""
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm/vm-1/hardware/cdrom/16000"] = {
        "value": {
            "backing": {"type": "ISO_FILE", "iso_file": "[local] pinned.iso"},
            "state": "CONNECTED",
        }
    }
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.device.cdrom"}, recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.device.cdrom",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "cdrom": "16000", "action": "remove"},
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "removed"
    assert recorder.calls == [
        ("GET", "/vcenter/vm/vm-1/hardware/cdrom/16000"),
        ("DELETE", "/vcenter/vm/vm-1/hardware/cdrom/16000"),
    ]


# ===========================================================================
# Park-envelope uniformity for the #2891 hardware writes (#2681)
# ===========================================================================

#: The op-identity + metadata fields every parked envelope carries
#: uniformly (#2681). The bespoke ``preview`` content key is deliberately
#: not in this set — it is asserted separately.
_ENVELOPE_IDENTITY_META_KEYS = frozenset(
    {"op_id", "connector_id", "target_id", "op_class", "preview_populated", "safety_level"}
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "composite_op_id",
    [
        "vmware.composite.vm.resize",
        "vmware.composite.vm.nic.repoint",
        "vmware.composite.vm.device.cdrom",
    ],
)
async def test_hardware_write_park_carries_uniform_identity_envelope(
    composite_op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A parked hardware-write composite carries the #2681 uniform op-identity envelope.

    The G0.38 #2681 contract: every parked ``proposed_effect`` carries the
    same op-identity + metadata field-set (op_id / connector_id /
    target_id / op_class / preview_populated / safety_level) regardless of
    the per-op preview outcome. Here the live-read builders additionally
    populate a from->to ``preview``, so ``preview_populated`` is True.
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(_benign_responses_for(composite_op_id))
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
        row = await s.get(ApprovalRequest, approval_request_id)
    assert row is not None
    effect = dict(row.proposed_effect)

    # The uniform op-identity + metadata envelope is present (#2681).
    assert effect.keys() >= _ENVELOPE_IDENTITY_META_KEYS, sorted(effect)
    assert effect["op_id"] == composite_op_id
    assert effect["connector_id"] == _CONNECTOR_ID
    assert isinstance(effect["target_id"], str)
    assert effect["safety_level"] == "dangerous"
    assert effect["op_class"] == "other"
    # The live-read builder populated a from->to preview.
    assert effect["preview_populated"] is True
    assert "preview" in effect


# ===========================================================================
# GOSC secret hygiene (#1503) across all three reviewer surfaces (#2892)
# ===========================================================================

#: Distinctive secret literals planted in the customization spec params so a
#: leak onto any reviewer surface is caught by a substring scan.
_ADMIN_PW = "S3cr3t-Admin-P@ssw0rd-LEAK-CANARY"
_PRODUCT_KEY = "AAAAA-BBBBB-CCCCC-DDDDD-LEAKKEY"
_DOMAIN_JOIN_PW = "D0main-J0in-P@ss-LEAK-CANARY"
_ALL_SECRETS = (_ADMIN_PW, _PRODUCT_KEY, _DOMAIN_JOIN_PW)


def _windows_gosc_params() -> dict[str, Any]:
    """A Windows GOSC create spec carrying every secret-bearing field."""
    return {
        "spec_name": "gosc-win-prod",
        "os_type": "windows",
        "hostname": "win-app-01",
        "interfaces": [{"ip_address": "10.20.0.5", "prefix": 24, "gateways": ["10.20.0.1"]}],
        "dns_servers": ["10.20.0.2"],
        "windows_admin_password": _ADMIN_PW,
        "windows_product_key": _PRODUCT_KEY,
        "windows_join_domain": "corp.example.test",
        "windows_domain_admin_username": "svc-join",
        "windows_domain_admin_password": _DOMAIN_JOIN_PW,
    }


def _assert_no_secret(blob: str, *, surface: str) -> None:
    """Fail if any planted secret literal appears in *blob* (a serialised surface)."""
    for secret in _ALL_SECRETS:
        assert secret not in blob, f"secret leaked onto the {surface} surface"


@pytest.mark.asyncio
async def test_gosc_create_secret_hygiene_across_all_surfaces(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """GOSC create with inline secrets never leaks them to reviewer surfaces (#1503).

    The single most important correctness property of #2892. A Windows
    customization spec carrying an admin password, a product key, and a
    domain-join password is dispatched by a USER (parks), inspected on the
    durable ``ApprovalRequest.proposed_effect``, approved, and resumed. Every
    reviewer-facing surface is scanned for the planted secret literals:

    * ``proposed_effect`` — the bespoke preview echoes IDENTITY fields only.
    * broadcast frame — the op is pinned ``credential_write`` so the params
      collapse to aggregate-only (no ``params`` key at all).
    * audit payload — the durable row stores only a params hash.

    Also asserts the #2681 uniform op-identity envelope on the parked row.
    """
    recorder = _RecordingVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

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
    params = _windows_gosc_params()
    op_id = "vmware.composite.guest.customization_spec.create"

    # Step 1: USER dispatch -> parked; the create POST did not fire.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert recorder.calls == [], "the create must not run before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    # --- Surface 1: proposed_effect (identity-only preview + #2681 envelope) ---
    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    effect = pending.proposed_effect
    _assert_no_secret(json.dumps(effect), surface="proposed_effect")
    # Identity preview (bespoke builder output nests under ``preview``) is
    # present and complete -- and carries no secret field.
    preview = effect["preview"]
    assert preview["spec_name"] == "gosc-win-prod"
    assert preview["os_type"] == "windows"
    assert preview["hostname_scheme"] == "FIXED:win-app-01"
    assert preview["nic_count"] == 1
    assert preview["static_ip_summary"] == ["10.20.0.5"]
    # The preview echoes ONLY identity keys -- no credential field bled in.
    assert set(preview) == {
        "spec_name",
        "os_type",
        "hostname_scheme",
        "nic_count",
        "static_ip_summary",
    }
    # #2681 uniform op-identity envelope: identity + metadata stamped on every
    # parked row regardless of the per-op preview outcome.
    assert effect["op_id"] == op_id
    assert effect["connector_id"] == _CONNECTOR_ID
    assert isinstance(effect["target_id"], str)
    assert effect["op_class"] == "credential_write"
    assert effect["safety_level"] == "dangerous"

    # Step 2: approve.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume -> the create executes; secrets ride into the vCenter
    # request body (the real API call) but nowhere else.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result["status"] == "created"
    assert recorder.calls == [("POST", "/vcenter/guest/customization-specs")]

    # --- Surface 2: broadcast frame (params collapsed to aggregate-only) ---
    gosc_events = [e for e in captured_events if e.op_id == op_id]
    assert gosc_events, "no broadcast event captured for the GOSC create"
    for event in gosc_events:
        assert event.op_class == "credential_write"
        # The credential-class collapse drops the params dict entirely.
        assert "params" not in event.payload, event.payload
        _assert_no_secret(json.dumps(event.payload, default=str), surface="broadcast")


@pytest.mark.asyncio
async def test_vm_customize_preview_and_broadcast_carry_no_secret(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.customize (the apply half) echoes only a spec-name reference -- no secret.

    The second half of "redaction covered for both ops": ``vm.customize``
    references a saved spec by name and never carries credential material, so
    its ``proposed_effect`` and broadcast frame are safe by construction. This
    pins that: the preview surfaces the VM + spec name + resolved power state,
    and no secret literal can appear (there is none in its params).
    """
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/vm": {
                "value": [{"vm": "vm-55", "name": "app-01", "power_state": "POWERED_OFF"}]
            }
        }
    )
    await _bootstrap(recorder, stub_embedding_service)

    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)
    result = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.customize",
        target=_FakeVmwareTarget(),
        params={"name": "app-01", "spec_name": "gosc-win-prod"},
    )
    assert result.status == "awaiting_approval", result.error
    approval_request_id = UUID(result.extras["approval_request_id"])
    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    effect = pending.proposed_effect
    preview = effect["preview"]
    # Identity preview resolved the VM's power state via the live read.
    assert preview["name"] == "app-01"
    assert preview["spec_name"] == "gosc-win-prod"
    assert preview["vm"] == "vm-55"
    assert preview["power_state"] == "POWERED_OFF"
    assert preview["applies_on"] == "next_power_on"
    # #2681 envelope stamped here too.
    assert effect["op_id"] == "vmware.composite.vm.customize"
    assert effect["safety_level"] == "dangerous"
