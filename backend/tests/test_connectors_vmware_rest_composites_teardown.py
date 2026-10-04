# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the #3339 inventory teardown deletes.

``network.portgroup.delete`` (distributed), ``host.standard_portgroup.delete``
and ``folder.delete``, driven against the shared vim fake. Each op is checked
for: the happy path (gated write, task poll, read-back), every refusal before
any write (and that the gate never sees it), ``unchanged`` when absent, a task
fault raising, the poll timeout, a parked gate keeping the write off the wire,
the read-back mismatch, the response schema, and the park-time blast radius
(the same plan the handler uses).
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.connectors.vmware_rest.composites import (
    _teardown,
    _teardown_network,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    FOLDER_DELETE_PARAMETER_SCHEMA,
    FOLDER_DELETE_RESPONSE_SCHEMA,
    HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_DVPG_DESTROY_OP = "POST:/DistributedVirtualPortgroup/{moId}/Destroy_Task"
_FOLDER_DESTROY_OP = "POST:/Folder/{moId}/Destroy_Task"
_REMOVE_PG_OP = "POST:/HostNetworkSystem/{moId}/RemovePortGroup"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _call(handler: Any, conn: VimFake, **params: Any) -> Any:
    return await handler(operator=operator(), target=object(), params=params, connector=conn)


def _validate(schema: dict[str, Any], out: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(out)


# ===========================================================================
# network.portgroup.delete
# ===========================================================================


def _dvpg(conn: VimFake, *, vms: list[str] | None = None, uplink: bool = False) -> None:
    conn.add(
        "DistributedVirtualPortgroup",
        "dvportgroup-42",
        name="example-dvpg",
        key="dvportgroup-42",
        vm=[moref("VirtualMachine", v) for v in (vms or [])],
        config={
            "name": "example-dvpg",
            "numPorts": 8,
            "type": "earlyBinding",
            "uplink": uplink,
            "distributedVirtualSwitch": moref("VmwareDistributedVirtualSwitch", "dvs-7"),
            "defaultPortConfig": {
                "vlan": {
                    "_typeName": "VmwareDistributedVirtualSwitchTrunkVlanSpec",
                    "vlanId": [{"start": 100, "end": 120}],
                }
            },
        },
    )
    for v in vms or []:
        conn.add("VirtualMachine", v, name=f"name-{v}")


async def test_portgroup_delete_happy_path(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn)
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "deleted"
    assert out["object"]["name"] == "example-dvpg"
    assert out["object"]["vlan"] == {"mode": "trunk", "ranges": [{"start": 100, "end": 120}]}
    assert out["object"]["port_check"] == "ok"
    assert out["task_state"] == "success"
    _validate(NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA, out)
    # One gated Destroy_Task on the portgroup, after the read-only checks.
    assert [c["op_id"] for c in gate.calls] == [_DVPG_DESTROY_OP]
    assert gate.calls[0]["params"] == {"portgroup": "dvportgroup-42"}
    assert (
        conn.calls_to("Destroy_Task")[0][0]
        == "/DistributedVirtualPortgroup/dvportgroup-42/Destroy_Task"
    )
    # The port check ran against the owning switch with its own type.
    path, body = conn.calls_to("FetchDVPorts")[0]
    assert path == "/VmwareDistributedVirtualSwitch/dvs-7/FetchDVPorts"
    assert body["criteria"]["_typeName"] == "DistributedVirtualSwitchPortCriteria"
    assert body["criteria"]["portgroupKey"] == ["dvportgroup-42"]


async def test_portgroup_delete_refuses_connected_vms(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn, vms=["vm-1", "vm-2"])
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "precondition_failed"
    assert {b["moid"] for b in out["blockers"]} == {"vm-1", "vm-2"}
    assert {b["name"] for b in out["blockers"]} == {"name-vm-1", "name-vm-2"}
    assert "name-vm-1" in out["guidance"]
    assert gate.calls == []
    assert "Destroy_Task" not in conn.methods()
    _validate(NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_portgroup_delete_refuses_vmkernel_port(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn)
    conn.dv_ports = [
        {
            "key": "17",
            "connectee": {
                "connectedEntity": moref("HostSystem", "host-9"),
                "nicKey": "vmk1",
                "type": "hostVmknic",
            },
        }
    ]
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [
        {
            "kind": "port",
            "id": "17",
            "connectee_type": "hostVmknic",
            "connected_entity": "host-9",
            "nic": "vmk1",
        }
    ]
    assert gate.calls == []


async def test_portgroup_delete_refuses_uplink(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn, uplink=True)
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "precondition_failed"
    assert "uplink" in out["guidance"]
    assert gate.calls == []


async def test_portgroup_delete_absent_is_unchanged(gate: GateRecorder) -> None:
    conn = VimFake()
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-404"
    )
    assert out["status"] == "unchanged"
    assert out["object"] == {"kind": "distributed_portgroup", "moid": "dvportgroup-404"}
    assert gate.calls == []
    _validate(NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_portgroup_delete_absent_as_json_vim_fault_is_unchanged(gate: GateRecorder) -> None:
    """A JSON ``ManagedObjectNotFound`` 500 body also reads as absent."""
    conn = VimFake()
    conn.not_found_as_json = True
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-404"
    )
    assert out["status"] == "unchanged"


async def test_other_json_vim_fault_propagates(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise http_status_error(500, '{"_typeName": "NoPermission"}')

    conn._post_vmomi_json = _boom  # type: ignore[method-assign]
    with pytest.raises(Exception, match="500"):
        await _call(
            _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
        )
    assert gate.calls == []


async def test_portgroup_delete_proceeds_when_port_list_unreadable(gate: GateRecorder) -> None:
    """The port read only names users; vSphere refuses an in-use delete itself."""
    conn = VimFake()
    _dvpg(conn)
    conn.dv_ports = RuntimeError("FetchDVPorts unavailable")
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "deleted"
    assert out["object"]["port_check"] == "unavailable (RuntimeError)"


async def test_portgroup_delete_task_fault_raises(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn)
    conn.task_faults["Destroy_Task"] = ("ResourceInUse", "The resource 'example-dvpg' is in use.")
    with pytest.raises(RuntimeError, match="is in use"):
        await _call(
            _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
        )


async def test_portgroup_delete_timeout(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_teardown, "_DESTROY_TASK_TIMEOUT_SECONDS", 0.0)
    conn = VimFake()
    _dvpg(conn)
    conn.running.add("Destroy_Task")
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "timeout"
    assert out["task_state"] == "timeout"
    _validate(NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_portgroup_delete_read_back_still_present(gate: GateRecorder) -> None:
    conn = VimFake()
    _dvpg(conn)
    conn.apply_writes = False
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out["status"] == "still_present"


async def test_portgroup_delete_park_keeps_write_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = parked(_DVPG_DESTROY_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    conn = VimFake()
    _dvpg(conn)
    out = await _call(
        _teardown_network.network_portgroup_delete_composite, conn, portgroup="dvportgroup-42"
    )
    assert out is verdict
    assert "Destroy_Task" not in conn.methods()


async def test_portgroup_preview_is_the_handler_plan() -> None:
    conn = VimFake()
    _dvpg(conn, vms=["vm-1"])
    effect = await _teardown_network.network_portgroup_delete_preview(
        preview_ctx({"portgroup": "dvportgroup-42"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["present"] is True
    assert block["object"]["vlan"]["mode"] == "trunk"
    assert block["irreversibility"] == "permanent"
    assert block["refusal"]["status"] == "precondition_failed"
    assert block["blockers"][0]["moid"] == "vm-1"
    assert "Destroy_Task" not in conn.methods()


async def test_portgroup_preview_absent_and_without_connector() -> None:
    effect = await _teardown_network.network_portgroup_delete_preview(
        preview_ctx({"portgroup": "dvportgroup-404"}, VimFake())
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["object"]["present"] is False
    assert effect["blast_radius"]["irreversibility"] == "none-object-already-absent"
    assert (
        await _teardown_network.network_portgroup_delete_preview(
            preview_ctx({"portgroup": "dvportgroup-42"}, None)
        )
        is None
    )


@pytest.mark.parametrize("value", ["example-dvpg", "dvportgroup-", "network-12", ""])
def test_portgroup_schema_requires_a_moid(value: str) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA).validate(
            {"portgroup": value}
        )


# ===========================================================================
# host.standard_portgroup.delete
# ===========================================================================


def _host(conn: VimFake, *, vnics: list[dict[str, Any]] | None = None) -> None:
    conn.add(
        "HostSystem",
        "host-21",
        name="esx-a.example.test",
        **{
            "configManager.networkSystem": moref("HostNetworkSystem", "networkSystem-21"),
            "config.network.portgroup": [
                {
                    "key": "key-vim.host.PortGroup-example-trunk",
                    "port": [],
                    "spec": {"name": "example-trunk", "vlanId": 4095, "vswitchName": "vSwitch1"},
                },
                {"key": "k2", "spec": {"name": "Management Network", "vlanId": 0}},
            ],
            "config.network.vnic": vnics or [],
            "vm": [moref("VirtualMachine", "vm-on-host")],
            "network": [
                moref("Network", "network-5"),
                moref("DistributedVirtualPortgroup", "dvportgroup-1"),
            ],
        },
    )
    conn.add("VirtualMachine", "vm-on-host", name="example-vm")
    # Default: the port group's vCenter Network object, no VM attached.
    conn.add("Network", "network-5", name="example-trunk", vm=[])


async def test_standard_portgroup_delete_happy_path(gate: GateRecorder) -> None:
    conn = VimFake()
    _host(conn)
    conn.add(
        "Network", "network-5", name="example-trunk", vm=[moref("VirtualMachine", "vm-elsewhere")]
    )
    out = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-21",
        portgroup_name="example-trunk",
    )
    assert out["status"] == "deleted"
    assert out["object"]["vswitch"] == "vSwitch1"
    assert out["object"]["vlan"] == {"mode": "trunk_all", "vlan_id": 4095}
    assert out["blockers"] == []
    _validate(HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA, out)
    assert [c["op_id"] for c in gate.calls] == [_REMOVE_PG_OP]
    path, body = conn.calls_to("RemovePortGroup")[0]
    assert path == "/HostNetworkSystem/networkSystem-21/RemovePortGroup"
    assert body == {"pgName": "example-trunk"}


async def test_standard_portgroup_delete_refuses_vm_on_this_host(gate: GateRecorder) -> None:
    """A powered-off VM still references the network -- refused, named."""
    conn = VimFake()
    _host(conn)
    conn.add(
        "Network",
        "network-5",
        name="example-trunk",
        vm=[moref("VirtualMachine", "vm-on-host"), moref("VirtualMachine", "vm-elsewhere")],
    )
    out = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-21",
        portgroup_name="example-trunk",
    )
    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [{"kind": "vm", "moid": "vm-on-host", "name": "example-vm"}]
    assert "example-vm" in out["guidance"]
    assert gate.calls == []


async def test_standard_portgroup_delete_refuses_vmkernel_adapter(gate: GateRecorder) -> None:
    conn = VimFake()
    _host(conn, vnics=[{"device": "vmk2", "portgroup": "example-trunk"}])
    conn.add("Network", "network-5", name="example-trunk", vm=[])
    out = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-21",
        portgroup_name="example-trunk",
    )
    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [{"kind": "vmkernel_adapter", "id": "vmk2", "name": None}]
    assert "RemovePortGroup" not in conn.methods()


async def test_standard_portgroup_absent_and_unknown_host(gate: GateRecorder) -> None:
    conn = VimFake()
    _host(conn)
    out = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-21",
        portgroup_name="gone",
    )
    assert out["status"] == "unchanged"
    missing = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-99",
        portgroup_name="example-trunk",
    )
    assert missing["status"] == "invalid_request"
    assert gate.calls == []
    _validate(HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA, missing)


async def test_standard_portgroup_fault_raises(gate: GateRecorder) -> None:
    conn = VimFake()
    _host(conn)
    conn.sync_fault = http_status_error(500, "ResourceInUse")
    with pytest.raises(Exception, match="500"):
        await _call(
            _teardown_network.host_standard_portgroup_delete_composite,
            conn,
            host="host-21",
            portgroup_name="example-trunk",
        )


async def test_standard_portgroup_still_present(gate: GateRecorder) -> None:
    conn = VimFake()
    _host(conn)
    conn.apply_writes = False
    out = await _call(
        _teardown_network.host_standard_portgroup_delete_composite,
        conn,
        host="host-21",
        portgroup_name="example-trunk",
    )
    assert out["status"] == "still_present"


async def test_standard_portgroup_preview() -> None:
    conn = VimFake()
    _host(conn)
    effect = await _teardown_network.host_standard_portgroup_delete_preview(
        preview_ctx({"host": "host-21", "portgroup_name": "example-trunk"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    obj = effect["blast_radius"]["object"]
    assert obj["host_name"] == "esx-a.example.test"
    assert obj["vlan"]["vlan_id"] == 4095
    assert "RemovePortGroup" not in conn.methods()


@pytest.mark.parametrize(
    "params",
    [
        {"host": "esx-a", "portgroup_name": "x"},
        {"host": "host-21"},
        {"host": "host-21", "portgroup_name": ""},
    ],
)
def test_standard_portgroup_schema_rejects(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA).validate(params)


# ===========================================================================
# folder.delete
# ===========================================================================


def _folder(
    conn: VimFake, *, children: list[tuple[str, str]] | None = None, parent: str = "Folder"
) -> None:
    conn.add(
        "Folder",
        "group-v100",
        name="example-folder",
        childType=["Folder", "VirtualMachine", "VirtualApp"],
        childEntity=[moref(t, m) for t, m in (children or [])],
        parent=moref(parent, "group-v3" if parent == "Folder" else "datacenter-1"),
    )


async def test_folder_delete_happy_path(gate: GateRecorder) -> None:
    conn = VimFake()
    _folder(conn)
    out = await _call(_teardown.folder_delete_composite, conn, folder="group-v100")
    assert out["status"] == "deleted"
    assert out["object"]["child_count"] == 0
    assert [c["op_id"] for c in gate.calls] == [_FOLDER_DESTROY_OP]
    assert conn.calls_to("Destroy_Task")[0][0] == "/Folder/group-v100/Destroy_Task"
    _validate(FOLDER_DELETE_RESPONSE_SCHEMA, out)


async def test_folder_delete_refuses_non_empty(gate: GateRecorder) -> None:
    conn = VimFake()
    _folder(conn, children=[("VirtualMachine", "vm-7"), ("Folder", "group-v101")])
    conn.add("VirtualMachine", "vm-7", name="example-vm")
    conn.add("Folder", "group-v101", name="nested")
    out = await _call(_teardown.folder_delete_composite, conn, folder="group-v100")
    assert out["status"] == "precondition_failed"
    assert {(b["kind"], b["name"]) for b in out["blockers"]} == {
        ("VirtualMachine", "example-vm"),
        ("Folder", "nested"),
    }
    assert "not empty" in out["guidance"]
    assert gate.calls == []


async def test_folder_delete_refuses_datacenter_system_folder(gate: GateRecorder) -> None:
    conn = VimFake()
    _folder(conn, parent="Datacenter")
    out = await _call(_teardown.folder_delete_composite, conn, folder="group-v100")
    assert out["status"] == "precondition_failed"
    assert "system folder" in out["guidance"]
    assert gate.calls == []


async def test_folder_delete_absent_unchanged_and_fault_raises(gate: GateRecorder) -> None:
    conn = VimFake()
    out = await _call(_teardown.folder_delete_composite, conn, folder="group-v404")
    assert out["status"] == "unchanged"
    _folder(conn)
    conn.task_faults["Destroy_Task"] = ("NotSupported", "The operation is not supported")
    with pytest.raises(RuntimeError, match="not supported"):
        await _call(_teardown.folder_delete_composite, conn, folder="group-v100")


async def test_folder_preview_lists_children() -> None:
    conn = VimFake()
    _folder(conn, children=[("VirtualMachine", "vm-7")])
    conn.add("VirtualMachine", "vm-7", name="example-vm")
    effect = await _teardown.folder_delete_preview(preview_ctx({"folder": "group-v100"}, conn))
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["blockers"] == [
        {"kind": "VirtualMachine", "moid": "vm-7", "name": "example-vm"}
    ]
    assert effect["blast_radius"]["refusal"]["status"] == "precondition_failed"


def test_folder_schema_requires_a_moid() -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(FOLDER_DELETE_PARAMETER_SCHEMA).validate({"folder": "example-folder"})
    Draft202012Validator(FOLDER_DELETE_PARAMETER_SCHEMA).validate({"folder": "group-v100"})
