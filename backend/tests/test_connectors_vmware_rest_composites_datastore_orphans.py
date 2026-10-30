# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``vmware.composite.datastore.orphan_dirs`` (#3814).

Driven against the shared vim fake with several datastores: VMFS and NFS
datastores with claimed, system and orphan folders, a template VM, a VM with a
disk on a second datastore, a vSAN datastore (UUID folders with friendly
names), and datastores that are skipped (not accessible, a vVol, a failing
browse, a name that does not exist). Also covers a paged property answer,
folders whose names differ only in case, the non-vCenter refusals, an
incomplete VM answer, a many-row answer through the real JSONFlux reducer, and
the parameter schema.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vmware_rest.composites import _datastore_orphans
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA,
    DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA,
)
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations import result_query as rq
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import VimFake, moref, operator

_OP_ID = "vmware.composite.datastore.orphan_dirs"
_VALIDATE_OUT = Draft202012Validator(DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA).validate
_READ_METHODS = {
    "RetrievePropertiesEx",
    "ContinueRetrievePropertiesEx",
    "SearchDatastoreSubFolders_Task",
}
_UUID_WEB = "5f1a0b2c-0000-0000-0000-00000000000a"
_UUID_GONE = "5f1a0b2c-0000-0000-0000-00000000000b"


class _Target:
    def __init__(self, product: str | None) -> None:
        self.name = "example-target"
        self.fingerprint = None if product is None else {"product": product, "reachable": True}


class _OrphanFake(VimFake):
    """The vim fake plus the datastore listing, vSAN friendly names and browse faults."""

    def __init__(self) -> None:
        super().__init__()
        self.gets: list[tuple[str, Any]] = []
        #: ds name -> {top-level folder path: friendly name} (vSAN).
        self.friendly: dict[str, dict[str, str]] = {}
        #: ds names whose browse fails with a permission fault.
        self.browse_fails: set[str] = set()

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return "/api" + path

    async def adapt_op_query(self, target: Any, query: Any, operator: Any) -> Any:
        return query

    async def _get_json(self, target: Any, path: str, *, operator: Any, params: Any) -> Any:
        self.gets.append((path, params))
        assert path == "/api/vcenter/datastore"
        names = (params or {}).get("filter.names")
        return [
            {"datastore": moid, "name": props["name"]}
            for (mo_type, moid), props in self.objects.items()
            if mo_type == "Datastore" and (not names or props["name"] in names)
        ]

    def _search(self, method: str, body: dict[str, Any]) -> dict[str, str]:
        name, _folder = self._split(body["datastorePath"])
        if name in self.browse_fails:
            return self._fault_task(
                "NoPermission", "Permission to perform this operation was denied."
            )
        task = super()._search(method, body)
        for row in self._tasks[task["value"]]["result"]:
            if row["folderPath"] != f"[{name}]":
                continue
            for entry in row["file"]:
                friendly = self.friendly.get(name, {}).get(entry["path"])
                if friendly is not None:
                    entry["friendlyName"] = friendly
        return task


def _datastore(
    conn: _OrphanFake,
    moid: str,
    name: str,
    *,
    ds_type: str = "VMFS",
    vms: tuple[str, ...] = (),
    accessible: bool = True,
) -> None:
    conn.add(
        "Datastore",
        moid,
        name=name,
        **{"summary.type": ds_type, "summary.accessible": accessible},
        browser=moref("HostDatastoreBrowser", "datastoreBrowser-" + moid),
        vm=[moref("VirtualMachine", vm) for vm in vms],
    )


