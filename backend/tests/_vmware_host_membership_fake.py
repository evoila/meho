# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim fake for the host membership tests (add, move, connect, disconnect, remove).

:class:`HostVim` is the teardown fake (:class:`tests._vmware_teardown_fake.VimFake`)
plus the five host membership methods. The writes change the fake store, so
the handlers' reads after a task see the result. All names and ids are made up.
"""

from __future__ import annotations

import copy
from typing import Any

from meho_backplane.auth.operator import Operator
from tests._vmware_teardown_fake import VimFake, moref

HOST = "host-1001"
CLUSTER = "domain-c7"
OTHER_CLUSTER = "domain-c8"
STANDALONE = "domain-s9"
FOLDER = "group-h4"
PASSWORD = "<host-password-from-vault>"
THUMBPRINT = "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33"


class HostVim(VimFake):
    """The shared vim fake plus the five host membership methods."""

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
        mo_type, moid, method = path.strip("/").split("/")
        if method in ("AddHost_Task", "AddStandaloneHost_Task"):
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: None)
        if method == "MoveHostInto_Task":
            self.calls.append((path, copy.deepcopy(json)))
            host = json["host"]["value"]
            return self._task(method, apply=lambda: self._set(host, parent=moref(mo_type, moid)))
        if method in ("ReconnectHost_Task", "DisconnectHost_Task"):
            self.calls.append((path, copy.deepcopy(json)))
            state = "connected" if method == "ReconnectHost_Task" else "disconnected"
            return self._task(
                method, apply=lambda: self._set(moid, **{"runtime.connectionState": state})
            )
        if method == "Destroy_Task" and mo_type == "ComputeResource":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._destroy_compute_resource(moid))
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
            session_used=session_used,
            session_token=session_token,
        )

    def _set(self, host: str, **props: Any) -> None:
        self.objects[("HostSystem", host)].update(props)

    def _destroy_compute_resource(self, moid: str) -> None:
        self.objects.pop(("ComputeResource", moid), None)
        for key, props in list(self.objects.items()):
            if key[0] == "HostSystem" and props.get("parent", {}).get("value") == moid:
                self.objects.pop(key)


def seed_host(
    conn: HostVim,
    *,
    parent: dict[str, str] | None = None,
    state: str = "connected",
    in_mm: bool = True,
    vms: tuple[str, ...] = ("vm-1",),
) -> None:
    conn.add(
        "HostSystem",
        HOST,
        name="<esxi-host-name>",
        parent=parent or moref("ClusterComputeResource", CLUSTER),
        vm=[moref("VirtualMachine", vm) for vm in vms],
        **{"runtime.connectionState": state, "runtime.inMaintenanceMode": in_mm},
    )
    for vm in vms:
        conn.add("VirtualMachine", vm, name=f"<vm-name-{vm}>")
    for cluster in (CLUSTER, OTHER_CLUSTER):
        conn.add("ClusterComputeResource", cluster, name=f"<cluster-{cluster}>", recentTask=[])
