# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``datastore.file.delete`` / ``datastore.dir.create`` (#3339).

Driven against the shared vim fake's datastore file tree. Covers the path
guards (handler and parameter schema agree), the datacenter walk the
``FileManager`` call needs, the registered-VM file refusal (incl. a VM without
``layoutEx`` and a paged answer), ``unchanged`` when absent, task fault /
timeout handling, the gate park, and the park-time blast radius (size + files).
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.connectors.vmware_rest.composites import (
    _datastore_files,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_browse import path_problem
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
    DATASTORE_DIR_CREATE_RESPONSE_SCHEMA,
    DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
    DATASTORE_FILE_DELETE_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    moref,
    operator,
    parked,
    preview_ctx,
)

_DELETE_OP = "POST:/FileManager/{moId}/DeleteDatastoreFile_Task"
_MKDIR_OP = "POST:/FileManager/{moId}/MakeDirectory"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _estate(*, vms: dict[str, dict[str, Any]] | None = None) -> VimFake:
    """One datastore ``demo-ds`` under a datastore folder under ``datacenter-2``."""
    conn = VimFake()
    conn.add(
        "Datastore",
        "datastore-17",
        name="demo-ds",
        browser=moref("HostDatastoreBrowser", "datastoreBrowser-datastore-17"),
        parent=moref("Folder", "group-s5"),
        vm=[moref("VirtualMachine", moid) for moid in (vms or {})],
    )
    conn.add("Folder", "group-s5", parent=moref("Datacenter", "datacenter-2"))
    for moid, props in (vms or {}).items():
        conn.add("VirtualMachine", moid, **props)
    conn.files["demo-ds"] = {
        "old-appliance": None,
        "old-appliance/old-appliance.vmx": 3000,
        "old-appliance/old-appliance-flat.vmdk": 10_000_000,
        "old-appliance/logs": None,
        "old-appliance/logs/vmware.log": 500,
        "iso": None,
        "iso/stale.iso": 4_000_000,
        "live-vm": None,
        "live-vm/live-vm.vmdk": 2_000,
        "live-vm/leftover.iso": 100,
    }
    return conn


def _live_vm(**extra: Any) -> dict[str, dict[str, Any]]:
    props: dict[str, Any] = {
        "name": "example-vm",
        "layoutEx.file": [
            {"name": "[demo-ds] live-vm/live-vm.vmx"},
            {"name": "[demo-ds] live-vm/live-vm.vmdk"},
        ],
        "config.files.vmPathName": "[demo-ds] live-vm/live-vm.vmx",
    }
    props.update(extra)
    return {"vm-5": props}


async def _delete(conn: VimFake, path: str) -> Any:
    return await _datastore_files.datastore_file_delete_composite(
        operator=operator(),
        target=object(),
        params={"datastore": "datastore-17", "path": path},
        connector=conn,
    )


async def _mkdir(conn: VimFake, path: str, **extra: Any) -> Any:
    return await _datastore_files.datastore_dir_create_composite(
        operator=operator(),
        target=object(),
        params={"datastore": "datastore-17", "path": path, **extra},
        connector=conn,
    )


# ---------------------------------------------------------------------------
# datastore.file.delete
# ---------------------------------------------------------------------------


async def test_delete_directory_happy_path(gate: GateRecorder) -> None:
    conn = _estate(vms=_live_vm())
    out = await _delete(conn, "old-appliance")
    assert out["status"] == "deleted"
    assert out["object"]["kind"] == "datastore_directory"
    assert out["object"]["datastore_path"] == "[demo-ds] old-appliance"
    Draft202012Validator(DATASTORE_FILE_DELETE_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_DELETE_OP]
    assert gate.calls[0]["params"] == {"datastore": "datastore-17", "path": "old-appliance"}
    path, body = conn.calls_to("DeleteDatastoreFile_Task")[0]
    assert path == "/FileManager/FileManager/DeleteDatastoreFile_Task"
    # The datacenter came from walking Datastore.parent -> Folder -> Datacenter.
    assert body == {
        "name": "[demo-ds] old-appliance",
        "datacenter": moref("Datacenter", "datacenter-2"),
    }
    assert not any(p.startswith("old-appliance") for p in conn.files["demo-ds"])
    # The existence check browsed the parent (the root) for the exact name.
    _, search = conn.calls_to("SearchDatastore_Task")[0]
    assert search["datastorePath"] == "[demo-ds]"
    assert search["searchSpec"]["matchPattern"] == ["old-appliance"]
    assert search["searchSpec"]["details"]["_typeName"] == "FileQueryFlags"


