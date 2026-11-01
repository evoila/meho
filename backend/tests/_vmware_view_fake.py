# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fake vCenter for the reads that go through a VIM container view.

It stands in for the connector's VI-JSON seam (``_post_vmomi_json``) of a real
:class:`VmwareRestConnector` subclass, so the dispatcher can bind the typed
handlers to it. A small object store (``(type, moid) -> properties``, each
object with a ``parent``) answers:

* ``RetrieveServiceContent`` (root folder ``group-d1``);
* ``CreateContainerView`` / ``DestroyView`` (a view of the objects of some
  types below a container; an unknown container fails like vCenter);
* ``RetrievePropertiesEx`` on a view or on listed objects, split into pages
  (``tests._vim_pages.PagedVim``), and ``ContinueRetrievePropertiesEx``;
* ``SearchIndex.FindByInventoryPath``.

All names and ids are made up.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from tests._vim_pages import PagedVim

ROOT = "group-d1"


def moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


class ViewFake(PagedVim, VmwareRestConnector):
    """Answers the view reads from an in-memory inventory."""

    def __init__(self) -> None:
        PagedVim.__init__(self)
        VmwareRestConnector.__init__(self, session_loader=AsyncMock())
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.views: dict[str, dict[str, Any]] = {}
        self.destroyed: list[str] = []
        #: ``mo_type -> exception`` a direct (non-view) read of that type raises.
        self.fail_reads: dict[str, Exception] = {}
        #: ``(type, moid) -> property paths`` vCenter "could not read" (missingSet).
        self.unreadable: dict[tuple[str, str], set[str]] = {}
        self.add("Folder", ROOT, name="Datacenters")

    # -- store ------------------------------------------------------------
    def add(
        self, mo_type: str, moid: str, *, parent: tuple[str, str] | None = None, **props: Any
    ) -> None:
        if parent is not None:
            props["parent"] = moref(*parent)
        self.objects[(mo_type, moid)] = props

    def _below(self, container: str, mo_type: str, moid: str) -> bool:
        """Is the object below *container*: in its folder tree, or on a host below it?"""
        props = self.objects[(mo_type, moid)]
        starts = [props.get("parent"), props.get("summary.runtime.host")]
        for current in starts:
            for _ in range(32):
                if not isinstance(current, dict):
                    break
                if current["value"] == container:
                    return True
                current = self.objects.get((current["type"], current["value"]), {}).get("parent")
        return False

    def methods(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _ in self.calls]

    # -- the seam -----------------------------------------------------------
    async def _post_vmomi_json(  # type: ignore[override]
        self,
        target: Any,
        vmomi_path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        self.calls.append((vmomi_path, copy.deepcopy(json)))
        self._record_session(session_used)
        _mo_type, moid, method = vmomi_path.strip("/").split("/")
        if method == "RetrieveServiceContent":
            return {
                "_typeName": "ServiceContent",
                "rootFolder": moref("Folder", ROOT),
                "viewManager": moref("ViewManager", "ViewManager"),
                "searchIndex": moref("SearchIndex", "SearchIndex"),
            }
        if method == "CreateContainerView":
            container = json["container"]
            if (container["type"], container["value"]) not in self.objects:
                raise ConnectorResourceNotFoundError([container["value"]], "not found")
            view = f"session[0]view-{len(self.views) + 1}"
            self.views[view] = json
            return moref("ContainerView", view)
        if method == "DestroyView":
            self.destroyed.append(moid)
            return None
        if method == "RetrievePropertiesEx":
            return self._retrieve(json)
        if method == "ContinueRetrievePropertiesEx":
            return self._continue(json["token"], session_token)
        if method == "FindByInventoryPath":
            return self._find_by_path(json["inventoryPath"])
        raise AssertionError(f"unexpected vim call {vmomi_path}")

    def _retrieve(self, body: dict[str, Any]) -> dict[str, Any]:
        spec = body["specSet"][0]
        prop_spec = spec["propSet"][0]
        path_set = prop_spec["pathSet"]
        first = spec["objectSet"][0]["obj"]
        if first["type"] == "ContainerView":
            view = self.views[first["value"]]
            container = view["container"]["value"]
            types = view["type"]
            refs = [
                (t, m)
                for (t, m) in self.objects
                if (not types or t in types) and self._below(container, t, m)
            ]
        else:
            mo_type = prop_spec["type"]
            if mo_type in self.fail_reads:
                raise self.fail_reads[mo_type]
            refs = [(o["obj"]["type"], o["obj"]["value"]) for o in spec["objectSet"]]
            refs = [r for r in refs if r in self.objects]
        objects = []
        for t, m in refs:
            unreadable = self.unreadable.get((t, m), set())
            entry: dict[str, Any] = {
                "obj": {"type": t, "value": m},
                "propSet": [
                    {"name": p, "val": copy.deepcopy(self.objects[(t, m)][p])}
                    for p in path_set
                    if p in self.objects[(t, m)] and p not in unreadable
                ],
            }
            if unreadable & set(path_set):
                entry["missingSet"] = [
                    {"path": p, "fault": {"_typeName": "NoPermission"}}
                    for p in path_set
                    if p in unreadable
                ]
            objects.append(entry)
        return self._serve_objects(objects)

    def _path_of(self, mo_type: str, moid: str) -> str:
        parts: list[str] = []
        key: tuple[str, str] | None = (mo_type, moid)
        while key is not None and key[1] != ROOT:
            props = self.objects[key]
            parts.append(props["name"].replace("/", "%2f"))
            parent = props.get("parent")
            key = (parent["type"], parent["value"]) if parent else None
        return "/".join(reversed(parts))

    def _find_by_path(self, path: str) -> Any:
        wanted = path.strip("/")
        for mo_type, moid in self.objects:
            if moid != ROOT and self._path_of(mo_type, moid) == wanted:
                return moref(mo_type, moid)
        return None


def small_inventory(fake: ViewFake) -> None:
    """One datacenter, one cluster with two hosts, a folder and three VMs."""
    fake.add("Datacenter", "datacenter-2", parent=("Folder", ROOT), name="dc-a")
    fake.add("Folder", "group-v3", parent=("Datacenter", "datacenter-2"), name="vm")
    fake.add("Folder", "group-h4", parent=("Datacenter", "datacenter-2"), name="host")
    fake.add("Folder", "group-v10", parent=("Folder", "group-v3"), name="apps/prod")
    fake.add("ClusterComputeResource", "domain-c7", parent=("Folder", "group-h4"), name="cl-1")
    fake.add("HostSystem", "host-11", parent=("ClusterComputeResource", "domain-c7"), name="esx-1")
    fake.add("HostSystem", "host-12", parent=("ClusterComputeResource", "domain-c7"), name="esx-2")
    fake.add("Folder", "group-s5", parent=("Datacenter", "datacenter-2"), name="datastore")
    fake.add("Datastore", "datastore-20", parent=("Folder", "group-s5"), name="ds-1")
    for moid, name, folder, host, power in (
        ("vm-101", "web-01", "group-v10", "host-11", "poweredOn"),
        ("vm-102", "web-02", "group-v10", "host-12", "poweredOff"),
        ("vm-103", "db-01", "group-v3", "host-12", "poweredOn"),
    ):
        fake.add("VirtualMachine", moid, parent=("Folder", folder), **vm_props(name, host, power))


def vm_props(name: str, host: str, power: str, **extra: Any) -> dict[str, Any]:
    """The properties ``vmware.vm.list`` reads, as VI-JSON returns them (some boxed)."""
    props: dict[str, Any] = {
        "name": name,
        "summary.runtime.powerState": {"_typeName": "VirtualMachinePowerState", "_value": power},
        "summary.runtime.host": moref("HostSystem", host),
        "summary.config.guestFullName": "Ubuntu Linux (64-bit)",
        "summary.config.numCpu": {"_typeName": "int", "_value": 2},
        "summary.config.memorySizeMB": 4096,
        "summary.config.template": False,
        "summary.guest.toolsStatus": "toolsOk",
        "summary.storage.committed": 10_000,
        "summary.storage.uncommitted": 5_000,
    }
    if power == "poweredOn":
        props["summary.guest.ipAddress"] = "<guest-ip>"
    props.update(extra)
    return props
