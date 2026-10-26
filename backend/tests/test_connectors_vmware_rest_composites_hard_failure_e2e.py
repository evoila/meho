# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Composite hard failures are recorded as errors, not ``ok`` (#3813).

#3812 taught the dispatcher to treat a composite envelope that carries an
``error``-severity ``issues[]`` entry as a failed call. Many write composites
signalled their hard failures (a task timeout, a rollback, a partial apply, a
failed sub-step) with only a bare ``status`` value, so those failed writes
still came back ``ok`` and audited ``result_status="ok"``.

Each case below drives one composite into its hard-failure branch through the
production :func:`~meho_backplane.operations.dispatch` entry point and checks
that the caller gets ``status="error"`` with the envelope's ``status`` as the
``error_code``, and that the audit row says ``error``. A second group checks
that soft refusals and idempotency guards still return ``ok``.

The two destructive composites must go through preview, approval and resume:
``vm.destroy`` is covered in ``test_connectors_vmware_rest_vm_destroy.py`` and
``storage_policy.delete`` at the end of this module.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest.composites import _guest, _write
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _CONNECTOR_ID,
    _TENANT_ID,
    _TWENTY_GIB,
    _bootstrap,
    _clear_requires_approval,
    _DiskGrowVmwareConnector,
    _DrsRuleVmwareConnector,
    _FakeVmwareTarget,
    _make_operator,
    _RecordingVmwareConnector,
    _vm_listing_row,
    _vmomi_cluster_drs,
    _vmomi_host_parent,
    _vmomi_retrieve_result,
    _vmomi_task_moref,
)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_vmomi(recorder: Any, override: Callable[[str, Any], Any | None]) -> None:
    """Route *recorder*'s vim calls through *override* first.

    *override* returns a response, or ``None`` to fall through to the
    recorder's own canned answer.
    """
    inner = recorder._post_vmomi_json

    async def _post(target: Any, path: str, *, operator: Any, json: Any = None) -> Any:
        answer = override(path, json)
        if answer is not None:
            return answer
        return await inner(target, path, operator=operator, json=json)

    recorder._post_vmomi_json = _post


def _task_never_finishes(recorder: Any) -> None:
    """Make every ``Task.info`` read on *recorder* answer ``running``.

    Paired with a zeroed module timeout, the vim task poll gives up after one
    read and the composite takes its ``timeout`` branch.
    """

    def _running(path: str, json: Any) -> Any | None:
        if not path.endswith("/RetrievePropertiesEx"):
            return None
        if json["specSet"][0]["propSet"][0]["type"] != "Task":
            return None
        task_moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
        return _vmomi_retrieve_result("Task", task_moid, "info", {"state": "running"})

    _wrap_vmomi(recorder, _running)


def _portgroup_pre_read() -> dict[str, Any]:
    """A portgroup ``config.configVersion`` + ``config.defaultPortConfig`` read."""
    return {
        "objects": [
            {
                "obj": {"type": "DistributedVirtualPortgroup", "value": "dvportgroup-1"},
                "propSet": [
                    {"name": "config.configVersion", "val": "3"},
                    {"name": "config.defaultPortConfig", "val": {"vlan": None}},
                ],
            }
        ]
    }


