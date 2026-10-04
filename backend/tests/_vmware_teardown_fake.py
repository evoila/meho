# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim fake for the #3339 teardown composite tests.

A recording stand-in for the connector's VI-JSON seam
(``_post_vmomi_json``): a small managed-object store answers
``RetrievePropertiesEx`` (incl. ``Task.info`` polls), a per-datastore file tree
answers the ``HostDatastoreBrowser`` searches, and the mutating methods
(``Destroy_Task``, ``RemovePortGroup``, ``DeleteDatastoreFile_Task``,
``MakeDirectory``) change the store so the handlers' read-backs see the
result. ``GateRecorder`` replaces the #2254 sub-op gate.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID

import httpx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.operations._preview import PreviewContext

#: (fault type, message) a method's task -- or a synchronous call -- fails with.
Fault = tuple[str, str]


def operator() -> Operator:
    return Operator(
        sub="op-teardown",
        name="Teardown Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c339"),
        tenant_role=TenantRole.OPERATOR,
    )


def moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def preview_ctx(params: dict[str, Any], connector: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # builders ignore it
        connector_instance=connector,
        operator=operator(),
        target=object(),
        params=params,
    )


class GateRecorder:
    """Stand-in for ``enforce_subop_policy``: records calls, returns a verdict."""

    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


def parked(op_id: str) -> OperationResult:
    return OperationResult(status="awaiting_approval", op_id=op_id, result=None, duration_ms=1.0)