def _fake() -> _OrphanFake:
    conn = _OrphanFake()
    _datastore(conn, "datastore-11", "ds-a", vms=("vm-1", "vm-2", "vm-3"))
    _datastore(conn, "datastore-12", "ds-b", ds_type="NFS", vms=("vm-2",))
    _datastore(conn, "datastore-13", "ds-down", accessible=False)
    _datastore(conn, "datastore-14", "ds-vvol", ds_type="VVOL")
    _datastore(conn, "datastore-15", "ds-locked")
    conn.browse_fails.add("ds-locked")
    conn.add(
        "VirtualMachine",
        "vm-1",
        name="app-01",
        **{
            "layoutEx.file": [
                {"name": "[ds-a] app-01/app-01.vmx"},
                {"name": "[ds-a] app-01/app-01.vmdk"},
            ],
            "config.files.vmPathName": "[ds-a] app-01/app-01.vmx",
        },
    )
    # db-01 lives on ds-b and has a second disk in a folder on ds-a.
    conn.add(
        "VirtualMachine",
        "vm-2",
        name="db-01",
        **{
            "layoutEx.file": [
                {"name": "[ds-b] db-01/db-01.vmx"},
                {"name": "[ds-a] db-01-data/db-01_1.vmdk"},
            ],
            "config.files.vmPathName": "[ds-b] db-01/db-01.vmx",
        },
    )
    # A template: it has no layoutEx, only its .vmx and its disk device.
    conn.add(
        "VirtualMachine",
        "vm-3",
        name="tmpl-linux",
        **{
            "config.template": True,
            "config.hardware.device": [
                {
                    "_typeName": "VirtualDisk",
                    "backing": {"fileName": "[ds-a] tmpl-linux/tmpl-linux.vmdk"},
                }
            ],
            "config.files.vmPathName": "[ds-a] tmpl-linux/tmpl-linux.vmtx",
        },
    )
    conn.files["ds-a"] = {
        "app-01": None,
        "app-01/app-01.vmx": 3_000,
        "app-01/app-01.vmdk": 1_000,
        "db-01-data": None,
        "db-01-data/db-01_1.vmdk": 50_000,
        "tmpl-linux": None,
        "tmpl-linux/tmpl-linux.vmtx": 2_000,
        "tmpl-linux/tmpl-linux.vmdk": 40_000,
        "old-vm": None,
        "old-vm/old-vm-flat.vmdk": 10_000,
        "old-vm/logs": None,
        "old-vm/logs/vmware.log": 500,
        "iso": None,
        "iso/esxi.iso": 400_000,
        ".sdd.sf": None,
        ".vSphere-HA": None,
        "contentlib-1234": None,
        "contentlib-1234/item.ovf": 100,
        "top-file.txt": 7,
    }
    conn.files["ds-b"] = {
        "db-01": None,
        "db-01/db-01.vmx": 2_000,
        "leftover": None,
        "leftover/leftover.vmdk": 9_000,
        "vmkdump": None,
    }
    conn.files["ds-down"] = {}
    conn.files["ds-locked"] = {"anything": None}
    conn.mtimes["ds-a"] = {
        "app-01/app-01.vmx": "2026-10-01T10:00:00Z",
        "old-vm/old-vm-flat.vmdk": "2025-01-02T03:04:05Z",
        "old-vm/logs/vmware.log": "2025-02-01T00:00:00Z",
        "iso/esxi.iso": "2026-03-01T00:00:00Z",
    }
    conn.mtimes["ds-b"] = {"leftover/leftover.vmdk": "2024-06-01T00:00:00Z"}
    return conn


def _add_vsan(conn: _OrphanFake) -> None:
    _datastore(conn, "datastore-16", "vsanDatastore", ds_type="vsan", vms=("vm-4",))
    conn.add(
        "VirtualMachine",
        "vm-4",
        name="web-01",
        **{
            "layoutEx.file": [{"name": f"[vsanDatastore] {_UUID_WEB}/web-01.vmx"}],
            "config.files.vmPathName": f"[vsanDatastore] {_UUID_WEB}/web-01.vmx",
        },
    )
    conn.files["vsanDatastore"] = {
        _UUID_WEB: None,
        f"{_UUID_WEB}/web-01.vmx": 1_000,
        _UUID_GONE: None,
        f"{_UUID_GONE}/gone-01.vmdk": 6_000,
        ".vsan.stats": None,
    }
    conn.friendly["vsanDatastore"] = {_UUID_WEB: "web-01", _UUID_GONE: "gone-01"}


async def _orphans(conn: VimFake, target: Any = None, **params: Any) -> dict[str, Any]:
    return await _datastore_orphans.datastore_orphan_dirs_composite(
        operator=operator(),
        target=target if target is not None else _Target("vcenter"),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


def _by_folder(out: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["datastore"], row["folder"]): row for row in out["directories"]}


