# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim fake for the VM move and copy tests (``vm.relocate``, ``vm.clone_from_vm``).

The teardown vim fake plus ``RelocateVM_Task`` and ``CloneVM_Task``. A start
records the call and returns a new task that runs (vCenter took it). The
tests set the task state by hand to stand in for vCenter finishing the job.
"""

from __future__ import annotations

import copy
from typing import Any

from meho_backplane.auth.operator import Operator
from tests._vmware_teardown_fake import VimFake, moref

VM = "vm-42"
FOLDER = "group-v3"


class MoveCopyVimFake(VimFake):
    """The teardown vim fake plus the two VM task methods."""

    def __init__(self) -> None:
        super().__init__()
        self.start_fault: Exception | None = None

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
        if method in ("RelocateVM_Task", "CloneVM_Task"):
            self.calls.append((path, copy.deepcopy(json)))
            if self.start_fault is not None:
                raise self.start_fault
            moid = f"task-{len(self._tasks) + 1}"
            self._tasks[moid] = {"state": "running", "progress": 5}
            return moref("Task", moid)
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
            session_used=session_used,
            session_token=session_token,
        )

    def task(self, moid: str, **info: Any) -> None:
        """Set the ``TaskInfo`` of task *moid* (a start made it, or a test adds one)."""
        self._tasks[moid] = info

    def starts(self) -> list[tuple[str, Any]]:
        return self.calls_to("RelocateVM_Task") + self.calls_to("CloneVM_Task")


def disk(key: int, datastore: str) -> dict[str, Any]:
    """A ``VirtualDisk`` device on *datastore*."""
    return {
        "_typeName": "VirtualDisk",
        "key": key,
        "backing": {
            "_typeName": "VirtualDiskFlatVer2BackingInfo",
            "datastore": moref("Datastore", datastore),
        },
    }


def seed_vm(conn: MoveCopyVimFake, **extra: Any) -> None:
    """A running VM on host-21, pool resgroup-8, folder group-v3, datastore-11."""
    props: dict[str, Any] = {
        "name": "<vm-name>",
        "runtime.powerState": "poweredOn",
        "runtime.host": moref("HostSystem", "host-21"),
        "resourcePool": moref("ResourcePool", "resgroup-8"),
        "parent": moref("Folder", FOLDER),
        "datastore": [moref("Datastore", "datastore-11")],
        "config.hardware.device": [
            {"_typeName": "VirtualE1000e", "key": 4000},
            disk(2000, "datastore-11"),
            disk(2001, "datastore-11"),
        ],
        "config.template": False,
        "recentTask": [],
        **extra,
    }
    conn.add("VirtualMachine", VM, **props)
    conn.add("Folder", FOLDER, childEntity=[moref("VirtualMachine", VM)])
