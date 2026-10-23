# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the VM hardware read, the CD drive add and the Tools installer unmount.

A REST fake stands in for the connector session. It answers with saved
vSphere REST responses (the shapes of ``Cpu.Info``, ``Memory.Info``,
``Cdrom.Info``, the adapter ``Info`` objects and ``Installer.Info``). No live
vCenter. The policy check of the write seam is replaced by a recorder, so the
tests show what is checked and that a held write never reaches vCenter.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest import typed_ops_vm_hardware as hw
from meho_backplane.connectors.vmware_rest.composites import _vm_cdrom_tools as tools
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params

_VM = "vm-42"
_BASE = f"/api/vcenter/vm/{_VM}"


def _operator() -> Operator:
    return Operator(
        sub="op-vm-hardware",
        name="VM Hardware Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0c2"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-test"


def _saved_responses() -> dict[str, Any]:
    """Saved vSphere REST answers for one VM with two CD drives and three controllers."""
    return {
        f"{_BASE}/hardware/cpu": {
            "count": 4,
            "cores_per_socket": 2,
            "hot_add_enabled": False,
            "hot_remove_enabled": False,
        },
        f"{_BASE}/hardware/memory": {
            "size_MiB": 8192,
            "hot_add_enabled": True,
            "hot_add_increment_size_MiB": 128,
            "hot_add_limit_MiB": 16384,
        },
        f"{_BASE}/hardware/cdrom": [{"cdrom": "16000"}, {"cdrom": "16001"}],
        f"{_BASE}/hardware/cdrom/16000": {
            "type": "SATA",
            "label": "CD/DVD drive 1",
            "sata": {"bus": 0, "unit": 0},
            "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/ubuntu.iso"},
            "state": "CONNECTED",
            "start_connected": True,
            "allow_guest_control": False,
        },
        f"{_BASE}/hardware/cdrom/16001": {
            "type": "IDE",
            "label": "CD/DVD drive 2",
            "ide": {"primary": False, "master": True},
            "backing": {"type": "CLIENT_DEVICE", "device_access_type": "PASSTHRU"},
            "state": "NOT_CONNECTED",
            "start_connected": False,
            "allow_guest_control": True,
        },
        f"{_BASE}/hardware/adapter/scsi": [{"adapter": "1000"}],
        f"{_BASE}/hardware/adapter/scsi/1000": {
            "label": "SCSI controller 0",
            "type": "PVSCSI",
            "scsi": {"bus": 0, "unit": 7},
            "pci_slot_number": 160,
            "sharing": "NONE",
        },
        f"{_BASE}/hardware/adapter/sata": [{"adapter": "15000"}],
        f"{_BASE}/hardware/adapter/sata/15000": {
            "label": "SATA controller 0",
            "type": "AHCI",
            "bus": 0,
            "pci_slot_number": 33,
        },
        f"{_BASE}/hardware/adapter/nvme": [],
        f"{_BASE}/tools/installer": {"is_connected": True},
        "/api/vcenter/vm": [{"vm": _VM, "name": "web-01"}],
    }


