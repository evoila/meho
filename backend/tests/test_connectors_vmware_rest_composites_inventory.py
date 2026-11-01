# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Inventory-tree changes and the resource pool update (vmware-rest composites).

* ``vmware.composite.datacenter.create`` (caution) and ``.datacenter.delete``
  (destructive);
* ``vmware.composite.object.rename`` (caution) and ``.object.move``
  (dangerous);
* ``vmware.composite.resource_pool.update`` (dangerous, REST).

The vim calls run on a recording fake of the connector's VI-JSON seam
(``tests._vmware_teardown_fake.VimFake``). The respx tests check that the
calls land on the vCenter 8 (``/sdk/vim25/8.0.3.0``) and vCenter 9
(``/sdk/vim25/9.0.0.0``) paths. The dispatcher tests check registration on
both catalogs, the approval wait and the preview-hash rule of the delete.
Every name and id is made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _inventory_tree,
    _object_rename_move,
    _resource_pool_update,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._inventory_schemas import (
    COMPOSITES as INVENTORY_COMPOSITES,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, blast_radius_missing_reason
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_DC_CREATE = "vmware.composite.datacenter.create"
_DC_DELETE = "vmware.composite.datacenter.delete"
_RENAME = "vmware.composite.object.rename"
_MOVE = "vmware.composite.object.move"
_POOL_UPDATE = "vmware.composite.resource_pool.update"
_FOLDER_CREATE = "vmware.composite.folder.create"


class InventoryFake(VimFake):
    """The vim fake plus ServiceContent, CreateDatacenter, Rename and MoveIntoFolder."""

    def __init__(self) -> None:
        super().__init__()
        self.next_dc = 900

    async def _post_vmomi_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        mo_type, moid, method = path.strip("/").split("/")
        body = kwargs.get("json")
        if method == "RetrieveServiceContent":
            self.calls.append((path, body))
            return {"rootFolder": moref("Folder", "group-d1")}
        if method == "CreateDatacenter":
            self.calls.append((path, body))
            if self.sync_fault is not None:
                raise self.sync_fault
            return self._create_datacenter(moid, body["name"])
        if method == "Rename_Task":
            self.calls.append((path, body))
            return self._task(method, apply=lambda: self._rename(mo_type, moid, body["newName"]))
        if method == "MoveIntoFolder_Task":
            self.calls.append((path, body))
            if self.sync_fault is not None:
                raise self.sync_fault
            return self._task(method, apply=lambda: self._move(moid, body["list"]))
        return await super()._post_vmomi_json(target, path, **kwargs)

    def _create_datacenter(self, parent: str, name: str) -> dict[str, str]:
        self.next_dc += 1
        n = self.next_dc
        dc = f"datacenter-{n}"
        folders = {
            "vmFolder": f"group-v{n}",
            "hostFolder": f"group-h{n}",
            "datastoreFolder": f"group-s{n}",
            "networkFolder": f"group-n{n}",
        }
        self.add(
            "Datacenter",
            dc,
            name=name,
            parent=moref("Folder", parent),
            **{prop: moref("Folder", moid) for prop, moid in folders.items()},
        )
        return moref("Datacenter", dc)

    def _rename(self, mo_type: str, moid: str, name: str) -> None:
        self.objects[(mo_type, moid)]["name"] = name

    def _move(self, folder: str, refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            self.objects[(ref["type"], ref["value"])]["parent"] = moref("Folder", folder)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _vim_fault(type_name: str, message: str) -> httpx.HTTPStatusError:
    return http_status_error(500, json.dumps({"_typeName": type_name, "faultstring": message}))


def _datacenter(conn: InventoryFake, moid: str = "datacenter-3", name: str = "<dc>") -> None:
    conn.add(
        "Datacenter",
        moid,
        name=name,
        parent=moref("Folder", "group-d1"),
        vmFolder=moref("Folder", "group-v4"),
        hostFolder=moref("Folder", "group-h5"),
        datastoreFolder=moref("Folder", "group-s6"),
        networkFolder=moref("Folder", "group-n7"),
    )
    for folder in ("group-v4", "group-h5", "group-s6", "group-n7"):
        conn.add("Folder", folder, childEntity=[])


async def _run(handler: Any, conn: InventoryFake, **params: Any) -> Any:
    return await handler(operator=operator(), target=object(), params=params, connector=conn)


# ===========================================================================
# datacenter.create
# ===========================================================================


async def test_datacenter_create_in_the_root_folder_reads_it_back(gate: GateRecorder) -> None:
    conn = InventoryFake()
    out = await _run(_inventory_tree.datacenter_create_composite, conn, name="<dc-name>")

    assert out["status"] == "created"
    assert out["parent_folder_id"] == "group-d1"
    assert out["datacenter"] == "datacenter-901"
    assert out["datacenter_info"] == {
        "datacenter": "datacenter-901",
        "name": "<dc-name>",
        "parent_folder_id": "group-d1",
        "vm_folder_id": "group-v901",
        "host_folder_id": "group-h901",
        "datastore_folder_id": "group-s901",
        "network_folder_id": "group-n901",
    }
    assert conn.calls_to("CreateDatacenter") == [
        ("/Folder/group-d1/CreateDatacenter", {"name": "<dc-name>"})
    ]
    assert [c["op_id"] for c in gate.calls] == ["POST:/Folder/{moId}/CreateDatacenter"]


async def test_datacenter_create_in_a_named_folder_skips_the_root_read(gate: GateRecorder) -> None:
    conn = InventoryFake()
    out = await _run(
        _inventory_tree.datacenter_create_composite,
        conn,
        name="<dc-name>",
        parent_folder_id="group-d20",
    )
    assert out["parent_folder_id"] == "group-d20"
    assert conn.calls_to("RetrieveServiceContent") == []
    assert conn.calls_to("CreateDatacenter")[0][0] == "/Folder/group-d20/CreateDatacenter"


async def test_datacenter_create_vendor_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = InventoryFake()
    conn.sync_fault = _vim_fault("DuplicateName", "The name '<dc-name>' already exists.")
    with pytest.raises(RuntimeError, match=r"DuplicateName.*already exists"):
        await _run(_inventory_tree.datacenter_create_composite, conn, name="<dc-name>")


async def test_datacenter_create_parked_gate_sends_nothing() -> None:
    conn = InventoryFake()
    gate = GateRecorder(parked("POST:/Folder/{moId}/CreateDatacenter"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", gate)
        out = await _run(_inventory_tree.datacenter_create_composite, conn, name="<dc-name>")
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert conn.calls_to("CreateDatacenter") == []


async def test_datacenter_create_preview_echoes_the_name() -> None:
    builder = _PREVIEW_BUILDERS[_DC_CREATE]
    effect = await builder(preview_ctx({"name": "<dc-name>"}, InventoryFake()))
    assert effect == {
        "summary": "Create the datacenter '<dc-name>' in the root folder.",
        "name": "<dc-name>",
        "parent_folder_id": None,
    }


# ===========================================================================
# datacenter.delete
# ===========================================================================


async def test_datacenter_delete_of_an_empty_datacenter(gate: GateRecorder) -> None:
    conn = InventoryFake()
    _datacenter(conn)
    out = await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="datacenter-3")

    assert out["status"] == "deleted"
    assert out["object"]["name"] == "<dc>"
    assert out["object"]["child_count"] == 0
    assert out["task_state"] == "success"
    assert conn.calls_to("Destroy_Task") == [("/Datacenter/datacenter-3/Destroy_Task", {})]
    assert ("Datacenter", "datacenter-3") not in conn.objects


async def test_datacenter_delete_refuses_a_datacenter_that_holds_objects(
    gate: GateRecorder,
) -> None:
    conn = InventoryFake()
    _datacenter(conn)
    conn.objects[("Folder", "group-h5")]["childEntity"] = [
        moref("ClusterComputeResource", "domain-c8")
    ]
    conn.objects[("Folder", "group-v4")]["childEntity"] = [moref("VirtualMachine", "vm-9")]
    conn.add("ClusterComputeResource", "domain-c8", name="<cluster>")
    conn.add("VirtualMachine", "vm-9", name="<vm>")

    out = await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="datacenter-3")

    assert out["status"] == "precondition_failed"
    assert out["object"]["child_count"] == 2
    assert {(b["kind"], b["moid"], b["name"]) for b in out["blockers"]} == {
        ("ClusterComputeResource", "domain-c8", "<cluster>"),
        ("VirtualMachine", "vm-9", "<vm>"),
    }
    assert conn.calls_to("Destroy_Task") == []
    assert gate.calls == []


async def test_datacenter_delete_refuses_on_a_partial_answer(gate: GateRecorder) -> None:
    conn = InventoryFake()
    _datacenter(conn)
    conn.unreadable[("Folder", "group-s6")] = {"childEntity"}
    out = await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="datacenter-3")
    assert out["status"] == "precondition_failed"
    assert "partial answer" in out["guidance"]
    assert conn.calls_to("Destroy_Task") == []


async def test_datacenter_delete_of_a_missing_datacenter_is_unchanged(gate: GateRecorder) -> None:
    conn = InventoryFake()
    out = await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="datacenter-3")
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_datacenter_delete_bad_id_is_an_invalid_request(gate: GateRecorder) -> None:
    conn = InventoryFake()
    out = await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="group-d1")
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_datacenter_delete_task_fault_raises(gate: GateRecorder) -> None:
    conn = InventoryFake()
    _datacenter(conn)
    conn.task_faults["Destroy_Task"] = ("ResourceInUse", "The datacenter is in use.")
    with pytest.raises(RuntimeError, match="The datacenter is in use"):
        await _run(_inventory_tree.datacenter_delete_composite, conn, datacenter="datacenter-3")