async def _audit_statuses(op_id: str) -> list[str | None]:
    """The ``result_status`` of every audit row written for *op_id*."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row.payload.get("result_status") for row in rows]


async def _assert_dispatch_error(result: OperationResult, op_id: str, error_code: str) -> None:
    """The call failed with *error_code* and the audit log agrees."""
    assert result.status == "error", (result.status, result.error, result.result)
    assert result.extras["error_code"] == error_code
    assert isinstance(result.result, dict)
    assert any(i["severity"] == "error" for i in result.result["issues"])
    statuses = await _audit_statuses(op_id)
    assert "error" in statuses, statuses
    assert "ok" not in statuses, statuses


async def _dispatch_direct(
    recorder: Any, stub_embedding_service: AsyncMock, op_id: str, params: dict[str, Any]
) -> OperationResult:
    """Register the composites and dispatch *op_id* with its top-level approval off."""
    await _bootstrap(recorder, stub_embedding_service)
    await _clear_requires_approval({op_id}, recorder)
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_FakeVmwareTarget(),
        params=params,
    )


# ---------------------------------------------------------------------------
# Hard-failure cases: (op_id, params, setup, expected error_code)
# ---------------------------------------------------------------------------

Setup = Callable[[pytest.MonkeyPatch], Any]


def _vm_create_rolled_back(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/folder"] = {"value": [{"folder": "group-v1"}]}
    recorder.failures["/vcenter/vm"] = "500"
    return recorder


def _vm_resize_reads(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm/vm-1"] = {
        "value": {
            "name": "vm-1",
            "power_state": "POWERED_OFF",
            "cpu": {"count": 1, "cores_per_socket": 1},
            "memory": {"size_MiB": 1024},
        }
    }
    return recorder


def _vm_resize_partial(mp: pytest.MonkeyPatch) -> Any:
    recorder = _vm_resize_reads(mp)
    recorder.failures["/vcenter/vm/vm-1/hardware/memory"] = "500"
    return recorder


def _vm_disk_grow_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_DISK_GROW_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _DiskGrowVmwareConnector()
    _task_never_finishes(recorder)
    return recorder


def _vm_snapshot_revert_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_SNAPSHOT_REVERT_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _RecordingVmwareConnector()
    tree = {
        "rootSnapshotList": [
            {
                "name": "snap-a",
                "snapshot": {"type": "VirtualMachineSnapshot", "value": "snapshot-1"},
                "childSnapshotList": [],
            }
        ]
    }
    recorder.vmomi_responses.update(
        {
            "VirtualMachine": _vmomi_retrieve_result("VirtualMachine", "vm-1", "snapshot", tree),
            "/VirtualMachineSnapshot/snapshot-1/RevertToSnapshot_Task": _vmomi_task_moref("t-1"),
        }
    )
    _task_never_finishes(recorder)
    return recorder


def _vm_power_error(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.failures["/vcenter/vm/vm-1/power?action=start"] = "500"
    return recorder


def _vm_power_bulk_item_error(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm"] = {"value": [{"vm": "vm-1"}, {"vm": "vm-2"}]}
    recorder.failures["/vcenter/vm/vm-2/power?action=start"] = "500"
    return recorder


def _guest_program_exit_unknown(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_guest, "_RUN_POLL_INTERVAL_SECONDS", 0.0)
    mp.setattr(
        _guest,
        "load_basic_credentials",
        AsyncMock(return_value={"guest_username": "svc", "guest_password": "pw"}),
    )
    recorder = _RecordingVmwareConnector()
    manager = {"type": "GuestProcessManager", "value": "pm-1"}
    recorder.vmomi_responses.update(
        {
            "GuestOperationsManager": _vmomi_retrieve_result(
                "GuestOperationsManager", "guestOperationsManager", "processManager", manager
            ),
            "/GuestProcessManager/pm-1/StartProgramInGuest": 4242,
        }
    )
    # The process is listed once while running, then it is gone before an
    # exit code is seen: the exit code is lost.
    listings = iter([[{"pid": 4242}], []])

    def _list(path: str, json: Any) -> Any | None:
        del json
        return next(listings) if path.endswith("/ListProcessesInGuest") else None

    _wrap_vmomi(recorder, _list)
    return recorder


def _host_evacuate_aborted(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    # DRS is manual and has no suggestion for the running VM, so MEHO cannot
    # move it (no_recommendation) and stops before maintenance mode (#4895).
    recorder.responses["/vcenter/vm"] = {"value": [_vm_listing_row("vm-1")]}
    recorder.vmomi_responses["HostSystem"] = _vmomi_host_parent("host-1", "domain-c1")
    recorder.vmomi_responses["ClusterComputeResource"] = _vmomi_cluster_drs("domain-c1", "manual")
    return recorder


def _host_evacuate_partial(mp: pytest.MonkeyPatch) -> Any:
    # Same no_recommendation, with partial failure allowed: the host enters
    # maintenance, the listing after the task still shows the running VM, so
    # it is reported as left behind.
    recorder = _host_evacuate_aborted(mp)
    recorder.vmomi_responses["/HostSystem/host-1/EnterMaintenanceMode_Task"] = _vmomi_task_moref(
        "t-1"
    )
    return recorder


def _guest_program_timeout(mp: pytest.MonkeyPatch) -> Any:
    recorder = _guest_program_exit_unknown(mp)
    # Each clock read jumps 10 s, so the 1 s timeout is over after one poll.
    clock = itertools.count(start=0.0, step=10.0)
    mp.setattr(_guest, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    def _always_running(path: str, json: Any) -> Any | None:
        del json
        return [{"pid": 4242}] if path.endswith("/ListProcessesInGuest") else None

    _wrap_vmomi(recorder, _always_running)
    return recorder


class _ToolsDownVmwareConnector(_RecordingVmwareConnector):
    """Answers the guest power call with a 503, as vCenter does when Tools is down."""

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        if self._spec(path).startswith("/vcenter/vm/vm-1/guest/power"):
            request = httpx.Request("POST", f"https://vc{path}")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("Service unavailable", request=request, response=response)
        return await super()._post_json(target, path, **kwargs)


def _vm_power_tools_down(mp: pytest.MonkeyPatch) -> Any:
    del mp
    return _ToolsDownVmwareConnector()


def _host_detach_incomplete(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses.update(
        {
            "/vcenter/network": {"value": []},
            "/vcenter/vm": {"value": [{"vm": "vm-1"}]},
            # Not a list: the VM's NICs cannot be moved.
            "/vcenter/vm/vm-1/hardware/ethernet": {"value": {}},
        }
    )
    return recorder


def _host_detach_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_DVS_RECONFIGURE_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _RecordingVmwareConnector()
    recorder.responses.update({"/vcenter/network": {"value": []}, "/vcenter/vm": {"value": []}})
    recorder.vmomi_responses.update(
        {
            "DistributedVirtualSwitch": _vmomi_retrieve_result(
                "DistributedVirtualSwitch", "dvs-1", "config.configVersion", "1"
            ),
            "/DistributedVirtualSwitch/dvs-1/ReconfigureDvs_Task": _vmomi_task_moref("t-1"),
        }
    )
    _task_never_finishes(recorder)
    return recorder


def _host_config_manager_unreadable(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/host"] = {"value": [{"host": "host-1"}]}
    recorder.vmomi_responses["HostSystem"] = {"objects": []}
    return recorder


def _host_disk_mark_flash_partial(mp: pytest.MonkeyPatch) -> Any:
    del mp
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/host"] = {"value": [{"host": "host-1"}]}
    storage_system = {"type": "HostStorageSystem", "value": "storageSystem-1"}
    recorder.vmomi_responses.update(
        {
            "HostSystem": _vmomi_retrieve_result(
                "HostSystem", "host-1", "configManager.storageSystem", storage_system
            ),
            "/HostStorageSystem/storageSystem-1/MarkAsSsd_Task": _vmomi_task_moref("t-1"),
            "Task": _vmomi_retrieve_result(
                "Task", "t-1", "info", {"state": "error", "error": {"localizedMessage": "busy"}}
            ),
        }
    )
    return recorder


def _portgroup_create_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_NETWORK_PORTGROUP_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _RecordingVmwareConnector()
    recorder.vmomi_responses["/DistributedVirtualSwitch/dvs-1/CreateDVPortgroup_Task"] = (
        _vmomi_task_moref("t-1")
    )
    _task_never_finishes(recorder)
    return recorder


def _portgroup_reconfigure_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_NETWORK_PORTGROUP_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _RecordingVmwareConnector()
    recorder.vmomi_responses.update(
        {
            "DistributedVirtualPortgroup": _portgroup_pre_read(),
            "/DistributedVirtualPortgroup/dvportgroup-1/ReconfigureDVPortgroup_Task": (
                _vmomi_task_moref("t-1")
            ),
        }
    )
    _task_never_finishes(recorder)
    return recorder


def _drs_rule_create_timeout(mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(_write, "_DRS_RULE_TASK_TIMEOUT_SECONDS", 0.0)
    recorder = _DrsRuleVmwareConnector()
    _task_never_finishes(recorder)
    return recorder


_DRS_RULE_PARAMS: dict[str, Any] = {
    "cluster": "domain-c1",
    "rule_name": "keep-apart",
    "rule_type": "anti_affinity",
    "vms": ["web-01", "web-02"],
}
_DETACH_PARAMS: dict[str, Any] = {"host": "host-1", "dvs": "dvs-1", "fallback_network": "net-fb"}

_HARD_FAILURES: list[tuple[str, dict[str, Any], Setup, str]] = [
    (
        "vmware.composite.vm.create",
        {"folder_name": "prod", "name": "vm-new", "guest_os": "UBUNTU_64"},
        _vm_create_rolled_back,
        "rolled_back",
    ),
    (
        "vmware.composite.vm.resize",
        {"vm": "vm-1", "cpu_count": 2, "memory_mib": 2048},
        _vm_resize_partial,
        "partial",
    ),
    (
        "vmware.composite.vm.disk.grow",
        {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        _vm_disk_grow_timeout,
        "timeout",
    ),
    (
        "vmware.composite.vm.snapshot.revert",
        {"vm": "vm-1", "snapshot_name": "snap-a"},
        _vm_snapshot_revert_timeout,
        "timeout",
    ),
    ("vmware.composite.vm.power", {"vm": "vm-1", "verb": "on"}, _vm_power_error, "error"),
    (
        "vmware.composite.vm.power",
        {"vm": "vm-1", "verb": "guest_shutdown"},
        _vm_power_tools_down,
        "tools_unavailable",
    ),
    (
        "vmware.composite.vm.power.bulk",
        {"action": "start"},
        _vm_power_bulk_item_error,
        "composite_error",
    ),
    (
        "vmware.composite.vm.guest.program.run",
        {"vm": "vm-1", "program_path": "/bin/true", "wait": True},
        _guest_program_exit_unknown,
        "exit_unknown",
    ),
    (
        "vmware.composite.vm.guest.program.run",
        {"vm": "vm-1", "program_path": "/bin/sleep", "wait": True, "timeout_seconds": 1},
        _guest_program_timeout,
        "timeout",
    ),
    ("vmware.composite.host.evacuate", {"host": "host-1"}, _host_evacuate_aborted, "aborted"),
    (
        "vmware.composite.host.evacuate",
        {"host": "host-1", "tolerate_partial_failure": True},
        _host_evacuate_partial,
        "partial",
    ),
    (
        "vmware.composite.host.detach_from_vds",
        _DETACH_PARAMS,
        _host_detach_incomplete,
        "incomplete",
    ),
    ("vmware.composite.host.detach_from_vds", _DETACH_PARAMS, _host_detach_timeout, "timeout"),
    (
        "vmware.composite.host.datastore_mount_nfs",
        {
            "host": "host-1",
            "nfs_server": "nfs.example.invalid",
            "remote_path": "/export/a",
            "datastore_name": "nfs-a",
        },
        _host_config_manager_unreadable,
        "config_manager_unreadable",
    ),
    (
        "vmware.composite.host.service_control",
        {"host": "host-1", "service": "TSM-SSH", "action": "start"},
        _host_config_manager_unreadable,
        "config_manager_unreadable",
    ),
    (
        "vmware.composite.host.disk_mark_flash",
        {"host": "host-1", "disk_uuids": ["disk-uuid-1"], "mode": "flash"},
        _host_disk_mark_flash_partial,
        "partial",
    ),
    (
        "vmware.composite.network.portgroup.create",
        {"vds": "dvs-1", "name": "pg-a"},
        _portgroup_create_timeout,
        "timeout",
    ),
    (
        "vmware.composite.network.portgroup.security.set",
        {"portgroup": "dvportgroup-1", "allow_promiscuous": False},
        _portgroup_reconfigure_timeout,
        "timeout",
    ),
    (
        "vmware.composite.network.portgroup.vlan.set",
        {"portgroup": "dvportgroup-1", "vlan_id": 120},
        _portgroup_reconfigure_timeout,
        "timeout",
    ),
    (
        "vmware.composite.cluster.drs_rule.create",
        _DRS_RULE_PARAMS,
        _drs_rule_create_timeout,
        "timeout",
    ),
]


def _case_id(case: tuple[str, dict[str, Any], Setup, str]) -> str:
    return f"{case[0].removeprefix('vmware.composite.')}-{case[3]}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "setup", "error_code"),
    _HARD_FAILURES,
    ids=[_case_id(case) for case in _HARD_FAILURES],
)
async def test_write_composite_hard_failure_dispatches_as_error(
    op_id: str,
    params: dict[str, Any],
    setup: Setup,
    error_code: str,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that did not complete is an error result with an ``error`` audit row."""
    recorder = setup(monkeypatch)

    result = await _dispatch_direct(recorder, stub_embedding_service, op_id, params)

    await _assert_dispatch_error(result, op_id, error_code)