# ---------------------------------------------------------------------------
# Classification across datastores
# ---------------------------------------------------------------------------


async def test_every_datastore_gets_classified_rows() -> None:
    conn = _fake()
    out = await _orphans(conn)
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["vm_check"] == "complete"
    rows = _by_folder(out)
    classes = {key: row["classification"] for key, row in rows.items()}
    assert classes == {
        ("ds-a", "app-01"): "claimed",
        ("ds-a", "db-01-data"): "claimed",
        ("ds-a", "tmpl-linux"): "claimed",
        ("ds-a", "old-vm"): "orphan",
        ("ds-a", "iso"): "orphan",
        ("ds-a", ".sdd.sf"): "system",
        ("ds-a", ".vSphere-HA"): "system",
        ("ds-a", "contentlib-1234"): "system",
        ("ds-b", "db-01"): "claimed",
        ("ds-b", "leftover"): "orphan",
        ("ds-b", "vmkdump"): "system",
    }
    assert out["datastore_count"] == 2
    assert out["count"] == 11
    assert (out["orphan_count"], out["system_count"], out["claimed_count"]) == (3, 4, 4)
    # Read-only: only property reads and browses.
    assert set(conn.methods()) <= _READ_METHODS


async def test_claimed_by_names_vms_templates_and_disks_on_a_second_datastore() -> None:
    rows = _by_folder(await _orphans(_fake()))
    assert rows[("ds-a", "app-01")]["claimed_by"] == ["app-01"]
    assert rows[("ds-a", "tmpl-linux")]["claimed_by"] == ["tmpl-linux"]
    # db-01's home is on ds-b; its second disk claims a folder on ds-a.
    assert rows[("ds-a", "db-01-data")]["claimed_by"] == ["db-01"]
    assert rows[("ds-b", "db-01")]["claimed_by"] == ["db-01"]
    assert rows[("ds-a", "old-vm")]["claimed_by"] == []


async def test_rows_have_sizes_counts_and_newest_change_first() -> None:
    out = await _orphans(_fake())
    old = _by_folder(out)[("ds-a", "old-vm")]
    # Only the files directly inside the folder count: ``old-vm/logs/vmware.log``
    # (500 bytes, newer) is in a sub-folder and stays out of the totals.
    assert old == {
        "datastore": "ds-a",
        "datastore_moid": "datastore-11",
        "folder": "old-vm",
        "datastore_path": "[ds-a] old-vm",
        "size_bytes": 10_000,
        "file_count": 1,
        "newest_modified": "2025-01-02T03:04:05Z",
        "classification": "orphan",
        "claimed_by": [],
    }
    stamps = [row["newest_modified"] for row in out["directories"]]
    dated = [stamp for stamp in stamps if stamp is not None]
    assert dated == sorted(dated, reverse=True)
    assert stamps[: len(dated)] == dated  # rows without a date come last
    # A plain file at the root is not a folder row.
    assert ("ds-a", "top-file.txt") not in _by_folder(out)


async def test_folders_that_differ_only_in_case_stay_apart() -> None:
    """VMFS and NFS paths are case-sensitive: ``Backup`` and ``backup`` are two folders."""
    conn = _fake()
    conn.files["ds-b"].update({"Backup": None, "Backup/a": 10, "backup": None, "backup/b": 20})
    conn.mtimes["ds-b"]["backup/b"] = "2026-05-05T00:00:00Z"
    rows = _by_folder(await _orphans(conn, datastores=["ds-b"]))
    upper = rows[("ds-b", "Backup")]
    lower = rows[("ds-b", "backup")]
    assert (upper["size_bytes"], upper["file_count"], upper["newest_modified"]) == (10, 1, None)
    assert (lower["size_bytes"], lower["file_count"], lower["newest_modified"]) == (
        20,
        1,
        "2026-05-05T00:00:00Z",
    )
    assert upper["datastore_path"] == "[ds-b] Backup"
    assert lower["datastore_path"] == "[ds-b] backup"


