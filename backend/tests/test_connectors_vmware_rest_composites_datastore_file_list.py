# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``vmware.composite.datastore.file.list`` (#3432).

Driven against the shared vim fake's datastore file tree, on a vCenter target
(name found with ``GET /vcenter/datastore``) and a standalone ESXi target (name
found on ``ha-datacenter``). Covers the file rows, the pattern, an empty folder,
a missing folder / datastore, the typed refusals, ``include_usage`` (folder
sums + VM users), a large listing (raw, and through the real JSONFlux reducer
to a result handle), a partial ESXi datastore answer, and the ESXi SOAP shapes
(one ``file`` entry that is not a list, a size given as text, the SOAP search
envelope).
"""

from __future__ import annotations

from typing import Any

import pytest
from defusedxml.ElementTree import fromstring
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vmware_rest.composites import _datastore_list
from meho_backplane.connectors.vmware_rest.composites._datastore_browse import Datastore
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    IncompleteAnswerError,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
    DATASTORE_FILE_LIST_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.soap import (
    build_search_datastore_envelope,
    parse_retrieve_result,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations import result_query as rq
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import VimFake, moref, operator

_OP_ID = "vmware.composite.datastore.file.list"
_VALIDATE_OUT = Draft202012Validator(DATASTORE_FILE_LIST_RESPONSE_SCHEMA).validate


class _Target:
    def __init__(self, product: str | None) -> None:
        self.name = "example-target"
        self.fingerprint = None if product is None else {"product": product, "reachable": True}


class _VcenterFake(VimFake):
    """The vim fake plus the ``GET /vcenter/datastore`` listing."""

    def __init__(self) -> None:
        super().__init__()
        self.gets: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return "/api" + path

    async def adapt_op_query(self, target: Any, query: Any, operator: Any) -> Any:
        return query

    async def _get_json(self, target: Any, path: str, *, operator: Any, params: Any) -> Any:
        self.gets.append((path, params))
        assert path == "/api/vcenter/datastore"
        return [
            {"datastore": moid, "name": props["name"]}
            for (mo_type, moid), props in self.objects.items()
            if mo_type == "Datastore"
        ]


def _tree(conn: VimFake) -> None:
    conn.files["demo-ds"] = {
        "iso": None,
        "iso/ks-host01.iso": 4_000_000,
        "iso/ks-host02.iso": 4_100_000,
        "iso/readme.txt": 10,
        "empty": None,
        "old-lab": None,
        "old-lab/disk-flat.vmdk": 10_000_000,
        "old-lab/logs": None,
        "old-lab/logs/vmware.log": 500,
        "live-vm": None,
        "live-vm/live-vm.vmdk": 2_000,
        "top.txt": 7,
    }
    conn.mtimes["demo-ds"] = {
        "iso/ks-host01.iso": "2026-10-01T10:00:00Z",
        "old-lab/disk-flat.vmdk": "2026-01-02T03:04:05Z",
        "old-lab/logs/vmware.log": "2026-02-01T00:00:00Z",
    }


def _add_datastore(
    conn: VimFake, moid: str, *, name: str = "demo-ds", ds_type: str = "VMFS", vms: Any = None
) -> None:
    conn.add(
        "Datastore",
        moid,
        name=name,
        **{"summary.type": ds_type},
        browser=moref("HostDatastoreBrowser", "browser-" + moid.replace("/", "-")),
        parent=moref("Folder", "group-s5"),
        vm=[moref("VirtualMachine", vm) for vm in (vms or {})],
    )
    for vm, props in (vms or {}).items():
        conn.add("VirtualMachine", vm, **props)


def _vcenter(*, ds_type: str = "VMFS", vms: Any = None) -> _VcenterFake:
    conn = _VcenterFake()
    _add_datastore(conn, "datastore-17", ds_type=ds_type, vms=vms)
    conn.add("Folder", "group-s5", parent=moref("Datacenter", "datacenter-2"))
    _tree(conn)
    return conn


def _esxi() -> VimFake:
    conn = VimFake()
    _add_datastore(conn, "nfs.example:/exports/vol", ds_type="NFS")
    conn.add("Datastore", "local-ds-id", name="local-ds")
    conn.add(
        "Datacenter",
        "ha-datacenter",
        datastore=[
            moref("Datastore", "nfs.example:/exports/vol"),
            moref("Datastore", "local-ds-id"),
        ],
    )
    conn.add("Folder", "group-s5", parent=moref("Datacenter", "ha-datacenter"))
    _tree(conn)
    return conn


_LIVE_VM = {
    "vm-5": {
        "name": "example-vm",
        "layoutEx.file": [{"name": "[demo-ds] live-vm/live-vm.vmdk"}],
        "config.files.vmPathName": "[demo-ds] live-vm/live-vm.vmx",
    }
}


async def _list(conn: VimFake, target: Any = None, **params: Any) -> dict[str, Any]:
    params.setdefault("datastore", "demo-ds")
    return await _datastore_list.datastore_file_list_composite(
        operator=operator(),
        target=target if target is not None else _Target("vcenter"),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# vCenter
# ---------------------------------------------------------------------------


async def test_vcenter_lists_files_with_size_and_modified() -> None:
    conn = _vcenter()
    out = await _list(conn, path="iso")
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["datastore_moid"] == "datastore-17"
    assert out["datastore_type"] == "VMFS"
    assert out["datastore_path"] == "[demo-ds] iso"
    assert out["count"] == 3
    first = out["entries"][0]
    assert first == {
        "name": "ks-host01.iso",
        "path": "iso/ks-host01.iso",
        "kind": "file",
        "file_type": "FileInfo",
        "size_bytes": 4_000_000,
        "modified": "2026-10-01T10:00:00Z",
    }
    assert out["vm_check"] is None
    # The name was found by the REST listing, filtered by name.
    assert conn.gets == [("/api/vcenter/datastore", {"filter.names": ["demo-ds"]})]
    _, body = conn.calls_to("SearchDatastore_Task")[0]
    assert body["datastorePath"] == "[demo-ds] iso"
    # Read-only: no write method was called.
    assert set(conn.methods()) <= {"RetrievePropertiesEx", "SearchDatastore_Task"}


async def test_pattern_keeps_matching_names() -> None:
    out = await _list(_vcenter(), path="iso", pattern="ks-host??.iso")
    assert [e["name"] for e in out["entries"]] == ["ks-host01.iso", "ks-host02.iso"]
    assert out["pattern"] == "ks-host??.iso"


async def test_root_listing_has_folders_first_without_size() -> None:
    out = await _list(_vcenter())
    _VALIDATE_OUT(out)
    assert out["path"] == ""
    assert out["datastore_path"] == "[demo-ds]"
    kinds = {e["name"]: (e["kind"], e["size_bytes"]) for e in out["entries"]}
    assert kinds["iso"] == ("folder", None)
    assert kinds["top.txt"] == ("file", 7)


async def test_empty_folder_is_ok_with_no_entries() -> None:
    out = await _list(_vcenter(), path="empty", include_usage=True)
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["entries"] == []
    assert out["count"] == 0


async def test_missing_folder_is_not_found() -> None:
    out = await _list(_vcenter(), path="no-such-folder")
    _VALIDATE_OUT(out)
    assert out["status"] == "not_found"
    assert "does not exist" in out["guidance"]


async def test_unknown_datastore_is_not_found() -> None:
    conn = _vcenter()
    out = await _list(conn, datastore="other-ds")
    _VALIDATE_OUT(out)
    assert out["status"] == "not_found"
    assert "SearchDatastore_Task" not in conn.methods()


async def test_vcenter_moid_works_as_datastore() -> None:
    out = await _list(_vcenter(), datastore="datastore-17", path="iso")
    assert out["status"] == "ok"
    assert out["datastore"] == "demo-ds"


async def test_two_datastores_with_one_name_is_ambiguous() -> None:
    conn = _vcenter()
    _add_datastore(conn, "datastore-18")
    out = await _list(conn)
    _VALIDATE_OUT(out)
    assert out["status"] == "ambiguous"
    assert "datastore-17" in out["guidance"] and "datastore-18" in out["guidance"]


@pytest.mark.parametrize("path", ["../etc", "a//b", "iso/*", "a[1]", "./iso"])
async def test_bad_path_is_refused_before_any_call(path: str) -> None:
    conn = _vcenter()
    out = await _list(conn, path=path)
    _VALIDATE_OUT(out)
    assert out["status"] == "invalid_request"
    assert conn.calls == [] and conn.gets == []


async def test_bad_pattern_is_refused() -> None:
    out = await _list(_vcenter(), pattern="iso/*.iso")
    assert out["status"] == "invalid_request"


async def test_other_product_gets_typed_refusal() -> None:
    conn = _vcenter()
    out = await _list(conn, target=_Target("nsx"))
    _VALIDATE_OUT(out)
    assert out["status"] == "unsupported_host_target"
    assert out["target_product"] == "nsx"
    assert conn.calls == []


async def test_include_usage_sums_folders_and_names_vm_users() -> None:
    conn = _vcenter(vms=_LIVE_VM)
    out = await _list(conn, include_usage=True)
    _VALIDATE_OUT(out)
    rows = {e["name"]: e for e in out["entries"]}
    assert rows["old-lab"]["size_bytes"] == 10_000_500
    assert rows["old-lab"]["file_count"] == 2
    assert rows["old-lab"]["latest_modified"] == "2026-02-01T00:00:00Z"
    assert rows["old-lab"]["vms"] == []
    assert rows["live-vm"]["vms"] == ["example-vm"]
    assert rows["empty"]["size_bytes"] == 0
    assert rows["top.txt"]["vms"] == []
    assert out["vm_check"] == "complete"
    assert "SearchDatastoreSubFolders_Task" in conn.methods()


async def test_include_usage_reports_incomplete_vm_answer() -> None:
    conn = _vcenter(vms={"vm-6": {"name": "broken-vm"}})
    out = await _list(conn, include_usage=True)
    assert out["vm_check"] == "incomplete"


async def test_include_usage_skips_vm_check_on_vsan() -> None:
    conn = _vcenter(ds_type="vsan", vms=_LIVE_VM)
    out = await _list(conn, include_usage=True)
    _VALIDATE_OUT(out)
    assert out["vm_check"] == "not_supported"
    assert all(e["vms"] is None for e in out["entries"])
    # Folder sums still work.
    assert {e["name"]: e["size_bytes"] for e in out["entries"]}["old-lab"] == 10_000_500


def _many_files(count: int) -> _VcenterFake:
    conn = _vcenter()
    for i in range(count):
        conn.files["demo-ds"][f"iso/file-{i:03}.iso"] = i
    return conn


async def test_many_files_return_all_rows_for_the_reducer() -> None:
    out = await _list(_many_files(500), path="iso")
    assert out["count"] == 503
    assert len(out["entries"]) == 503
    _VALIDATE_OUT(out)


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
async def test_many_files_reduce_to_a_handle_that_keeps_the_status_fields() -> None:
    """A long listing goes through the real reducer: a handle, the status fields inline.

    The reducer context carries the op's registered ``result_scalars`` hint (the
    dict the dispatcher lifts off the descriptor), so the fields an agent needs
    before a drill-in stay next to the handle. The stored rows are read back
    with ``result_query``, the way an agent does it.
    """
    op = operator()
    out = await _list(_many_files(500), path="iso")
    context = {
        "op_id": _OP_ID,
        "tenant_id": str(op.tenant_id),
        "operator_sub": op.sub,
        "result_scalars": STORAGE_NETWORK_LLM_INSTRUCTIONS[_OP_ID]["result_scalars"],
    }

    reduced, handle = await JsonFluxReducer().reduce(
        out, DATASTORE_FILE_LIST_RESPONSE_SCHEMA, context
    )

    assert handle is not None
    assert handle.total_rows == 503
    assert handle.fetch_more is not None
    assert handle.fetch_more.drill_in.available is True
    assert "entries" not in reduced
    assert reduced["row_count"] == 503
    for key in STORAGE_NETWORK_LLM_INSTRUCTIONS[_OP_ID]["result_scalars"]["keys"]:
        assert reduced[key] == out[key], key
    assert reduced["status"] == "ok"
    assert reduced["count"] == 503
    assert reduced["datastore_moid"] == "datastore-17"
    assert reduced["datastore_path"] == "[demo-ds] iso"

    read = await run_result_query(
        op,
        handle.handle_id,
        ResultQuerySpec(filter=[{"field": "name", "op": "=", "value": "file-007.iso"}]),
    )
    assert read["coverage"] == "complete"
    assert read["rows"] == [
        {
            "name": "file-007.iso",
            "path": "iso/file-007.iso",
            "kind": "file",
            "file_type": "FileInfo",
            "size_bytes": 7,
            "modified": None,
        }
    ]


# ---------------------------------------------------------------------------
# Standalone ESXi
# ---------------------------------------------------------------------------


async def test_esxi_finds_the_name_on_ha_datacenter() -> None:
    conn = _esxi()
    out = await _list(conn, target=_Target("esxi"), path="iso", pattern="*.iso")
    _VALIDATE_OUT(out)
    assert out["status"] == "ok"
    assert out["datastore_moid"] == "nfs.example:/exports/vol"
    assert [e["name"] for e in out["entries"]] == ["ks-host01.iso", "ks-host02.iso"]
    _, body = conn.calls_to("SearchDatastore_Task")[0]
    assert body["searchSpec"]["matchPattern"] == ["*.iso"]


async def test_esxi_unknown_name_is_not_found() -> None:
    out = await _list(_esxi(), target=_Target("esxi"), datastore="other-ds")
    assert out["status"] == "not_found"


async def test_esxi_partial_datastore_answer_is_not_treated_as_absent() -> None:
    """A datastore name the session may not read: an error, never ``not_found``."""
    conn = _esxi()
    conn.unreadable[("Datastore", "local-ds-id")] = {"name"}
    with pytest.raises(IncompleteAnswerError, match="only part of its datastore list"):
        await _list(conn, target=_Target("esxi"), datastore="local-ds")
    assert "SearchDatastore_Task" not in conn.methods()


async def test_esxi_include_usage() -> None:
    out = await _list(_esxi(), target=_Target("esxi"), include_usage=True)
    assert out["status"] == "ok"
    assert {e["name"]: e["size_bytes"] for e in out["entries"]}["old-lab"] == 10_000_500
    assert out["vm_check"] == "complete"


def test_soap_search_result_shapes_become_rows() -> None:
    """One SOAP ``file`` entry is a dict, and its size is text: both become a normal row."""
    xml = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><soapenv:Body>'
        '<RetrievePropertiesExResponse xmlns="urn:vim25"><returnval><objects>'
        '<obj type="Task">haTask-1</obj><propSet><name>info</name>'
        '<val xsi:type="TaskInfo"><state>success</state>'
        '<result xsi:type="HostDatastoreBrowserSearchResults">'
        '<datastore type="Datastore">nfs.example:/exports/vol</datastore>'
        "<folderPath>[demo-ds] iso</folderPath>"
        '<file xsi:type="IsoImageFileInfo"><path>ks-host01.iso</path>'
        "<fileSize>4000000</fileSize><modification>2026-10-01T10:00:00Z</modification>"
        "</file></result></val></propSet></objects></returnval>"
        "</RetrievePropertiesExResponse></soapenv:Body></soapenv:Envelope>"
    )
    parsed = parse_retrieve_result(xml)
    info = unwrap_vim_value(parsed["objects"][0]["propSet"][0]["val"])
    ds = Datastore("nfs.example:/exports/vol", "demo-ds", "NFS", "b", "ha-datacenter", [])
    rows = _datastore_list._entries(ds, "iso", [info["result"]])
    assert rows == [
        {
            "name": "ks-host01.iso",
            "path": "iso/ks-host01.iso",
            "kind": "file",
            "file_type": "IsoImageFileInfo",
            "size_bytes": 4_000_000,
            "modified": "2026-10-01T10:00:00Z",
        }
    ]


def test_build_search_datastore_envelope_follows_wsdl_order() -> None:
    spec = {
        "_typeName": "HostDatastoreBrowserSearchSpec",
        "details": {
            "_typeName": "FileQueryFlags",
            "fileType": True,
            "fileSize": True,
            "modification": True,
            "fileOwner": False,
        },
        "sortFoldersFirst": True,
        "matchPattern": ["*.iso"],
    }
    envelope = build_search_datastore_envelope(
        "ha-datastorebrowser", "SearchDatastoreSubFolders_Task", "[demo-ds] a&b", spec
    )
    method = fromstring(envelope)[0][0]
    assert method.tag == "{urn:vim25}SearchDatastoreSubFolders_Task"
    children = [child.tag.rpartition("}")[2] for child in method]
    assert children == ["_this", "datastorePath", "searchSpec"]
    assert method[0].get("type") == "HostDatastoreBrowser"
    assert method[1].text == "[demo-ds] a&b"
    search_spec = method[2]
    assert [c.tag.rpartition("}")[2] for c in search_spec] == [
        "details",
        "matchPattern",
        "sortFoldersFirst",
    ]
    assert [c.tag.rpartition("}")[2] for c in search_spec[0]] == [
        "fileType",
        "fileSize",
        "modification",
        "fileOwner",
    ]
    assert search_spec[1].text == "*.iso"
    assert "_typeName" not in envelope


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


def test_parameter_schema_examples_validate() -> None:
    validator = Draft202012Validator(DATASTORE_FILE_LIST_PARAMETER_SCHEMA)
    for example in DATASTORE_FILE_LIST_PARAMETER_SCHEMA["examples"]:
        validator.validate(example)
    validator.validate({"datastore": "demo-ds", "path": ""})


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"datastore": ""},
        {"datastore": "demo-ds", "path": "iso/*"},
        {"datastore": "demo-ds", "pattern": "a/b"},
        {"datastore": "demo-ds", "extra": 1},
    ],
)
def test_parameter_schema_refuses(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(DATASTORE_FILE_LIST_PARAMETER_SCHEMA).validate(params)