async def test_datacenter_delete_preview_is_the_blast_radius() -> None:
    conn = InventoryFake()
    _datacenter(conn)
    effect = await _PREVIEW_BUILDERS[_DC_DELETE](preview_ctx({"datacenter": "datacenter-3"}, conn))
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["moid"] == "datacenter-3"
    assert block["object"]["present"] is True
    children = {c["moid"] for c in block["children"]}
    assert children == {"group-v4", "group-h5", "group-s6", "group-n7"}
    assert block["irreversibility"] == "permanent"
    assert "refusal" not in block


async def test_datacenter_delete_preview_shows_the_refusal_of_a_full_datacenter() -> None:
    conn = InventoryFake()
    _datacenter(conn)
    conn.objects[("Folder", "group-v4")]["childEntity"] = [moref("VirtualMachine", "vm-9")]
    effect = await _PREVIEW_BUILDERS[_DC_DELETE](preview_ctx({"datacenter": "datacenter-3"}, conn))
    assert effect["blast_radius"]["refusal"]["status"] == "precondition_failed"
    assert effect["blast_radius"]["blockers"][0]["moid"] == "vm-9"


async def test_datacenter_delete_preview_refuses_a_missing_datacenter() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await _PREVIEW_BUILDERS[_DC_DELETE](
            preview_ctx({"datacenter": "datacenter-3"}, InventoryFake())
        )