async def test_paged_property_answers_are_read_to_the_end() -> None:
    """A datastore or a VM on a later page is neither missed nor marked orphan."""
    conn = _fake()
    conn.page_size = 1
    out = await _orphans(conn)
    _VALIDATE_OUT(out)
    assert out["vm_check"] == "complete"
    assert out["datastore_count"] == 2
    classes = {key: row["classification"] for key, row in _by_folder(out).items()}
    assert classes[("ds-a", "app-01")] == "claimed"
    assert classes[("ds-a", "tmpl-linux")] == "claimed"
    assert classes[("ds-b", "db-01")] == "claimed"
    assert classes[("ds-b", "leftover")] == "orphan"
    assert {row["datastore"] for row in out["skipped"]} == {"ds-down", "ds-vvol", "ds-locked"}
    # Five datastores and three VMs: four plus two continuation pages, each
    # token used once on the session of the first call, nothing left behind.
    assert len(conn.continue_calls) == 6
    assert {session for _token, session in conn.continue_calls} == {"<session-1>"}
    assert conn._pending == {}
    assert set(conn.methods()) <= _READ_METHODS


async def test_one_vm_read_and_one_browse_per_datastore() -> None:
    conn = _fake()
    await _orphans(conn)
    vm_reads = [
        body
        for _path, body in conn.calls_to("RetrievePropertiesEx")
        if body["specSet"][0]["propSet"][0]["type"] == "VirtualMachine"
    ]
    assert len(vm_reads) == 1
    moids = [o["obj"]["value"] for o in vm_reads[0]["specSet"][0]["objectSet"]]
    assert moids == ["vm-1", "vm-2", "vm-3"]
    browses = conn.calls_to("SearchDatastoreSubFolders_Task")
    browsed = [body["datastorePath"] for _path, body in browses]
    assert browsed == ["[ds-a]", "[ds-b]", "[ds-locked]"]


# ---------------------------------------------------------------------------
# Skipped datastores
# ---------------------------------------------------------------------------


async def test_unreadable_datastores_are_skipped_with_a_reason() -> None:
    out = await _orphans(_fake())
    reasons = {row["datastore"]: row["reason"] for row in out["skipped"]}
    assert set(reasons) == {"ds-down", "ds-vvol", "ds-locked"}
    assert "not accessible" in reasons["ds-down"]
    assert "'VVOL' is not supported" in reasons["ds-vvol"]
    assert "Permission to perform this operation was denied" in reasons["ds-locked"]
    assert not any(row["datastore"] in reasons for row in out["directories"])


async def test_datastores_filter_reads_only_those_and_skips_unknown_names() -> None:
    conn = _fake()
    out = await _orphans(conn, datastores=["ds-b", "no-such-ds"])
    _VALIDATE_OUT(out)
    assert {row["datastore"] for row in out["directories"]} == {"ds-b"}
    assert out["skipped"] == [{"datastore": "no-such-ds", "reason": "no datastore with this name"}]
    assert conn.gets == [("/api/vcenter/datastore", {"filter.names": ["ds-b", "no-such-ds"]})]


async def test_vcenter_without_datastores_answers_empty() -> None:
    conn = _OrphanFake()
    out = await _orphans(conn)
    _VALIDATE_OUT(out)
    assert out["count"] == 0
    assert out["directories"] == []
    assert out["skipped"] == []
    assert out["vm_check"] == "complete"


# ---------------------------------------------------------------------------
# vSAN
# ---------------------------------------------------------------------------


async def test_vsan_folders_use_friendly_names_and_match_uuid_paths() -> None:
    conn = _fake()
    _add_vsan(conn)
    out = await _orphans(conn, datastores=["vsanDatastore"])
    _VALIDATE_OUT(out)
    rows = _by_folder(out)
    web = rows[("vsanDatastore", "web-01")]
    assert web["classification"] == "claimed"
    assert web["claimed_by"] == ["web-01"]
    assert web["datastore_path"] == f"[vsanDatastore] {_UUID_WEB}"
    gone = rows[("vsanDatastore", "gone-01")]
    assert gone["classification"] == "orphan"
    assert gone["size_bytes"] == 6_000
    assert rows[("vsanDatastore", ".vsan.stats")]["classification"] == "system"


# ---------------------------------------------------------------------------
# Incomplete answers and refusals
# ---------------------------------------------------------------------------


async def test_vm_whose_files_cannot_be_read_makes_the_check_incomplete() -> None:
    conn = _fake()
    conn.add("VirtualMachine", "vm-3", name="tmpl-linux")
    out = await _orphans(conn)
    _VALIDATE_OUT(out)
    assert out["vm_check"] == "incomplete"