@pytest.mark.asyncio
async def test_content_library_status_read_error_dispatches_as_error(
    stub_embedding_service: AsyncMock,
) -> None:
    """The read composite's ``read_error`` already carries an ``error`` issue."""
    op_id = "vmware.composite.content_library.subscribed.status"
    recorder = _RecordingVmwareConnector()
    recorder.responses["/content/subscribed-library/lib-1"] = {"value": []}

    result = await _dispatch_direct(
        recorder, stub_embedding_service, op_id, {"library_id": "lib-1"}
    )

    await _assert_dispatch_error(result, op_id, "read_error")


@pytest.fixture
def jsonflux_reducer() -> Iterator[None]:
    """Use the real :class:`JsonFluxReducer` (more than 50 rows -> a handle)."""
    set_default_reducer(JsonFluxReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())


@pytest.mark.asyncio
@pytest.mark.usefixtures("jsonflux_reducer")
async def test_large_failed_power_bulk_returns_a_handle(
    stub_embedding_service: AsyncMock,
) -> None:
    """A failed bulk over many VMs still returns a handle, with the failure inline.

    The error path must not send the whole per-VM list back inline. The
    ``status`` / ``summary`` / ``issues`` fields stay visible without a drill-in.
    """
    op_id = "vmware.composite.vm.power.bulk"
    vm_count = 120
    recorder = _RecordingVmwareConnector()
    recorder.responses["/vcenter/vm"] = {"value": [{"vm": f"vm-{n}"} for n in range(vm_count)]}
    recorder.failures["/vcenter/vm/vm-7/power?action=start"] = "500"

    result = await _dispatch_direct(recorder, stub_embedding_service, op_id, {"action": "start"})

    await _assert_dispatch_error(result, op_id, "composite_error")
    assert result.handle is not None
    assert result.handle.total_rows == vm_count
    assert result.result is not None
    assert "results" not in result.result
    assert result.result["summary"] == {"ok": vm_count - 1, "error": 1}
    assert "failed on 1 of 120 VMs" in result.result["issues"][0]["message"]
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalar_one()
    assert row.payload["error"]["error_code"] == "composite_error"