class VimFake:
    """Recording double for the connector's VI-JSON seam."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.files: dict[str, dict[str, int | None]] = {}  # ds name -> {rel path: size|None(dir)}
        self.calls: list[tuple[str, Any]] = []
        self.task_faults: dict[str, Fault] = {}
        self.running: set[str] = set()
        self.sync_fault: Exception | None = None
        self.apply_writes = True
        self.dv_ports: list[dict[str, Any]] | Exception = []
        self.page_token: bool = False
        #: Answer a missing object with a JSON ``VimFault`` HTTP 500 body
        #: instead of the connector-promoted not-found error.
        self.not_found_as_json = False
        self._tasks: dict[str, dict[str, Any]] = {}

    # -- store helpers ---------------------------------------------------
    def add(self, mo_type: str, moid: str, **props: Any) -> None:
        self.objects[(mo_type, moid)] = props

    def methods(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _ in self.calls]

    def calls_to(self, method: str) -> list[tuple[str, Any]]:
        return [(path, body) for path, body in self.calls if path.endswith(f"/{method}")]

    # -- the seam --------------------------------------------------------
    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        self.calls.append((path, copy.deepcopy(json)))
        mo_type, moid, method = path.strip("/").split("/")
        if method == "RetrievePropertiesEx":
            return self._retrieve(json, promote_managed_object_not_found)
        if method == "Destroy_Task":
            return self._task(method, apply=lambda: self.objects.pop((mo_type, moid), None))
        if method == "FetchDVPorts":
            if isinstance(self.dv_ports, Exception):
                raise self.dv_ports
            return copy.deepcopy(self.dv_ports)
        if method == "RemovePortGroup":
            return self._sync(lambda: self._remove_portgroup(moid, json["pgName"]))
        if method in ("SearchDatastore_Task", "SearchDatastoreSubFolders_Task"):
            return self._search(method, json)
        if method == "DeleteDatastoreFile_Task":
            return self._task(method, apply=lambda: self._delete_file(json["name"]))
        if method == "MakeDirectory":
            return self._sync(lambda: self._mkdir(json["name"], json["createParentDirectories"]))
        raise AssertionError(f"unexpected vim call {path}")

    # -- RetrievePropertiesEx ----------------------------------------------
    def _retrieve(self, body: Any, promote: bool) -> dict[str, Any]:
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        path_set = spec["propSet"][0]["pathSet"]
        moids = [o["obj"]["value"] for o in spec["objectSet"]]
        if mo_type == "Task":
            return {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": m},
                        "propSet": [{"name": "info", "val": self._tasks[m]}],
                    }
                    for m in moids
                ]
            }
        objects = []
        for moid in moids:
            props = self.objects.get((mo_type, moid))
            if props is None:
                if self.not_found_as_json:
                    raise http_status_error(
                        500,
                        json.dumps(
                            {"_typeName": "ManagedObjectNotFound", "obj": moref(mo_type, moid)}
                        ),
                    )
                if promote:
                    raise ConnectorResourceNotFoundError([moid], f"{moid} not found")
                raise RuntimeError(f"ManagedObjectNotFound: {mo_type} {moid}")
            objects.append(
                {
                    "obj": {"type": mo_type, "value": moid},
                    "propSet": [
                        {"name": p, "val": copy.deepcopy(props[p])} for p in path_set if p in props
                    ],
                }
            )
        result: dict[str, Any] = {"objects": objects}
        if self.page_token and mo_type == "VirtualMachine":
            result["token"] = "more"
        return result

    # -- tasks -------------------------------------------------------------
    def _task(self, method: str, *, apply: Any, result: Any = None) -> dict[str, str]:
        moid = f"task-{len(self._tasks) + 1}"
        info: dict[str, Any] = {"state": "success", "result": result}
        if method in self.task_faults:
            fault_type, message = self.task_faults[method]
            info = {
                "state": "error",
                "error": {"localizedMessage": message, "fault": {"_typeName": fault_type}},
            }
        elif method in self.running:
            info = {"state": "running"}
        elif self.apply_writes:
            apply()
        self._tasks[moid] = info
        return moref("Task", moid)

    def _sync(self, apply: Any) -> dict[str, Any]:
        if self.sync_fault is not None:
            raise self.sync_fault
        if self.apply_writes:
            apply()
        return {}

    # -- host port groups ----------------------------------------------------
    def _remove_portgroup(self, network_system: str, name: str) -> None:
        for (mo_type, _moid), props in self.objects.items():
            if mo_type != "HostSystem":
                continue
            if props.get("configManager.networkSystem", {}).get("value") != network_system:
                continue
            groups = props.get("config.network.portgroup") or []
            props["config.network.portgroup"] = [g for g in groups if g["spec"]["name"] != name]

    # -- datastore files -----------------------------------------------------
    @staticmethod
    def _split(ds_path: str) -> tuple[str, str]:
        name, _, rest = ds_path[1:].partition("]")
        return name, rest.strip().strip("/")

    def _search(self, method: str, body: dict[str, Any]) -> dict[str, str]:
        name, folder = self._split(body["datastorePath"])
        tree = self.files.get(name, {})
        if folder and tree.get(folder, 0) is not None:
            return self._fault_task("FileNotFound", f"File [{name}] {folder} was not found")
        pattern = (body["searchSpec"].get("matchPattern") or [None])[0]
        folders = [folder]
        if method == "SearchDatastoreSubFolders_Task":
            folders += [
                p for p, size in tree.items() if size is None and p.startswith(folder + "/")
            ]
        results = []
        for current in folders:
            entries = []
            for p, size in sorted(tree.items()):
                parent, _, base = p.rpartition("/")
                if parent != current or (pattern is not None and base != pattern):
                    continue
                entry: dict[str, Any] = {
                    "_typeName": "FolderFileInfo" if size is None else "FileInfo",
                    "path": base,
                }
                if size is not None:
                    entry["fileSize"] = size
                entries.append(entry)
            results.append(
                {
                    "_typeName": "HostDatastoreBrowserSearchResults",
                    "folderPath": f"[{name}] {current}/" if current else f"[{name}]",
                    "file": entries,
                }
            )
        result: Any = results if method == "SearchDatastoreSubFolders_Task" else results[0]
        moid = f"task-{len(self._tasks) + 1}"
        self._tasks[moid] = {"state": "success", "result": result}
        return moref("Task", moid)

    def _fault_task(self, fault_type: str, message: str) -> dict[str, str]:
        moid = f"task-{len(self._tasks) + 1}"
        self._tasks[moid] = {
            "state": "error",
            "error": {"localizedMessage": message, "fault": {"_typeName": fault_type}},
        }
        return moref("Task", moid)

    def _delete_file(self, ds_path: str) -> None:
        name, rel = self._split(ds_path)
        tree = self.files[name]
        for p in [p for p in tree if p == rel or p.startswith(rel + "/")]:
            del tree[p]

    def _mkdir(self, ds_path: str, parents: bool) -> None:
        name, rel = self._split(ds_path)
        tree = self.files.setdefault(name, {})
        parts = rel.split("/")
        parent = "/".join(parts[:-1])
        if not parents and parent and tree.get(parent, 0) is not None:
            raise AssertionError("MakeDirectory without parents on a missing parent")
        for i in range(1, len(parts) + 1):
            tree.setdefault("/".join(parts[:i]), None)


def http_status_error(status: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.example.test/sdk")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request, text=text)
    )
