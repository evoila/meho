# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``vmware.composite.datastore.vsan.folder.list`` (#4022).

Driven against the shared vim fake, with a vSAN browse on top: top-level
folders are named by UUID and carry a ``friendlyName`` in the root browse, and
a friendly name in a datastore path resolves to its UUID folder. Covers the
top-level list (sizes, latest change, the VMs that use a folder by UUID path
or by friendly name), the file list of one folder, a missing folder
("absent"), a large list (result handle), the refusals, and that the VMFS / NFS
guard of ``datastore.file.delete`` still refuses vSAN.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.connectors.vmware_rest.composites import _datastore_vsan
from meho_backplane.connectors.vmware_rest.composites._datastore_files import (
    datastore_file_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
    DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import VimFake, moref, operator

_OP_ID = "vmware.composite.datastore.vsan.folder.list"
_VALIDATE_OUT = Draft202012Validator(DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA).validate
_DS = "datastore-21"
_DS_NAME = "vsanDatastore"
_READ_METHODS = {"RetrievePropertiesEx", "SearchDatastore_Task", "SearchDatastoreSubFolders_Task"}

_UUID_APP = "5f1a0b2c-0000-0000-0000-00000000000a"
_UUID_DB = "5f1a0b2c-0000-0000-0000-00000000000b"
_UUID_OLD = "5f1a0b2c-0000-0000-0000-00000000000c"


class _VsanFake(VimFake):
    """The vim fake with a vSAN browse: UUID folders with a friendly name."""

    def __init__(self) -> None:
        super().__init__()
        #: top-level UUID folder -> friendly name
        self.friendly: dict[str, str] = {}
        #: rel path (UUID form) -> modification time stamp
        self.mtimes: dict[str, str] = {}

    def _to_uuid(self, folder: str) -> str:
        head, sep, rest = folder.partition("/")
        for uuid, name in self.friendly.items():
            if name == head:
                return uuid + sep + rest
        return folder

    def _search(self, method: str, body: dict[str, Any]) -> dict[str, str]:
        name, folder = self._split(body["datastorePath"])
        folder = self._to_uuid(folder)
        tree = self.files.get(name, {})
        if folder and tree.get(folder, 0) is not None:
            return self._fault_task("FileNotFound", f"File [{name}] {folder} was not found")
        folders = [folder]
        if method == "SearchDatastoreSubFolders_Task":
            folders += [
                p
                for p, size in tree.items()
                if size is None and (not folder or p.startswith(folder + "/"))
            ]
        results = []
        for current in folders:
            entries = []
            for p, size in sorted(tree.items()):
                parent, _, base = p.rpartition("/")
                if parent != current:
                    continue
                entry: dict[str, Any] = {
                    "_typeName": "FolderFileInfo" if size is None else "FileInfo",
                    "path": base,
                }
                if not current and base in self.friendly:
                    entry["friendlyName"] = self.friendly[base]
                if size is not None:
                    entry["fileSize"] = size
                if p in self.mtimes:
                    entry["modification"] = self.mtimes[p]
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


#: app-01 names its files by UUID path (what vCenter shows on vSAN); db-01 by
#: friendly name. Both must count as users of their folder.
_VMS: dict[str, dict[str, Any]] = {
    "vm-1": {
        "name": "app-01",
        "layoutEx.file": [
            {"name": f"[{_DS_NAME}] {_UUID_APP}/app-01.vmx"},
            {"name": f"[{_DS_NAME}] {_UUID_APP}/app-01.vmdk"},
        ],
        "config.files.vmPathName": f"[{_DS_NAME}] {_UUID_APP}/app-01.vmx",
    },
    "vm-2": {
        "name": "db-01",
        "layoutEx.file": [
            {"name": f"[{_DS_NAME}] db-01/db-01.vmx"},
            # A disk shared from the app-01 folder.
            {"name": f"[{_DS_NAME}] app-01/shared-1.vmdk"},
        ],
        "config.files.vmPathName": f"[{_DS_NAME}] db-01/db-01.vmx",
    },
}


def _vsan(*, ds_type: str = "vsan", vms: dict[str, dict[str, Any]] | None = None) -> _VsanFake:
    conn = _VsanFake()
    vms = _VMS if vms is None else vms
    conn.add(
        "Datastore",
        _DS,
        name=_DS_NAME,
        **{"summary.type": ds_type},
        browser=moref("HostDatastoreBrowser", "datastoreBrowser-" + _DS),
        parent=moref("Folder", "group-s5"),
        vm=[moref("VirtualMachine", vm) for vm in vms],
    )
    conn.add("Folder", "group-s5", parent=moref("Datacenter", "datacenter-2"))
    for vm, props in vms.items():
        conn.add("VirtualMachine", vm, **props)
    conn.friendly = {_UUID_APP: "app-01", _UUID_DB: "db-01", _UUID_OLD: "old-vm"}
    conn.files[_DS_NAME] = {
        ".vsan.stats": None,
        _UUID_APP: None,
        f"{_UUID_APP}/app-01.vmx": 3_000,
        f"{_UUID_APP}/app-01.vmdk": 1_000,
        f"{_UUID_APP}/shared-1.vmdk": 5_000,
        _UUID_DB: None,
        f"{_UUID_DB}/db-01.vmx": 2_000,
        f"{_UUID_DB}/logs": None,
        f"{_UUID_DB}/logs/vmware.log": 500,
        _UUID_OLD: None,
        f"{_UUID_OLD}/old-vm.vmdk": 7_000,
    }
    conn.mtimes = {
        _UUID_APP: "2026-01-01T00:00:00Z",
        f"{_UUID_APP}/app-01.vmx": "2026-10-01T10:00:00Z",
        f"{_UUID_APP}/app-01.vmdk": "2026-09-01T10:00:00Z",
        f"{_UUID_DB}/logs/vmware.log": "2026-10-20T08:30:00Z",
    }
    return conn


async def _list(conn: VimFake, **params: Any) -> dict[str, Any]:
    params.setdefault("datastore", _DS)
    return await _datastore_vsan.datastore_vsan_folder_list_composite(
        operator=operator(),
        target=None,
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Top-level folders
# ---------------------------------------------------------------------------


async def test_top_level_folders_have_names_uuid_size_and_vms() -> None:
    conn = _vsan()
    out = await _list(conn)
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["datastore_name"] == _DS_NAME
    assert out["datastore_type"] == "vsan"
    assert out["datastore_path"] == f"[{_DS_NAME}]"
    assert out["vm_check"] == "complete"
    rows = {row["name"]: row for row in out["entries"]}
    # The hidden .vsan.stats folder is left out.
    assert set(rows) == {"app-01", "db-01", "old-vm"}
    assert out["count"] == 3
    assert rows["app-01"] == {
        "name": "app-01",
        "uuid_path": _UUID_APP,
        "datastore_path": f"[{_DS_NAME}] {_UUID_APP}",
        "size_bytes": 9_000,
        "file_count": 3,
        "modified": "2026-10-01T10:00:00Z",
        "vms": ["app-01", "db-01"],
    }
    # Files in a subfolder count too, and their change time.
    assert rows["db-01"]["size_bytes"] == 2_500
    assert rows["db-01"]["file_count"] == 2
    assert rows["db-01"]["modified"] == "2026-10-20T08:30:00Z"
    assert rows["db-01"]["vms"] == ["db-01"]
    assert rows["old-vm"]["vms"] == []
    assert rows["old-vm"]["size_bytes"] == 7_000
    assert set(conn.methods()) <= _READ_METHODS


async def test_incomplete_vm_answer_is_reported() -> None:
    conn = _vsan(vms={"vm-9": {"name": "unknown-files"}})
    out = await _list(conn)
    _VALIDATE_OUT(out)
    assert out["vm_check"] == "incomplete"


async def test_empty_vsan_datastore_lists_no_folders() -> None:
    conn = _vsan(vms={})
    conn.files[_DS_NAME] = {}
    out = await _list(conn)
    _VALIDATE_OUT(out)
    assert out == {**out, "status": "ok", "count": 0, "entries": [], "vm_check": "complete"}
    assert "SearchDatastoreSubFolders_Task" not in conn.methods()


# ---------------------------------------------------------------------------
# Files in one folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["app-01", _UUID_APP])
async def test_folder_lists_files_with_size_and_last_change(folder: str) -> None:
    conn = _vsan()
    out = await _list(conn, folder=folder)
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["folder"] == folder
    assert out["datastore_path"] == f"[{_DS_NAME}] {folder}"
    assert out["vm_check"] is None
    assert out["entries"] == [
        {
            "name": "app-01.vmdk",
            "kind": "file",
            "file_type": "FileInfo",
            "size_bytes": 1_000,
            "modified": "2026-09-01T10:00:00Z",
        },
        {
            "name": "app-01.vmx",
            "kind": "file",
            "file_type": "FileInfo",
            "size_bytes": 3_000,
            "modified": "2026-10-01T10:00:00Z",
        },
        {
            "name": "shared-1.vmdk",
            "kind": "file",
            "file_type": "FileInfo",
            "size_bytes": 5_000,
            "modified": None,
        },
    ]
    assert set(conn.methods()) <= {"RetrievePropertiesEx", "SearchDatastore_Task"}


async def test_subfolder_shows_as_folder_without_size() -> None:
    out = await _list(_vsan(), folder="db-01")
    rows = {row["name"]: row for row in out["entries"]}
    assert rows["logs"]["kind"] == "folder"
    assert rows["logs"]["size_bytes"] is None
    out = await _list(_vsan(), folder="db-01/logs")
    assert [row["name"] for row in out["entries"]] == ["vmware.log"]


async def test_missing_folder_answers_absent() -> None:
    conn = _vsan()
    out = await _list(conn, folder="deleted-vm")
    _VALIDATE_OUT(out)
    assert out["status"] == "absent"
    assert out["entries"] == []
    assert out["count"] == 0
    assert "does not exist" in out["guidance"]


async def test_folder_reads_absent_after_its_files_are_gone() -> None:
    conn = _vsan()
    for path in [p for p in conn.files[_DS_NAME] if p.startswith(_UUID_OLD)]:
        del conn.files[_DS_NAME][path]
    del conn.friendly[_UUID_OLD]
    assert (await _list(conn, folder="old-vm"))["status"] == "absent"
    names = {row["name"] for row in (await _list(conn))["entries"]}
    assert "old-vm" not in names


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reducer reads its row and size limits from the settings."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_settings_env")
async def test_many_files_return_every_row_and_a_result_handle() -> None:
    conn = _vsan()
    for i in range(300):
        conn.files[_DS_NAME][f"{_UUID_OLD}/disk-{i:03}.vmdk"] = i
    out = await _list(conn, folder="old-vm")
    _VALIDATE_OUT(out)
    assert out["count"] == 301
    assert len(out["entries"]) == 301

    hints = STORAGE_NETWORK_LLM_INSTRUCTIONS[_OP_ID]["result_scalars"]
    summary, handle = await JsonFluxReducer().reduce(
        out, None, {"op_id": _OP_ID, "result_scalars": hints}
    )
    assert handle is not None
    assert summary["row_count"] == 301
    assert summary["status"] == "ok"
    assert summary["folder"] == "old-vm"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ds_type", ["VMFS", "NFS", "vvol"])
async def test_non_vsan_datastore_is_refused_without_a_browse(ds_type: str) -> None:
    conn = _vsan(ds_type=ds_type)
    out = await _list(conn)
    _VALIDATE_OUT(out)
    assert out["status"] == "invalid_request"
    assert "vSAN" in out["guidance"]
    assert set(conn.methods()) == {"RetrievePropertiesEx"}


async def test_unknown_datastore_is_not_found() -> None:
    conn = _vsan()
    conn.not_found_as_json = True
    out = await _list(conn, datastore="datastore-99")
    _VALIDATE_OUT(out)
    assert out["status"] == "not_found"


@pytest.mark.parametrize(
    "params",
    [
        {"datastore": "vsanDatastore"},
        {"folder": "../etc"},
        {"folder": "/app-01"},
        {"folder": "a//b"},
        {"folder": "app-*"},
        {"folder": ""},
    ],
)
async def test_bad_input_is_refused_before_any_call(params: dict[str, Any]) -> None:
    conn = _vsan()
    out = await _list(conn, **params)
    _VALIDATE_OUT(out)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_file_delete_still_refuses_vsan() -> None:
    """The VMFS / NFS guard of the delete is unchanged by this read."""
    conn = _vsan()
    out = await datastore_file_delete_composite(
        operator=operator(),
        target=None,
        params={"datastore": _DS, "path": "old-vm"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "invalid_request"
    assert "Only VMFS and NFS" in out["guidance"]
    assert "DeleteDatastoreFile_Task" not in conn.methods()


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


def test_parameter_schema_examples_validate() -> None:
    validator = Draft202012Validator(DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA)
    for example in DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA["examples"]:
        validator.validate(example)
    validator.validate({"datastore": _DS, "folder": f"{_UUID_DB}/logs"})
    validator.validate({"datastore": _DS, "folder": "my vm (copy)"})


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"datastore": "vsanDatastore"},
        {"datastore": _DS, "folder": ""},
        {"datastore": _DS, "folder": ".."},
        {"datastore": _DS, "folder": "a/../b"},
        {"datastore": _DS, "folder": "app-01/"},
        {"datastore": _DS, "folder": "[vsanDatastore] app-01"},
        {"datastore": _DS, "extra": 1},
    ],
)
def test_parameter_schema_refuses(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA).validate(params)