# ===========================================================================
# object.rename
# ===========================================================================


async def test_rename_a_vm_reads_the_name_back(gate: GateRecorder) -> None:
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<old-name>")
    out = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="vm-42",
        new_name="<new-name>",
    )
    assert out["status"] == "renamed"
    assert out["old_name"] == "<old-name>"
    assert out["name_after"] == "<new-name>"
    assert conn.calls_to("Rename_Task") == [
        ("/VirtualMachine/vm-42/Rename_Task", {"newName": "<new-name>"})
    ]
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualMachine/{moId}/Rename_Task"]


@pytest.mark.parametrize(
    ("kind", "mo_type", "moid"),
    [
        ("folder", "Folder", "group-v4"),
        ("datacenter", "Datacenter", "datacenter-3"),
        ("cluster", "ClusterComputeResource", "domain-c8"),
        ("datastore", "Datastore", "datastore-11"),
        ("datastore_cluster", "StoragePod", "group-p12"),
        ("network", "Network", "network-13"),
        ("distributed_switch", "DistributedVirtualSwitch", "dvs-14"),
        ("distributed_portgroup", "DistributedVirtualPortgroup", "dvportgroup-15"),
        ("resource_pool", "ResourcePool", "resgroup-16"),
    ],
)
async def test_rename_each_kind_uses_its_own_vim_path(
    gate: GateRecorder, kind: str, mo_type: str, moid: str
) -> None:
    conn = InventoryFake()
    conn.add(mo_type, moid, name="<old-name>")
    out = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type=kind,
        moid=moid,
        new_name="<new-name>",
    )
    assert out["status"] == "renamed"
    assert conn.calls_to("Rename_Task")[0][0] == f"/{mo_type}/{moid}/Rename_Task"
    assert gate.calls[0]["op_id"] == f"POST:/{mo_type}/{{moId}}/Rename_Task"


