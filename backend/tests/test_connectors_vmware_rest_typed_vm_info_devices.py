# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the disks, network adapters and guest file systems of ``vmware.vm.info``.

* the parsers of :mod:`~meho_backplane.connectors.vmware_rest.vm_devices` on a
  hand-written ``config.hardware.device`` list and ``guest.disk`` list, as
  VI-JSON returns them;
* :func:`vm_info_impl` reads the two properties in its one property read, and
  reads the names of distributed port groups in a second, best-effort read.

All names and ids are made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import (
    VMWARE_VM_INFO_OP,
    build_vm_info_retrieve_params,
    vm_info_impl,
)
from meho_backplane.connectors.vmware_rest.vm_devices import (
    parse_disks,
    parse_filesystems,
    parse_nics,
    portgroup_moids,
)


def _operator() -> Operator:
    return Operator(
        sub="op-vm-info-devices",
        name="VM Info Devices Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a3"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _ref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


_DEVICES: dict[str, Any] = {
    "_typeName": "ArrayOfVirtualDevice",
    "_value": [
        {
            "_typeName": "ParaVirtualSCSIController",
            "key": 1000,
            "deviceInfo": {"label": "SCSI controller 0"},
        },
        {
            "_typeName": "VirtualDisk",
            "key": 2000,
            "deviceInfo": {"label": "Hard disk 1"},
            "controllerKey": 1000,
            "unitNumber": 0,
            "capacityInBytes": 21474836480,
            "backing": {
                "_typeName": "VirtualDiskFlatVer2BackingInfo",
                "fileName": "[ds-1] app-vm/app-vm.vmdk",
                "datastore": _ref("Datastore", "datastore-20"),
                "thinProvisioned": True,
            },
        },
        {
            "_typeName": "VirtualDisk",
            "key": 2001,
            "deviceInfo": {"label": "Hard disk 2"},
            "controllerKey": 1000,
            "unitNumber": 1,
            "capacityInKB": 1024,
            "backing": {
                "_typeName": "VirtualDiskFlatVer2BackingInfo",
                "fileName": "[ds-1] app-vm/app-vm_1.vmdk",
                "thinProvisioned": False,
                "eagerlyScrub": True,
            },
        },
        {
            "_typeName": "VirtualVmxnet3",
            "key": 4000,
            "deviceInfo": {"label": "Network adapter 1"},
            "macAddress": "00:50:56:00:00:01",
            "connectable": {"connected": True, "startConnected": True},
            "backing": {
                "_typeName": "VirtualEthernetCardDistributedVirtualPortBackingInfo",
                "port": {"portgroupKey": "dvportgroup-30", "switchUuid": "<switch-uuid>"},
            },
        },
        {
            "_typeName": "VirtualE1000e",
            "key": 4001,
            "deviceInfo": {"label": "Network adapter 2"},
            "macAddress": "00:50:56:00:00:02",
            "connectable": {"connected": False, "startConnected": False},
            "backing": {
                "_typeName": "VirtualEthernetCardNetworkBackingInfo",
                "deviceName": "vm-network",
                "network": _ref("Network", "network-31"),
            },
        },
        {"_typeName": "VirtualCdrom", "key": 3000, "deviceInfo": {"label": "CD/DVD drive 1"}},
    ],
}

_GUEST_DISKS = [
    {
        "_typeName": "GuestDiskInfo",
        "diskPath": "/",
        "capacity": 20000,
        "freeSpace": 5000,
        "filesystemType": "ext4",
    }
]


def test_disks_have_size_file_provisioning_and_controller() -> None:
    from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

    disks = parse_disks(unwrap_vim_value(_DEVICES))
    assert disks == [
        {
            "key": 2000,
            "label": "Hard disk 1",
            "capacity_bytes": 21474836480,
            "file": "[ds-1] app-vm/app-vm.vmdk",
            "datastore": "datastore-20",
            "provisioning": "thin",
            "controller": "SCSI controller 0",
            "controller_type": "ParaVirtualSCSIController",
            "unit_number": 0,
        },
        {
            "key": 2001,
            "label": "Hard disk 2",
            "capacity_bytes": 1048576,
            "file": "[ds-1] app-vm/app-vm_1.vmdk",
            "datastore": None,
            "provisioning": "thick_eager_zeroed",
            "controller": "SCSI controller 0",
            "controller_type": "ParaVirtualSCSIController",
            "unit_number": 1,
        },
    ]


def test_nics_have_port_group_mac_and_connection() -> None:
    from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

    devices = unwrap_vim_value(_DEVICES)
    assert portgroup_moids(devices) == ["dvportgroup-30"]
    nics = parse_nics(devices, {"dvportgroup-30": "pg-app"})
    assert [(n["label"], n["network"], n["network_name"], n["connected"]) for n in nics] == [
        ("Network adapter 1", "dvportgroup-30", "pg-app", True),
        ("Network adapter 2", "network-31", "vm-network", False),
    ]
    assert nics[0]["mac_address"] == "00:50:56:00:00:01"
    assert nics[0]["adapter_type"] == "VirtualVmxnet3"


def test_filesystems_and_empty_inputs() -> None:
    assert parse_filesystems(_GUEST_DISKS) == [
        {"mount_point": "/", "filesystem_type": "ext4", "capacity_bytes": 20000, "free_bytes": 5000}
    ]
    assert parse_filesystems(None) == []
    assert parse_disks(None) == []
    assert parse_nics("junk", {}) == []


class _Fake:
    """Answers the VM read and the port group name read; records the calls."""

    def __init__(self, *, fail_names: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_names = fail_names

    async def _post_vmomi_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        del target, path
        body = kwargs["json"]
        self.calls.append(body)
        prop_spec = body["specSet"][0]["propSet"][0]
        if prop_spec["type"] == "DistributedVirtualPortgroup":
            if self.fail_names:
                raise RuntimeError("no permission")
            return {
                "objects": [
                    {
                        "obj": {"type": "DistributedVirtualPortgroup", "value": "dvportgroup-30"},
                        "propSet": [{"name": "name", "val": "pg-app"}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "VirtualMachine", "value": "vm-42"},
                    "propSet": [
                        {"name": "name", "val": "app-vm"},
                        {"name": "config.hardware.device", "val": _DEVICES},
                        {"name": "guest.disk", "val": _GUEST_DISKS},
                    ],
                }
            ]
        }


def test_the_vm_read_asks_for_devices_and_guest_disks() -> None:
    path_set = build_vm_info_retrieve_params("vm-42")["specSet"][0]["propSet"][0]["pathSet"]
    assert "config.hardware.device" in path_set
    assert "guest.disk" in path_set


async def test_vm_info_returns_disks_nics_and_filesystems() -> None:
    fake = _Fake()
    out = await vm_info_impl(fake, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]
    assert [d["label"] for d in out["disks"]] == ["Hard disk 1", "Hard disk 2"]
    assert out["nics"][0]["network_name"] == "pg-app"
    assert out["guest_filesystems"][0]["free_bytes"] == 5000
    assert len(fake.calls) == 2
    Draft202012Validator(VMWARE_VM_INFO_OP.response_schema).validate(out)


async def test_vm_info_keeps_going_when_port_group_names_fail() -> None:
    fake = _Fake(fail_names=True)
    out = await vm_info_impl(fake, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]
    assert out["nics"][0]["network"] == "dvportgroup-30"
    assert out["nics"][0]["network_name"] is None
    assert out["nics"][1]["network_name"] == "vm-network"