async def test_delete_single_file(gate: GateRecorder) -> None:
    conn = _estate()
    out = await _delete(conn, "iso/stale.iso")
    assert out["status"] == "deleted"
    assert out["object"]["kind"] == "datastore_file"
    assert out["object"]["size_bytes"] == 4_000_000


@pytest.mark.parametrize("path", ["live-vm", "live-vm/live-vm.vmdk", "LIVE-VM/live-vm.vmx"])
async def test_delete_refuses_registered_vm_files(gate: GateRecorder, path: str) -> None:
    conn = _estate(vms=_live_vm())
    conn.files["demo-ds"]["LIVE-VM"] = None
    conn.files["demo-ds"]["LIVE-VM/live-vm.vmx"] = 10
    out = await _delete(conn, path)
    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["moid"] == "vm-5"
    assert out["blockers"][0]["name"] == "example-vm"
    assert "example-vm" in out["guidance"]
    assert gate.calls == []
    assert "DeleteDatastoreFile_Task" not in conn.methods()


async def test_delete_allows_unclaimed_file_inside_a_vm_directory(gate: GateRecorder) -> None:
    conn = _estate(vms=_live_vm())
    out = await _delete(conn, "live-vm/leftover.iso")
    assert out["status"] == "deleted"


async def test_delete_vm_without_layout_claims_its_home(gate: GateRecorder) -> None:
    conn = _estate(vms=_live_vm(**{"layoutEx.file": []}))
    out = await _delete(conn, "live-vm/leftover.iso")
    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["files"] == ["live-vm/"]


async def test_delete_refuses_when_vm_files_are_unknowable(gate: GateRecorder) -> None:
    conn = _estate(vms={"vm-6": {"name": "broken-vm"}})
    out = await _delete(conn, "iso/stale.iso")
    assert out["status"] == "precondition_failed"
    assert "could not enumerate" in out["guidance"]
    assert gate.calls == []


async def test_delete_refuses_a_paged_vm_answer(gate: GateRecorder) -> None:
    conn = _estate(vms=_live_vm())
    conn.page_token = True
    out = await _delete(conn, "iso/stale.iso")
    assert out["status"] == "precondition_failed"
    assert gate.calls == []


@pytest.mark.parametrize("path", ["iso/missing.iso", "no-such-dir/file.iso"])
async def test_delete_absent_is_unchanged(gate: GateRecorder, path: str) -> None:
    conn = _estate()
    out = await _delete(conn, path)
    assert out["status"] == "unchanged"
    assert gate.calls == []
    Draft202012Validator(DATASTORE_FILE_DELETE_RESPONSE_SCHEMA).validate(out)


_BAD_PATHS = [
    "",
    "/old-appliance",
    "../etc",
    "a/../b",
    "a/./b",
    ".",
    ".sdd.sf",
    ".vSphere-HA/x",
    "contentlib-1234/item",
    "iso/*.iso",
    "iso/[x]",
    "iso\\x",
    "a//b",
    "trailing/",
    "bad\x01name",
]


@pytest.mark.parametrize("path", _BAD_PATHS)
async def test_delete_refuses_bad_paths_before_any_io(gate: GateRecorder, path: str) -> None:
    conn = _estate()
    out = await _delete(conn, path)
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    assert gate.calls == []


@pytest.mark.parametrize("path", _BAD_PATHS)
def test_schema_and_handler_agree_on_bad_paths(path: str) -> None:
    """Preview validates the same path contract the handler enforces (#3323)."""
    assert path_problem(path) is not None
    with pytest.raises(ValidationError):
        Draft202012Validator(DATASTORE_FILE_DELETE_PARAMETER_SCHEMA).validate(
            {"datastore": "datastore-17", "path": path}
        )


@pytest.mark.parametrize("path", ["old-appliance", "iso/stale.iso", "a b/c.d-e_f", "x/.lck-1"])
def test_schema_and_handler_accept_good_paths(path: str) -> None:
    assert path_problem(path) is None
    Draft202012Validator(DATASTORE_FILE_DELETE_PARAMETER_SCHEMA).validate(
        {"datastore": "datastore-17", "path": path}
    )


async def test_delete_unknown_datastore_is_invalid_request(gate: GateRecorder) -> None:
    conn = _estate()
    out = await _datastore_files.datastore_file_delete_composite(
        operator=operator(),
        target=object(),
        params={"datastore": "datastore-404", "path": "iso"},
        connector=conn,
    )
    assert out["status"] == "invalid_request"
    assert "not found" in out["guidance"]