async def test_rename_to_the_same_name_sends_nothing(gate: GateRecorder) -> None:
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<name>")
    out = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="vm-42",
        new_name="<name>",
    )
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_rename_refusals_send_nothing(gate: GateRecorder) -> None:
    conn = InventoryFake()
    missing = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="vm-42",
        new_name="<name>",
    )
    assert missing["status"] == "not_found"
    assert missing["issues"][0]["severity"] == "error"
    wrong = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="domain-c8",
        new_name="<name>",
    )
    assert wrong["status"] == "invalid_request"
    host = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="standalone_host",
        moid="domain-s9",
        new_name="<name>",
    )
    assert host["status"] == "invalid_request"
    assert gate.calls == []
    assert conn.calls_to("Rename_Task") == []


async def test_rename_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = InventoryFake()
    conn.add("Folder", "group-v4", name="<old-name>")
    conn.task_faults["Rename_Task"] = ("DuplicateName", "The name '<new-name>' already exists.")
    with pytest.raises(RuntimeError, match=r"DuplicateName.*already exists"):
        await _run(
            _object_rename_move.object_rename_composite,
            conn,
            object_type="folder",
            moid="group-v4",
            new_name="<new-name>",
        )


async def test_rename_timeout_is_an_error(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_object_rename_move, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<old-name>")
    conn.running.add("Rename_Task")
    out = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="vm-42",
        new_name="<new-name>",
    )
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