# ---------------------------------------------------------------------------
# Soft refusals and idempotency guards stay ok
# ---------------------------------------------------------------------------


def _drs_rule_exists(mp: pytest.MonkeyPatch) -> Any:
    del mp
    return _DrsRuleVmwareConnector(existing_rules=[{"name": "keep-apart", "key": 1}])


def _no_reads(mp: pytest.MonkeyPatch) -> Any:
    del mp
    return _RecordingVmwareConnector()


_SOFT_REFUSALS: list[tuple[str, dict[str, Any], Setup, str]] = [
    ("vmware.composite.cluster.drs_rule.create", _DRS_RULE_PARAMS, _drs_rule_exists, "rule_exists"),
    ("vmware.composite.vm.resize", {"vm": "vm-1", "cpu_count": 1}, _vm_resize_reads, "no_change"),
    (
        "vmware.composite.network.portgroup.security.set",
        {"portgroup": "dvportgroup-1"},
        _no_reads,
        "no_change_requested",
    ),
    (
        "vmware.composite.vm.snapshot.revert",
        {"vm": "vm-1", "snapshot_name": "missing"},
        _vm_snapshot_revert_timeout,
        "not_found",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "setup", "status"),
    _SOFT_REFUSALS,
    ids=[_case_id(case) for case in _SOFT_REFUSALS],
)
async def test_soft_refusal_stays_ok(
    op_id: str,
    params: dict[str, Any],
    setup: Setup,
    status: str,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal or a no-op is not a failure: ``ok`` result, ``ok`` audit row."""
    recorder = setup(monkeypatch)

    result = await _dispatch_direct(recorder, stub_embedding_service, op_id, params)

    assert result.status == "ok", (result.error, result.result)
    assert result.result["status"] == status
    assert "issues" not in result.result
    assert await _audit_statuses(op_id) == ["ok"]


# ---------------------------------------------------------------------------
# storage_policy.delete (destructive): preview -> park -> approve -> resume
# ---------------------------------------------------------------------------


class _PbmRecordingVmwareConnector(_RecordingVmwareConnector):
    """Adds the PBM SOAP delete the storage-policy composite calls."""

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        super().__init__()
        self._outcomes = outcomes
        self.pbm_deletes: list[list[str]] = []

    async def pbm_delete_profiles(
        self, target: Any, operator: Any, *, profile_ids: list[str]
    ) -> list[dict[str, Any]]:
        del target, operator
        self.pbm_deletes.append(profile_ids)
        return self._outcomes


async def _seed_target() -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name="test-vcenter",
                aliases=[],
                product="vmware",
                host="vcenter.test",
                port=443,
                auth_model="shared_service_account",
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_storage_policy_delete_fault_on_resume_is_error(
    stub_embedding_service: AsyncMock,
) -> None:
    """A PbmDelete fault after approval returns an error, and the audit row says so."""
    op_id = "vmware.composite.storage_policy.delete"
    recorder = _PbmRecordingVmwareConnector(
        [{"key": "pol-1", "fault": {"_typeName": "PbmFaultProfileStorageFault"}}]
    )
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": "test-vcenter",
        "params": {"policy_id": "pol-1"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)

    assert recorder.pbm_deletes == [["pol-1"]]
    await _assert_dispatch_error(resume, op_id, "delete_failed")