def _status_error(method: str, path: str, status: int, body: dict[str, Any]) -> httpx.HTTPError:
    request = httpx.Request(method, f"https://vc.test{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _RestFake:
    """Recording double for the connector's REST session."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = _saved_responses() if responses is None else responses
        self.get_errors: dict[str, int] = {}
        self.post_error: httpx.HTTPError | None = None
        self.post_result: Any = "16002"
        self.on_post: Any = None
        self.gets: list[tuple[str, Any]] = []
        self.posts: list[tuple[str, str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.gets.append((path, params))
        if path in self.get_errors:
            raise _status_error("GET", path, self.get_errors[path], {"error_type": "NOT_FOUND"})
        if path not in self.responses:
            raise AssertionError(f"unexpected GET {path!r}")
        return copy.deepcopy(self.responses[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        self.posts.append((verb, path, copy.deepcopy(json)))
        if self.post_error is not None:
            raise self.post_error
        if self.on_post is not None:
            self.on_post(self)
        return self.post_result


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _assert_schema(schema: dict[str, Any], out: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(out)


# ---------------------------------------------------------------------------
# vmware.vm.hardware.show
# ---------------------------------------------------------------------------


async def _show(conn: _RestFake, **params: Any) -> dict[str, Any]:
    return await hw.vm_hardware_show_impl(conn, _operator(), _Target(), params)  # type: ignore[arg-type]


async def test_show_reads_cpu_memory_cdroms_controllers_and_installer() -> None:
    out = await _show(_RestFake(), vm=_VM)
    _assert_schema(hw.VMWARE_VM_HARDWARE_SHOW_OP.response_schema or {}, out)
    assert out["cpu"] == {
        "count": 4,
        "cores_per_socket": 2,
        "hot_add_enabled": False,
        "hot_remove_enabled": False,
    }
    assert out["memory"] == {
        "size_mib": 8192,
        "hot_add_enabled": True,
        "hot_add_increment_size_mib": 128,
        "hot_add_limit_mib": 16384,
    }
    iso, client = out["cdroms"]
    assert iso["backing_type"] == "ISO_FILE"
    assert iso["iso_file"] == "[datastore1] iso/ubuntu.iso"
    assert iso["sata"] == {"bus": 0, "unit": 0}
    assert iso["connected"] is True
    assert client["backing_type"] == "CLIENT_DEVICE"
    assert client["ide"] == {"primary": False, "master": True}
    assert client["connected"] is False
    assert [(c["kind"], c["adapter"], c["bus"]) for c in out["disk_controllers"]] == [
        ("scsi", "1000", 0),
        ("sata", "15000", 0),
    ]
    assert out["disk_controllers"][0]["type"] == "PVSCSI"
    assert out["disk_controllers"][0]["unit"] == 7
    assert out["tools_installer_mounted"] is True
    assert out["read_notes"] == []


async def test_show_resolves_a_name() -> None:
    conn = _RestFake()
    out = await _show(conn, name="web-01")
    assert out["vm"] == _VM
    assert out["name"] == "web-01"
    assert conn.gets[0] == ("/api/vcenter/vm", {"filter.names": ["web-01"]})


async def test_show_unknown_vm_is_not_found() -> None:
    conn = _RestFake()
    conn.get_errors[f"{_BASE}/hardware/cpu"] = 404
    with pytest.raises(ConnectorResourceNotFoundError):
        await _show(conn, vm=_VM)


async def test_show_unknown_name_is_not_found() -> None:
    conn = _RestFake()
    conn.responses["/api/vcenter/vm"] = []
    with pytest.raises(ConnectorResourceNotFoundError):
        await _show(conn, name="missing")


async def test_show_cpu_error_other_than_404_raises() -> None:
    conn = _RestFake()
    conn.get_errors[f"{_BASE}/hardware/cpu"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        await _show(conn, vm=_VM)


async def test_show_best_effort_parts_become_null_with_a_note() -> None:
    conn = _RestFake()
    conn.get_errors[f"{_BASE}/hardware/cdrom"] = 500
    conn.get_errors[f"{_BASE}/tools/installer"] = 500
    for kind in ("scsi", "sata", "nvme"):
        conn.get_errors[f"{_BASE}/hardware/adapter/{kind}"] = 500
    out = await _show(conn, vm=_VM)
    _assert_schema(hw.VMWARE_VM_HARDWARE_SHOW_OP.response_schema or {}, out)
    assert out["cdroms"] is None
    assert out["disk_controllers"] is None
    assert out["tools_installer_mounted"] is None
    assert len(out["read_notes"]) == 5
    assert out["cpu"]["count"] == 4


async def test_show_one_failed_controller_list_keeps_the_others() -> None:
    conn = _RestFake()
    conn.get_errors[f"{_BASE}/hardware/adapter/nvme"] = 404
    out = await _show(conn, vm=_VM)
    assert [c["kind"] for c in out["disk_controllers"]] == ["scsi", "sata"]
    assert len(out["read_notes"]) == 1


@pytest.mark.parametrize("bad", ["vm-42/../../session", "vm-42\n", "web-01", "vm-"])
async def test_show_refuses_a_vm_id_that_is_not_a_moid(bad: str) -> None:
    conn = _RestFake()
    with pytest.raises(ValueError, match="not a vCenter VM id"):
        await _show(conn, vm=bad)
    assert conn.gets == []


async def test_show_skips_a_device_id_with_odd_characters() -> None:
    conn = _RestFake()
    conn.responses[f"{_BASE}/hardware/cdrom"] = [{"cdrom": "16000/../x"}]
    out = await _show(conn, vm=_VM)
    assert out["cdroms"][0]["cdrom"] == "16000/../x"
    assert out["cdroms"][0]["backing_type"] is None
    assert all("../x" not in path for path, _ in conn.gets)


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42"}, True),
        ({"name": "web-01"}, True),
        ({"vm": "vm-42", "name": "web-01"}, False),
        ({}, False),
        ({"vm": "vm-42\n"}, False),
    ],
)
def test_show_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(hw.VMWARE_VM_HARDWARE_SHOW_OP.parameter_schema, params)
    assert (errors == []) is valid, errors


def test_every_rest_call_of_the_read_is_declared() -> None:
    assert set(hw.HARDWARE_REST_CALLS) >= {
        "GET:/vcenter/vm/{vm}/hardware/cpu",
        "GET:/vcenter/vm/{vm}/hardware/memory",
        "GET:/vcenter/vm/{vm}/hardware/cdrom",
        "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
        "GET:/vcenter/vm/{vm}/hardware/adapter/scsi",
        "GET:/vcenter/vm/{vm}/hardware/adapter/sata",
        "GET:/vcenter/vm/{vm}/hardware/adapter/nvme",
        "GET:/vcenter/vm/{vm}/tools/installer",
    }


# ---------------------------------------------------------------------------
# Risk level and approval
# ---------------------------------------------------------------------------


def test_read_is_safe_and_runs_without_approval() -> None:
    op = hw.VMWARE_VM_HARDWARE_SHOW_OP
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False


@pytest.mark.parametrize(
    ("op_id", "level"),
    [
        ("vmware.composite.vm.cdrom.add", "dangerous"),
        ("vmware.composite.vm.tools.installer.unmount", "caution"),
    ],
)
def test_changes_wait_for_approval_on_both_catalogs(op_id: str, level: str) -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == op_id)
    assert spec.safety_level == level
    assert spec.requires_approval is True
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _PREVIEW_BUILDERS


# ---------------------------------------------------------------------------
# vmware.composite.vm.cdrom.add
# ---------------------------------------------------------------------------


async def _add(conn: _RestFake, **params: Any) -> Any:
    return await tools.vm_cdrom_add_composite(
        operator=_operator(),
        target=_Target(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


def _new_drive_appears(conn: _RestFake) -> None:
    conn.responses[f"{_BASE}/hardware/cdrom"].append({"cdrom": "16002"})
    conn.responses[f"{_BASE}/hardware/cdrom/16002"] = {
        "type": "SATA",
        "label": "CD/DVD drive 3",
        "sata": {"bus": 0, "unit": 1},
        "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/tools.iso"},
        "state": "NOT_CONNECTED",
        "start_connected": True,
        "allow_guest_control": False,
    }


async def test_add_with_iso_sends_a_create_spec_and_reads_the_drive_back(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake()
    conn.on_post = _new_drive_appears
    out = await _add(conn, vm=_VM, iso_file="[datastore1] iso/tools.iso", type="SATA")
    _assert_schema(tools.VM_CDROM_ADD_RESPONSE_SCHEMA, out)
    assert conn.posts == [
        (
            "POST",
            f"{_BASE}/hardware/cdrom",
            {
                "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/tools.iso"},
                "start_connected": True,
                "type": "SATA",
            },
        )
    ]
    assert out["status"] == "added"
    assert out["cdrom"] == "16002"
    assert out["cdrom_info"]["iso_file"] == "[datastore1] iso/tools.iso"
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/vm/{vm}/hardware/cdrom"
    assert call["params"]["vm"] == _VM


async def test_add_without_iso_adds_an_empty_client_device_drive(gate: _GateRecorder) -> None:
    conn = _RestFake()
    await _add(conn, name="web-01")
    ((_, path, body),) = conn.posts
    assert path == f"{_BASE}/hardware/cdrom"
    assert body == {"backing": {"type": "CLIENT_DEVICE"}, "start_connected": False}


async def test_add_refused_by_vcenter_changes_nothing(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_error = _status_error(
        "POST",
        f"{_BASE}/hardware/cdrom",
        400,
        {
            "error_type": "NOT_ALLOWED_IN_CURRENT_STATE",
            "messages": [{"default_message": "IDE device cannot be added while powered on"}],
        },
    )
    out = await _add(conn, vm=_VM, type="IDE")
    _assert_schema(tools.VM_CDROM_ADD_RESPONSE_SCHEMA, out)
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert "nothing changed" in out["guidance"]


async def test_add_server_error_raises(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_error = _status_error("POST", f"{_BASE}/hardware/cdrom", 500, {})
    with pytest.raises(httpx.HTTPStatusError):
        await _add(conn, vm=_VM)


async def test_add_held_by_policy_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vcenter/vm/{vm}/hardware/cdrom",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake()
    assert await _add(conn, vm=_VM) is held
    assert conn.posts == []


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42"}, True),
        ({"name": "web-01", "iso_file": "[datastore1] iso/a.iso"}, True),
        ({"vm": "vm-42", "iso_file": "iso/a.iso"}, False),
        ({"vm": "vm-42", "type": "NVME"}, False),
        ({"vm": "vm-42", "name": "web-01"}, False),
    ],
)
def test_add_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(tools.VM_CDROM_ADD_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


async def test_add_preview_shows_the_drives_the_vm_has_now() -> None:
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=_RestFake(),  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params={"name": "web-01", "iso_file": "[datastore1] iso/a.iso"},
    )
    preview = await _PREVIEW_BUILDERS["vmware.composite.vm.cdrom.add"](ctx)
    assert preview is not None
    assert preview["vm"] == _VM
    assert len(preview["current_cdroms"]) == 2
    assert preview["requested"]["backing"]["iso_file"] == "[datastore1] iso/a.iso"


# ---------------------------------------------------------------------------
# vmware.composite.vm.tools.installer.unmount
# ---------------------------------------------------------------------------


async def _unmount(conn: _RestFake, **params: Any) -> Any:
    return await tools.vm_tools_installer_unmount_composite(
        operator=_operator(),
        target=_Target(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


def _installer_out(conn: _RestFake) -> None:
    conn.responses[f"{_BASE}/tools/installer"] = {"is_connected": False}


async def test_unmount_sends_disconnect_and_reads_again(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_result = {}
    conn.on_post = _installer_out
    out = await _unmount(conn, vm=_VM)
    _assert_schema(tools.VM_TOOLS_INSTALLER_UNMOUNT_RESPONSE_SCHEMA, out)
    assert conn.posts == [("POST", f"{_BASE}/tools/installer?action=disconnect", None)]
    assert out["status"] == "unmounted"
    assert out["mounted_before"] is True
    assert out["mounted_after"] is False
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/vm/{vm}/tools/installer?action=disconnect"


async def test_unmount_when_not_mounted_sends_nothing(gate: _GateRecorder) -> None:
    conn = _RestFake()
    _installer_out(conn)
    out = await _unmount(conn, name="web-01")
    _assert_schema(tools.VM_TOOLS_INSTALLER_UNMOUNT_RESPONSE_SCHEMA, out)
    assert out["status"] == "not_mounted"
    assert conn.posts == []
    assert gate.calls == []


async def test_unmount_still_mounted_after_the_call(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_result = {}
    out = await _unmount(conn, vm=_VM)
    assert out["status"] == "still_mounted"
    assert out["mounted_after"] is True


async def test_unmount_refused_for_a_vm_that_is_off(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_error = _status_error(
        "POST",
        f"{_BASE}/tools/installer?action=disconnect",
        400,
        {"error_type": "NOT_ALLOWED_IN_CURRENT_STATE", "messages": []},
    )
    out = await _unmount(conn, vm=_VM)
    _assert_schema(tools.VM_TOOLS_INSTALLER_UNMOUNT_RESPONSE_SCHEMA, out)
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"


async def test_unmount_held_by_policy_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vcenter/vm/{vm}/tools/installer?action=disconnect",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake()
    assert await _unmount(conn, vm=_VM) is held
    assert conn.posts == []


async def test_unmount_preview_shows_the_installer_state() -> None:
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=_RestFake(),  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM},
    )
    preview = await _PREVIEW_BUILDERS["vmware.composite.vm.tools.installer.unmount"](ctx)
    assert preview is not None
    assert preview["installer_mounted"] is True
    assert "Unmount" in preview["summary"]


async def test_preview_without_a_connector_declines() -> None:
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM},
    )
    for op_id in (tools.OP_ID_VM_CDROM_ADD, tools.OP_ID_VM_TOOLS_INSTALLER_UNMOUNT):
        assert await _PREVIEW_BUILDERS[op_id](ctx) is None