async def test_vm_left_out_of_the_answer_makes_the_check_incomplete() -> None:
    conn = _fake()
    conn.omitted.add("vm-1")
    out = await _orphans(conn)
    assert out["vm_check"] == "incomplete"


@pytest.mark.parametrize(("product", "expected"), [("esxi", "esxi"), ("nsx", "nsx")])
async def test_non_vcenter_target_gets_a_typed_refusal(product: str, expected: str) -> None:
    conn = _fake()
    out = await _orphans(conn, target=_Target(product))
    _VALIDATE_OUT(out)
    assert out["status"] == "unsupported_host_target"
    assert out["target_product"] == expected
    assert "vCenter" in out["guidance"]
    assert out["directories"] == []
    assert conn.calls == []
    assert conn.gets == []


@pytest.mark.parametrize(
    ("name", "system"),
    [
        (".sdd.sf", True),
        (".locker-host01", True),
        (".naa.600508b1", True),
        ("contentlib-abc", True),
        ("vsantraces", True),
        ("VMKDUMP", True),
        ("fcd", True),
        ("catalog", True),
        ("old-vm", False),
        ("iso", False),
    ],
)
def test_system_folder_names(name: str, system: bool) -> None:
    assert _datastore_orphans.is_system_folder(name) is system


# ---------------------------------------------------------------------------
# Many rows: a result handle
# ---------------------------------------------------------------------------


def _many(count: int) -> _OrphanFake:
    conn = _fake()
    for i in range(count):
        conn.files["ds-b"][f"stale-{i:03}"] = None
        conn.files["ds-b"][f"stale-{i:03}/disk.vmdk"] = i
    return conn


class _MemoryValkey:
    """The two calls of the async Valkey client the result store uses."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture
def result_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """The real result store on an in-memory stand-in for Valkey."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: store)
    return store


@pytest.mark.usefixtures("result_store")
async def test_many_rows_reduce_to_a_handle_with_counts_and_skipped() -> None:
    """A long answer becomes a handle; the counts, the digest and skipped stay readable."""
    op = operator()
    out = await _orphans(_many(300))
    _VALIDATE_OUT(out)
    instructions = STORAGE_NETWORK_LLM_INSTRUCTIONS[_OP_ID]
    context = {
        "op_id": _OP_ID,
        "tenant_id": str(op.tenant_id),
        "operator_sub": op.sub,
        "result_scalars": instructions["result_scalars"],
        "result_digest": instructions["result_digest"],
    }

    reduced, handle = await JsonFluxReducer().reduce(
        out, DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA, context
    )

    assert handle is not None
    assert handle.total_rows == 311
    assert "directories" not in reduced
    assert reduced["count"] == 311
    assert reduced["orphan_count"] == 303
    assert reduced["status_counts"] == {"claimed": 4, "system": 4, "orphan": 303}
    assert reduced["vm_check"] == "complete"

    read = await run_result_query(
        op,
        handle.handle_id,
        ResultQuerySpec(filter=[{"field": "folder", "op": "=", "value": "stale-007"}]),
    )
    assert read["rows"] == [
        {
            "datastore": "ds-b",
            "datastore_moid": "datastore-12",
            "folder": "stale-007",
            "datastore_path": "[ds-b] stale-007",
            "size_bytes": 7,
            "file_count": 1,
            "newest_modified": None,
            "classification": "orphan",
            "claimed_by": [],
        }
    ]
    skipped = await run_result_query(op, handle.handle_id, ResultQuerySpec(path="/skipped"))
    assert {row["datastore"] for row in skipped["rows"]} == {"ds-down", "ds-vvol", "ds-locked"}


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


def test_parameter_schema_examples_validate() -> None:
    validator = Draft202012Validator(DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA)
    for example in DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA["examples"]:
        validator.validate(example)


@pytest.mark.parametrize(
    "params",
    [
        {"datastores": []},
        {"datastores": [""]},
        {"datastores": ["a", "a"]},
        {"datastores": "ds-a"},
        {"extra": 1},
    ],
)
def test_parameter_schema_refuses(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA).validate(params)