async def test_rename_parked_gate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    op_id = "POST:/VirtualMachine/{moId}/Rename_Task"
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(op_id)))
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<old-name>")
    out = await _run(
        _object_rename_move.object_rename_composite,
        conn,
        object_type="vm",
        moid="vm-42",
        new_name="<new-name>",
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert conn.calls_to("Rename_Task") == []


async def test_rename_preview_names_the_current_name() -> None:
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<old-name>")
    params = {"object_type": "vm", "moid": "vm-42", "new_name": "<new-name>"}
    effect = await _PREVIEW_BUILDERS[_RENAME](preview_ctx(params, conn))
    assert effect["current_name"] == "<old-name>"
    assert effect["summary"] == "Rename vm vm-42 from '<old-name>' to '<new-name>'."


# ===========================================================================
# object.move
# ===========================================================================


def _move_fake() -> InventoryFake:
    conn = InventoryFake()
    conn.add("Folder", "group-v7", name="<target-folder>", childType=["Folder", "VirtualMachine"])
    conn.add("Folder", "group-v4", name="vm", parent=moref("Datacenter", "datacenter-3"))
    conn.add("Folder", "group-v8", name="<sub-folder>", parent=moref("Folder", "group-v4"))
    conn.add("Folder", "group-h5", name="host", parent=moref("Datacenter", "datacenter-3"))
    conn.add("VirtualMachine", "vm-42", name="<vm-a>", parent=moref("Folder", "group-v4"))
    conn.add("VirtualMachine", "vm-43", name="<vm-b>", parent=moref("Folder", "group-v7"))
    conn.add("ClusterComputeResource", "domain-c8", name="<cluster>")
    return conn


async def test_move_vms_into_a_folder_skips_the_ones_already_there(gate: GateRecorder) -> None:
    conn = _move_fake()
    out = await _run(
        _object_rename_move.object_move_composite,
        conn,
        folder="group-v7",
        objects=[
            {"object_type": "vm", "moid": "vm-42"},
            {"object_type": "vm", "moid": "vm-43"},
            {"object_type": "folder", "moid": "group-v8"},
        ],
    )
    assert out["status"] == "moved"
    assert out["folder_name"] == "<target-folder>"
    assert conn.calls_to("MoveIntoFolder_Task") == [
        (
            "/Folder/group-v7/MoveIntoFolder_Task",
            {"list": [moref("VirtualMachine", "vm-42"), moref("Folder", "group-v8")]},
        )
    ]
    rows = {r["moid"]: r for r in out["objects"]}
    assert rows["vm-42"]["parent_before"] == "group-v4"
    assert all(r["moved"] for r in out["objects"])
    assert gate.calls[0]["params"] == {"folder": "group-v7", "objects": ["vm-42", "group-v8"]}


async def test_move_when_all_are_in_the_folder_is_unchanged(gate: GateRecorder) -> None:
    conn = _move_fake()
    out = await _run(
        _object_rename_move.object_move_composite,
        conn,
        folder="group-v7",
        objects=[{"object_type": "vm", "moid": "vm-43"}],
    )
    assert out["status"] == "unchanged"
    assert gate.calls == []


@pytest.mark.parametrize(
    ("objects", "status", "text"),
    [
        ([{"object_type": "cluster", "moid": "domain-c8"}], "precondition_failed", "cannot hold"),
        ([{"object_type": "folder", "moid": "group-h5"}], "precondition_failed", "cannot hold"),
        ([{"object_type": "vm", "moid": "vm-77"}], "precondition_failed", "not found"),
        ([{"object_type": "vm", "moid": "domain-c8"}], "invalid_request", "not a vm moid"),
    ],
)
async def test_move_refusals_send_nothing(
    gate: GateRecorder, objects: list[dict[str, str]], status: str, text: str
) -> None:
    conn = _move_fake()
    out = await _run(
        _object_rename_move.object_move_composite, conn, folder="group-v7", objects=objects
    )
    assert out["status"] == status
    assert text in out["issues"][0]["message"]
    assert gate.calls == []
    assert conn.calls_to("MoveIntoFolder_Task") == []


async def test_move_into_a_missing_folder_is_not_found(gate: GateRecorder) -> None:
    conn = _move_fake()
    out = await _run(
        _object_rename_move.object_move_composite,
        conn,
        folder="group-v99",
        objects=[{"object_type": "vm", "moid": "vm-42"}],
    )
    assert out["status"] == "not_found"
    assert gate.calls == []


async def test_move_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = _move_fake()
    conn.task_faults["MoveIntoFolder_Task"] = ("InvalidFolder", "The folder is not valid.")
    with pytest.raises(RuntimeError, match=r"InvalidFolder.*not valid"):
        await _run(
            _object_rename_move.object_move_composite,
            conn,
            folder="group-v7",
            objects=[{"object_type": "vm", "moid": "vm-42"}],
        )


async def test_move_immediate_vim_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = _move_fake()
    conn.sync_fault = _vim_fault("NotSupported", "The operation is not supported.")
    with pytest.raises(RuntimeError, match=r"NotSupported.*not supported"):
        await _run(
            _object_rename_move.object_move_composite,
            conn,
            folder="group-v7",
            objects=[{"object_type": "vm", "moid": "vm-42"}],
        )


async def test_move_that_does_not_read_back_is_partial(gate: GateRecorder) -> None:
    conn = _move_fake()
    conn.apply_writes = False
    out = await _run(
        _object_rename_move.object_move_composite,
        conn,
        folder="group-v7",
        objects=[{"object_type": "vm", "moid": "vm-42"}],
    )
    assert out["status"] == "partial"
    assert out["objects"][0]["moved"] is False


async def test_move_preview_names_the_folder_and_the_objects() -> None:
    conn = _move_fake()
    params = {"folder": "group-v7", "objects": [{"object_type": "vm", "moid": "vm-42"}]}
    effect = await _PREVIEW_BUILDERS[_MOVE](preview_ctx(params, conn))
    assert effect["folder_name"] == "<target-folder>"
    assert effect["objects"][0]["parent"] == "group-v4"
    assert "permissions" in effect["summary"]


# ===========================================================================
# resource_pool.update (REST)
# ===========================================================================


class PoolFake:
    """REST double: ``GET`` / ``PATCH /vcenter/resource-pool/{id}``."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, pool: dict[str, Any] | None) -> None:
        self.pool = pool
        self.patches: list[dict[str, Any]] = []
        self.patch_error: httpx.HTTPStatusError | None = None

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(self, target: Any, path: str, *, operator: Operator, params: Any) -> Any:
        if self.pool is None:
            raise http_status_error(404, json.dumps({"error_type": "NOT_FOUND"}))
        return json.loads(json.dumps(self.pool))

    async def _post_json(self, target: Any, path: str, *, verb: str, json: Any, **_: Any) -> Any:
        assert (verb, path) == ("PATCH", "/api/vcenter/resource-pool/resgroup-42")
        self.patches.append(json)
        if self.patch_error is not None:
            raise self.patch_error
        assert self.pool is not None
        self.pool.update(json)
        return None


def _pool() -> dict[str, Any]:
    alloc = {
        "reservation": 0,
        "expandable_reservation": True,
        "limit": -1,
        "shares": {"level": "NORMAL", "shares": 4000},
    }
    return {"name": "<pool>", "cpu_allocation": dict(alloc), "memory_allocation": dict(alloc)}


async def _update(conn: PoolFake, **params: Any) -> Any:
    return await _resource_pool_update.resource_pool_update_composite(
        operator=operator(),
        target=object(),
        params={"resource_pool": "resgroup-42", **params},
        connector=conn,  # type: ignore[arg-type]
    )


async def test_pool_update_keeps_the_fields_left_out(gate: GateRecorder) -> None:
    conn = PoolFake(_pool())
    out = await _update(conn, memory_allocation={"limit": 16384, "shares": {"level": "HIGH"}})

    assert out["status"] == "updated"
    assert conn.patches == [
        {
            "memory_allocation": {
                "reservation": 0,
                "expandable_reservation": True,
                "limit": 16384,
                "shares": {"level": "HIGH"},
            }
        }
    ]
    assert out["before"]["memory_allocation"]["limit"] == -1
    assert out["after"]["memory_allocation"]["limit"] == 16384
    assert out["after"]["cpu_allocation"]["limit"] == -1
    assert [c["op_id"] for c in gate.calls] == ["PATCH:/vcenter/resource-pool/{resourcePool}"]


async def test_pool_update_custom_shares_sends_the_number(gate: GateRecorder) -> None:
    conn = PoolFake(_pool())
    await _update(conn, cpu_allocation={"shares": {"level": "CUSTOM", "shares": 9000}})
    assert conn.patches[0]["cpu_allocation"]["shares"] == {"level": "CUSTOM", "shares": 9000}


async def test_pool_update_with_the_current_values_sends_nothing(gate: GateRecorder) -> None:
    conn = PoolFake(_pool())
    out = await _update(conn, cpu_allocation={"limit": -1, "shares": {"level": "NORMAL"}})
    assert out["status"] == "unchanged"
    assert conn.patches == []
    assert gate.calls == []


async def test_pool_update_of_a_missing_pool_is_not_found(gate: GateRecorder) -> None:
    out = await _update(PoolFake(None), cpu_allocation={"limit": 100})
    assert out["status"] == "not_found"
    assert out["issues"][0]["severity"] == "error"
    assert gate.calls == []


async def test_pool_update_vendor_refusal_reaches_the_answer(gate: GateRecorder) -> None:
    conn = PoolFake(_pool())
    conn.patch_error = http_status_error(
        400,
        json.dumps(
            {
                "error_type": "INVALID_ARGUMENT",
                "messages": [{"default_message": "The reservation is too large."}],
            }
        ),
    )
    out = await _update(conn, cpu_allocation={"reservation": 999999})
    assert out["status"] == "refused"
    assert out["error_type"] == "INVALID_ARGUMENT"
    assert "reservation is too large" in out["guidance"]


async def test_pool_update_parked_gate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    op_id = "PATCH:/vcenter/resource-pool/{resourcePool}"
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(op_id)))
    conn = PoolFake(_pool())
    out = await _update(conn, cpu_allocation={"limit": 100})
    assert isinstance(out, OperationResult)
    assert conn.patches == []


def test_pool_update_schema_needs_an_allocation_and_custom_needs_a_number() -> None:
    schema = _resource_pool_update.RESOURCE_POOL_UPDATE_PARAMETER_SCHEMA
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"resource_pool": "resgroup-42", "cpu_allocation": {"limit": 1}})
    for bad in (
        {"resource_pool": "resgroup-42"},
        {"resource_pool": "resgroup-42", "cpu_allocation": {}},
        {"resource_pool": "resgroup-42", "cpu_allocation": {"shares": {"level": "CUSTOM"}}},
        {"resource_pool": "domain-c8", "cpu_allocation": {"limit": 1}},
    ):
        assert not validator.is_valid(bad), bad


async def test_pool_update_preview_shows_what_it_will_send() -> None:
    conn = PoolFake(_pool())
    params = {"resource_pool": "resgroup-42", "cpu_allocation": {"limit": 100}}
    effect = await _PREVIEW_BUILDERS[_POOL_UPDATE](preview_ctx(params, conn))
    assert effect["current"]["name"] == "<pool>"
    assert effect["will_send"]["cpu_allocation"]["limit"] == 100


# ===========================================================================
# Parameter schemas
# ===========================================================================


def test_inventory_parameter_schemas_check_the_inputs() -> None:
    schemas = {row.op_id: row.parameter_schema for row in INVENTORY_COMPOSITES}
    move = Draft202012Validator(schemas[_MOVE])
    too_many = [{"object_type": "vm", "moid": f"vm-{n}"} for n in range(51)]
    assert not move.is_valid({"folder": "group-v7", "objects": too_many})
    assert not move.is_valid({"folder": "group-p7", "objects": too_many[:1]})
    assert not move.is_valid(
        {"folder": "group-v7", "objects": [{"object_type": "resource_pool", "moid": "resgroup-1"}]}
    )
    rename = Draft202012Validator(schemas[_RENAME])
    assert not rename.is_valid({"object_type": "vm", "moid": "vm-1", "new_name": "x" * 80})
    host = {"object_type": "standalone_host", "moid": "domain-s1", "new_name": "x"}
    assert not rename.is_valid(host)
    create = Draft202012Validator(schemas[_DC_CREATE])
    assert not create.is_valid({"name": "x", "parent_folder_id": "group-v1"})


# ===========================================================================
# The vCenter 8 and vCenter 9 VI-JSON paths (respx)
# ===========================================================================


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "<placeholder>"}


@dataclass
class _StubTarget:
    name: str = "vc"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _retrieve(mo_type: str, moid: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": mo_type, "value": moid},
                "propSet": [{"name": k, "val": v} for k, v in props.items()],
            }
        ]
    }


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_rename_lands_on_the_versioned_vi_json_path(
    gate: GateRecorder, about: str, release: str
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    vim = f"/sdk/vim25/{release}"
    names = iter(["<old-name>", "<new-name>"])
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})

            def _props(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                mo_type = body["specSet"][0]["propSet"][0]["type"]
                if mo_type == "Task":
                    return httpx.Response(
                        200, json=_retrieve("Task", "task-1", {"info": {"state": "success"}})
                    )
                return httpx.Response(
                    200, json=_retrieve("VirtualMachine", "vm-42", {"name": next(names)})
                )

            mock.post(f"{vim}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_props
            )
            rename = mock.post(f"{vim}/VirtualMachine/vm-42/Rename_Task").respond(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-1"}
            )
            out = await _object_rename_move.object_rename_composite(
                operator=operator(),
                target=_StubTarget(),
                params={"object_type": "vm", "moid": "vm-42", "new_name": "<new-name>"},
                connector=connector,
            )
        assert rename.call_count == 1
        assert json.loads(rename.calls[0].request.content) == {"newName": "<new-name>"}
        assert out["status"] == "renamed"
        assert out["name_after"] == "<new-name>"
    finally:
        await connector.aclose()


# ===========================================================================
# Registration and the governed flow through the dispatcher
# ===========================================================================

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004339")
_TARGET_NAME = "example-vcenter"


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="Inventory Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _bootstrap(conn: InventoryFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_composites_inventory",
            )
        )
        await s.commit()


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_on_8_0_and_9_0_with_the_table_levels() -> None:
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    expected = {
        _DC_CREATE: "caution",
        _DC_DELETE: "destructive",
        _RENAME: "caution",
        _MOVE: "dangerous",
        _POOL_UPDATE: "dangerous",
        _FOLDER_CREATE: "dangerous",
    }
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(expected))
                )
            )
            .scalars()
            .all()
        )
    levels = {(row.op_id, row.version): (row.safety_level, row.requires_approval) for row in rows}
    assert levels == {
        (op_id, version): (level, True)
        for op_id, level in expected.items()
        for version in ("8.0", "9.0")
    }


async def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": "vmware-rest-9.0",
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


async def _approve_and_resume(call: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    return effect, resume


@pytest.mark.usefixtures("_dispatch_env")
async def test_datacenter_delete_without_a_preview_hash_is_refused() -> None:
    conn = InventoryFake()
    _datacenter(conn)
    await _bootstrap(conn)
    args = await _args(_DC_DELETE, {"datacenter": "datacenter-3"})
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    wrong = await call_operation(_person("op-requester"), {**args, "preview_hash": "0" * 64})
    assert wrong["status"] == "denied", wrong
    assert wrong["extras"]["error_code"] == "preview_hash_mismatch"
    assert conn.calls_to("Destroy_Task") == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_datacenter_delete_waits_for_a_second_person_then_deletes() -> None:
    conn = InventoryFake()
    _datacenter(conn)
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = await _args(_DC_DELETE, {"datacenter": "datacenter-3"})
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("Destroy_Task") == []

    effect, resume = await _approve_and_resume(call)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["name"] == "<dc>"
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"


@pytest.mark.usefixtures("_dispatch_env")
async def test_rename_waits_for_approval_then_renames() -> None:
    conn = InventoryFake()
    conn.add("VirtualMachine", "vm-42", name="<old-name>")
    await _bootstrap(conn)
    args = await _args(_RENAME, {"object_type": "vm", "moid": "vm-42", "new_name": "<new-name>"})
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("Rename_Task") == []

    effect, resume = await _approve_and_resume(call)
    assert effect["preview"]["current_name"] == "<old-name>"
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "renamed"
    assert resume.result["name_after"] == "<new-name>"