async def test_delete_task_fault_raises(gate: GateRecorder) -> None:
    conn = _estate()
    conn.task_faults["DeleteDatastoreFile_Task"] = ("FileLocked", "file is locked")
    with pytest.raises(RuntimeError, match="file is locked"):
        await _delete(conn, "iso/stale.iso")


async def test_delete_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_datastore_files, "_DELETE_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _estate()
    conn.running.add("DeleteDatastoreFile_Task")
    out = await _delete(conn, "iso/stale.iso")
    assert out["status"] == "timeout"


async def test_delete_park_keeps_write_off_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    verdict = parked(_DELETE_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    conn = _estate()
    out = await _delete(conn, "iso/stale.iso")
    assert out is verdict
    assert "DeleteDatastoreFile_Task" not in conn.methods()


async def test_delete_still_present(gate: GateRecorder) -> None:
    conn = _estate()
    conn.apply_writes = False
    out = await _delete(conn, "iso/stale.iso")
    assert out["status"] == "still_present"


async def test_delete_preview_sizes_the_directory() -> None:
    conn = _estate()
    effect = await _datastore_files.datastore_file_delete_preview(
        preview_ctx({"datastore": "datastore-17", "path": "old-appliance"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["file_count"] == 3
    assert block["object"]["total_bytes"] == 10_003_500
    assert {c["path"] for c in block["children"]} == {
        "old-appliance/old-appliance.vmx",
        "old-appliance/old-appliance-flat.vmdk",
        "old-appliance/logs/vmware.log",
    }
    assert "refusal" not in block
    assert "DeleteDatastoreFile_Task" not in conn.methods()


async def test_delete_preview_carries_the_refusal() -> None:
    conn = _estate(vms=_live_vm())
    effect = await _datastore_files.datastore_file_delete_preview(
        preview_ctx({"datastore": "datastore-17", "path": "live-vm"}, conn)
    )
    assert effect is not None
    assert effect["blast_radius"]["refusal"]["status"] == "precondition_failed"
    assert (
        await _datastore_files.datastore_file_delete_preview(
            preview_ctx({"datastore": "datastore-17", "path": "iso"}, None)
        )
        is None
    )


# ---------------------------------------------------------------------------
# datastore.dir.create
# ---------------------------------------------------------------------------


async def test_dir_create_happy_path(gate: GateRecorder) -> None:
    conn = _estate()
    out = await _mkdir(conn, "iso/new-media")
    assert out["status"] == "created"
    Draft202012Validator(DATASTORE_DIR_CREATE_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_MKDIR_OP]
    path, body = conn.calls_to("MakeDirectory")[0]
    assert path == "/FileManager/FileManager/MakeDirectory"
    assert body == {
        "name": "[demo-ds] iso/new-media",
        "datacenter": moref("Datacenter", "datacenter-2"),
        "createParentDirectories": False,
    }


async def test_dir_create_existing_is_unchanged(gate: GateRecorder) -> None:
    conn = _estate()
    out = await _mkdir(conn, "iso")
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_dir_create_refuses_a_file_of_that_name(gate: GateRecorder) -> None:
    conn = _estate()
    out = await _mkdir(conn, "iso/stale.iso")
    assert out["status"] == "precondition_failed"
    assert gate.calls == []


async def test_dir_create_missing_parent_needs_create_parents(gate: GateRecorder) -> None:
    conn = _estate()
    refused = await _mkdir(conn, "stage/media")
    assert refused["status"] == "precondition_failed"
    assert "create_parents" in refused["guidance"]
    assert gate.calls == []
    out = await _mkdir(conn, "stage/media", create_parents=True)
    assert out["status"] == "created"
    assert conn.calls_to("MakeDirectory")[0][1]["createParentDirectories"] is True


async def test_dir_create_not_verified(gate: GateRecorder) -> None:
    conn = _estate()
    conn.apply_writes = False
    out = await _mkdir(conn, "fresh")
    assert out["status"] == "not_verified"


async def test_dir_create_bad_path_and_schema() -> None:
    conn = _estate()
    out = await _mkdir(conn, "../escape")
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    with pytest.raises(ValidationError):
        Draft202012Validator(DATASTORE_DIR_CREATE_PARAMETER_SCHEMA).validate(
            {"datastore": "datastore-17", "path": "../escape"}
        )


async def test_dir_create_preview_is_a_param_echo() -> None:
    preview = await _datastore_files.datastore_dir_create_preview(
        preview_ctx({"datastore": "datastore-17", "path": "iso"}, None)
    )
    assert preview == {
        "action": "create_datastore_directory",
        "datastore": "datastore-17",
        "path": "iso",
        "create_parents": False,
    }
