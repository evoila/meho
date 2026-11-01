# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A recording fake of the vCenter connector for the vSAN change tests.

It extends the shared vim fake (``tests._vmware_teardown_fake.VimFake``) with
the vSAN seam (``_post_vsan`` and ``_vsan_needs_soap``) and the host disk
calls: ``HostVsanSystem.QueryDisksForVsan`` (on the 8.0 and the 9.x path),
``HostStorageSystem.RetrieveDiskPartitionInfo`` and ``UpdateDiskPartitions``.
The changes alter the fake's disks the way vCenter would, so the reads after
a change see the result. All names and ids are made up.
"""

from __future__ import annotations

import copy
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from tests._vmware_teardown_fake import VimFake, moref

CLUSTER = "domain-c7"
HOST = "host-12"
VSAN_SYSTEM = "vsanSystem-12"
STORAGE_SYSTEM = "storageSystem-12"


def scsi_disk(name: str, *, ssd: bool = True, blocks: int = 1000) -> dict[str, Any]:
    """A small ``HostScsiDisk`` in the VI-JSON form."""
    return {
        "_typeName": "HostScsiDisk",
        "deviceName": f"/vmfs/devices/disks/{name}",
        "deviceType": "disk",
        "key": f"key-vim.host.ScsiDisk-{name}",
        "uuid": f"uuid-{name}",
        "canonicalName": name,
        "displayName": f"Local disk ({name})",
        "lunType": "disk",
        "operationalState": ["ok"],
        "capacity": {"_typeName": "HostDiskDimensionsLba", "blockSize": 512, "block": blocks},
        "devicePath": f"/vmfs/devices/disks/{name}",
        "ssd": ssd,
    }


class VsanFake(VimFake):
    """The vim fake plus the vSAN seam and the host disk calls."""

    def __init__(self, *, soap: bool = False) -> None:
        super().__init__()
        #: ``True`` behaves like vCenter 8.0 (vSAN as SOAP, the 8.0 VI-JSON paths).
        self.soap = soap
        #: canonical name -> ``{"disk": HostScsiDisk, "state": ...}``.
        self.disks: dict[str, dict[str, Any]] = {}
        #: device path -> partition rows (``HostDiskPartitionAttributes``).
        self.partitions: dict[str, list[dict[str, Any]]] = {}
        #: disk -> ``eligible`` value of ``VsanHostQueryWipeDisk``.
        self.wipe_eligible: dict[str, str] = {}
        #: ``(method, body)`` of every vSAN call, in order.
        self.vsan_calls: list[tuple[VsanMethod, Any]] = []
        #: device path -> error raised by ``UpdateDiskPartitions``.
        self.partition_faults: dict[str, Exception] = {}
        self.vsan_fault: Exception | None = None

    # -- set-up helpers ------------------------------------------------------
    def add_cluster(self, *, recent: list[str] | None = None) -> None:
        self.add(
            "ClusterComputeResource",
            CLUSTER,
            name="cluster-a",
            recentTask=[moref("Task", task) for task in recent or []],
        )

    def add_host(self, *, vmfs_disks: list[str] | None = None) -> None:
        self.add(
            "HostSystem",
            HOST,
            **{
                "name": "esxi-a",
                "configManager.vsanSystem": moref("HostVsanSystem", VSAN_SYSTEM),
                "configManager.storageSystem": moref("HostStorageSystem", STORAGE_SYSTEM),
            },
        )
        mounts = [
            {
                "_typeName": "HostFileSystemMountInfo",
                "volume": {
                    "_typeName": "HostVmfsVolume",
                    "name": f"datastore-on-{disk}",
                    "extent": [{"_typeName": "HostScsiDiskPartition", "diskName": disk}],
                },
            }
            for disk in vmfs_disks or []
        ]
        self.add(
            "HostStorageSystem",
            STORAGE_SYSTEM,
            fileSystemVolumeInfo={"_typeName": "HostFileSystemVolumeInfo", "mountInfo": mounts},
        )

    def add_disk(
        self, name: str, state: str = "eligible", *, partitions: int = 0, ssd: bool = True
    ) -> None:
        disk = scsi_disk(name, ssd=ssd)
        self.disks[name] = {"_typeName": "VsanHostDiskResult", "disk": disk, "state": state}
        self.partitions[disk["devicePath"]] = [
            {"partition": n, "type": "vmfs", "startSector": 2048 * n, "endSector": 2048 * n + 99}
            for n in range(1, partitions + 1)
        ]
        self.wipe_eligible[name] = "Yes"

    def running_task(self, task: str, description: str) -> None:
        """A task that still runs on the cluster, with a description id."""
        self._tasks[task] = {"state": "running", "descriptionId": description}

    # -- the vSAN seam --------------------------------------------------------
    async def _vsan_needs_soap(self, target: Any, operator: Operator) -> bool:
        return self.soap

    async def _post_vsan(
        self, target: Any, vsan_method: VsanMethod, *, operator: Operator, json: Any
    ) -> Any:
        self.vsan_calls.append((vsan_method, copy.deepcopy(json)))
        method = vsan_method.method
        if method == "VsanHostQueryWipeDisk":
            return [
                {
                    "_typeName": "VsanHostWipeDiskStatus",
                    "disk": disk,
                    "eligible": self.wipe_eligible.get(disk, "No"),
                    "wipeState": "Success" if self._wiped(disk) else None,
                }
                for disk in json["disks"]
            ]
        if self.vsan_fault is not None:
            raise self.vsan_fault
        if method == "VsanClusterReconfig":
            return self._task(method, apply=lambda: None)
        if method == "VsanEncryptedClusterRekey_Task":
            return self._task(method, apply=lambda: None)
        if method == "VsanAddStoragePoolDisk":
            names = [d["diskName"] for spec in json["specs"] for d in spec["disks"]]
            return self._task(method, apply=lambda: self._claim(names))
        if method == "InitializeDiskMappings":
            spec = json["spec"]
            names = [d["canonicalName"] for d in spec["cacheDisks"] + spec["capacityDisks"]]
            return self._task(method, apply=lambda: self._claim(names))
        if method == "VsanHostWipeDisk":
            return self._task(method, apply=lambda: self._wipe(json["disks"]))
        raise AssertionError(f"unexpected vSAN call {vsan_method}")

    # -- the VI-JSON seam -----------------------------------------------------
    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        method = path.rsplit("/", 1)[-1]
        if method == "QueryDisksForVsan":
            self.calls.append((path, copy.deepcopy(json)))
            expected = "/HostVsanSystem/" if self.soap else "/vsan/HostVsanSystem/"
            assert path.startswith(expected), path
            return [copy.deepcopy(self.disks[n]) for n in json["canonicalName"] if n in self.disks]
        if method == "RetrieveDiskPartitionInfo":
            self.calls.append((path, copy.deepcopy(json)))
            return [
                {
                    "_typeName": "HostDiskPartitionInfo",
                    "deviceName": device,
                    "spec": {
                        "_typeName": "HostDiskPartitionSpec",
                        "partition": copy.deepcopy(self.partitions.get(device, [])),
                    },
                }
                for device in json["devicePath"]
            ]
        if method == "UpdateDiskPartitions":
            self.calls.append((path, copy.deepcopy(json)))
            device = json["devicePath"]
            if device in self.partition_faults:
                raise self.partition_faults[device]
            self.partitions[device] = []
            return None
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
            session_used=session_used,
            session_token=session_token,
        )

    # -- effects ---------------------------------------------------------------
    def _claim(self, names: list[str]) -> None:
        for name in names:
            self.disks[name]["state"] = "inUse"

    def _wipe(self, names: list[str]) -> None:
        for name in names:
            self.partitions[self.disks[name]["disk"]["devicePath"]] = []

    def _wiped(self, name: str) -> bool:
        entry = self.disks.get(name)
        return entry is not None and not self.partitions.get(entry["disk"]["devicePath"])

    def vsan_methods(self) -> list[str]:
        return [method.method for method, _ in self.vsan_calls]
