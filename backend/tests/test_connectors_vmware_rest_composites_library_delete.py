# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``content_library.delete`` / ``content_library.item.delete`` (#3339 / #3331).

A recording REST double stands in for the connector session (``_get_json`` /
``_post_json`` mounted through ``mount_op_path``). It normalises every URL
path the way httpx does before sending (``..`` segments collapse), so a test
can prove that no input reaches another object. The vim reads of the
mounted-item check (``Datastore.vm`` + the VMs' devices) go to the shared vim
fake. The #2254 sub-op gate is replaced by a recorder.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.composites import _library_delete, _write
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from tests._vmware_teardown_fake import GateRecorder, VimFake, moref, operator, parked, preview_ctx

_LIB = "11111111-1111-1111-1111-111111111111"
_SUB = "22222222-2222-2222-2222-222222222222"
_ITEM = "33333333-3333-3333-3333-333333333333"
_ITEM_B = "44444444-4444-4444-4444-444444444444"


def _uuid(n: int) -> str:
    return f"{n:08d}-0000-0000-0000-{n:012d}"


def _not_found(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://vc.example.test{path}")
    return httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )


class _RestFake:
    """Recording REST double over an in-memory content-library store + a vim fake."""

    def __init__(self) -> None:
        self.libraries: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.vms: dict[str, dict[str, Any]] = {"vm-42": {"name": "prod-db"}}
        self.subscriptions: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.apply_deletes = True
        self.delete_fault: Exception | None = None
        self.vim = VimFake()
        self.vim.add("Datastore", "datastore-17", vm=[])

    async def _post_vmomi_json(self, *args: Any, **kwargs: Any) -> Any:
        return await self.vim._post_vmomi_json(*args, **kwargs)

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    @staticmethod
    def _wire(path: str) -> str:
        """The path httpx actually sends (dot segments removed)."""
        base, _, query = path.partition("?")
        real = httpx.URL("https://vc.example.test" + base).path
        return f"{real}?{query}" if query else real

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        real = self._wire(path)
        self.calls.append(("GET", real, params))
        parts = real.removeprefix("/api/").split("/")
        if parts == ["content", "subscribed-library"]:
            return [i for i, lib in self.libraries.items() if lib["type"] == "SUBSCRIBED"]
        if parts[:2] == ["content", "library"] and parts[3:] == ["subscriptions"]:
            return self.subscriptions.get(parts[2], [])
        if parts[:2] == ["vcenter", "vm"] and len(parts) == 3 and parts[2] in self.vms:
            return self.vms[parts[2]]
        if parts[1] in ("library", "subscribed-library", "local-library") and len(parts) == 3:
            lib = self.libraries.get(parts[2])
            if lib is None:
                raise _not_found(real)
            return {"id": parts[2], **lib}
        if parts[1:3] == ["library", "item"] and len(parts) == 4:
            item = self.items.get(parts[3])
            if item is None:
                raise _not_found(real)
            return {"id": parts[3], **item}
        if parts[1:3] == ["library", "item"] and parts[4:] == ["file"]:
            return self.items[parts[3]].get("files", [])
        raise _not_found(real)

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
        **_: Any,
    ) -> Any:
        real = self._wire(path)
        self.calls.append((verb, real, json))
        if real == "/api/content/library?action=find":
            return [i for i, lib in self.libraries.items() if lib["name"] == json["name"]]
        if real == "/api/content/library/item?action=find":
            return [
                i
                for i, item in self.items.items()
                if item["library_id"] == json["library_id"]
                and ("name" not in json or item["name"] == json["name"])
            ]
        if verb == "DELETE":
            if self.delete_fault is not None:
                raise self.delete_fault
            target_id = real.rsplit("/", 1)[-1]
            if self.apply_deletes:
                for store in (self.libraries, self.items, self.vms):
                    store.pop(target_id, None)
            return {}
        raise AssertionError(f"unexpected {verb} {real}")

    def deletes(self) -> list[str]:
        return [path for verb, path, _ in self.calls if verb == "DELETE"]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _store(*, items: int = 0, published: bool = False) -> _RestFake:
    conn = _RestFake()
    conn.libraries[_LIB] = {
        "name": "demo-lib",
        "type": "LOCAL",
        "publish_info": {"published": published},
        "storage_backings": [{"type": "DATASTORE", "datastore_id": "datastore-17"}],
    }
    for n in range(items):
        conn.items[_uuid(n)] = {
            "name": f"appliance-{n}",
            "type": "ovf",
            "size": 1000 + n,
            "library_id": _LIB,
        }
    return conn


def _mount(conn: _RestFake, item_id: str, *, vm: str = "vm-7") -> None:
    """A powered-off VM whose CD-ROM uses an ISO file of *item_id*."""
    conn.vim.add("Datastore", "datastore-17", vm=[moref("VirtualMachine", vm)])
    conn.vim.add(
        "VirtualMachine",
        vm,
        name="iso-user",
        **{
            "config.hardware.device": [
                {
                    "_typeName": "VirtualCdrom",
                    "backing": {
                        "_typeName": "VirtualCdromIsoBackingInfo",
                        "fileName": f"[demo-ds] contentlib-{_LIB}/{item_id}/media.iso",
                    },
                }
            ]
        },
    )


async def _lib_delete(conn: _RestFake, **params: Any) -> Any:
    return await _library_delete.content_library_delete_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


async def _item_delete(conn: _RestFake, **params: Any) -> Any:
    return await _library_delete.content_library_item_delete_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


# ---------------------------------------------------------------------------
# Strict ids (#3925 review, blocker 1): no id can point a request elsewhere.
# ---------------------------------------------------------------------------

_TRAVERSAL_IDS = [
    "../../../vcenter/vm/vm-42",
    f"../local-library/{_LIB}",
    f"{_LIB}/../x",
    f"{_LIB}%2F..",
    "L1",
    "",
]


@pytest.mark.parametrize("bad", _TRAVERSAL_IDS)
async def test_item_delete_refuses_a_non_uuid_id_before_any_request(
    gate: GateRecorder, bad: str
) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA).validate(
            {"item_id": bad}
        )
    conn = _store(items=1)
    out = await _item_delete(conn, item_id=bad)
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    assert gate.calls == []
    assert "vm-42" in conn.vms


@pytest.mark.parametrize("bad", _TRAVERSAL_IDS)
async def test_library_delete_refuses_a_non_uuid_id_before_any_request(
    gate: GateRecorder, bad: str
) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA).validate({"library_id": bad})
    conn = _store(items=1)
    out = await _lib_delete(conn, library_id=bad, delete_items=True)
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    assert _LIB in conn.libraries


async def test_review_probe_item_traversal_cannot_delete_a_vm(gate: GateRecorder) -> None:
    """The reviewer's proof case: refused by the schema AND the handler, nothing sent."""
    params = {"item_id": "../../../vcenter/vm/vm-42"}
    assert list(
        Draft202012Validator(CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA).iter_errors(params)
    )
    conn = _store()
    out = await _item_delete(conn, **params)
    assert out["status"] == "invalid_request"
    assert ("DELETE", "/api/vcenter/vm/vm-42") not in [(v, p) for v, p, _ in conn.calls]
    assert "vm-42" in conn.vms


async def test_review_probe_library_traversal_cannot_skip_the_item_check(
    gate: GateRecorder,
) -> None:
    params = {"library_id": f"../local-library/{_LIB}"}
    assert list(Draft202012Validator(CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA).iter_errors(params))
    conn = _store(items=1)
    out = await _lib_delete(conn, **params)
    assert out["status"] == "invalid_request"
    assert conn.deletes() == []
    assert _LIB in conn.libraries


# ---------------------------------------------------------------------------
# content_library.delete
# ---------------------------------------------------------------------------


async def test_library_delete_empty_local(gate: GateRecorder) -> None:
    conn = _store()
    out = await _lib_delete(conn, library_id=_LIB)
    assert out["status"] == "deleted"
    assert out["object"]["name"] == "demo-lib"
    assert out["object"]["item_count"] == 0
    assert conn.deletes() == [f"/api/content/local-library/{_LIB}"]
    assert [c["op_id"] for c in gate.calls] == ["DELETE:/content/local-library/{libraryId}"]
    Draft202012Validator(CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA).validate(out)


async def test_library_delete_subscribed_uses_its_own_path(gate: GateRecorder) -> None:
    conn = _store()
    conn.libraries[_SUB] = {"name": "demo-sub", "type": "SUBSCRIBED", "subscription_info": {}}
    out = await _lib_delete(conn, library_name="demo-sub")
    assert out["status"] == "deleted"
    assert conn.deletes() == [f"/api/content/subscribed-library/{_SUB}"]


async def test_library_delete_non_empty_needs_delete_items(gate: GateRecorder) -> None:
    conn = _store(items=2)
    refused = await _lib_delete(conn, library_id=_LIB)
    assert refused["status"] == "precondition_failed"
    assert "delete_items=true" in refused["guidance"]
    assert conn.deletes() == []
    assert gate.calls == []
    out = await _lib_delete(conn, library_id=_LIB, delete_items=True)
    assert out["status"] == "deleted"


async def test_library_delete_refuses_while_a_library_subscribes(gate: GateRecorder) -> None:
    conn = _store(published=True)
    conn.libraries[_SUB] = {
        "name": "demo-sub",
        "type": "SUBSCRIBED",
        "subscription_info": {
            "subscription_url": f"https://vc.example.test/cls/vcsp/lib/{_LIB}/lib.json"
        },
    }
    out = await _lib_delete(conn, library_id=_LIB, delete_items=True)
    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [{"kind": "subscribed_library", "id": _SUB, "name": "demo-sub"}]
    assert conn.deletes() == []


async def test_library_delete_refuses_a_subscriber_on_another_vcenter(gate: GateRecorder) -> None:
    conn = _store(published=True)
    conn.subscriptions[_LIB] = [
        {
            "subscription": "sub-1",
            "subscribed_library": _uuid(9),
            "subscribed_library_name": "remote-sub",
            "subscribed_library_vcenter_hostname": "vc-b.example.test",
        }
    ]
    out = await _lib_delete(conn, library_id=_LIB)
    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["vcenter"] == "vc-b.example.test"


async def test_library_delete_refuses_while_a_vm_mounts_an_item(gate: GateRecorder) -> None:
    conn = _store(items=2)
    _mount(conn, _uuid(1))
    out = await _lib_delete(conn, library_id=_LIB, delete_items=True)
    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["moid"] == "vm-7"
    assert "unmount" in out["guidance"]
    assert conn.deletes() == []


async def test_library_delete_refuses_on_a_partial_vm_answer(gate: GateRecorder) -> None:
    conn = _store(items=1)
    _mount(conn, _uuid(5))
    conn.vim.unreadable[("VirtualMachine", "vm-7")] = {"config.hardware.device"}
    out = await _lib_delete(conn, library_id=_LIB, delete_items=True)
    assert out["status"] == "precondition_failed"
    assert "only part" in out["guidance"]


async def test_library_not_on_datastores_with_items_is_refused(gate: GateRecorder) -> None:
    conn = _store(items=1)
    conn.libraries[_LIB]["storage_backings"] = [{"type": "OTHER", "storage_uri": "nfs://x/y"}]
    out = await _lib_delete(conn, library_id=_LIB, delete_items=True)
    assert out["status"] == "precondition_failed"
    assert "not stored only on datastores" in out["guidance"]


async def test_library_delete_unsupported_type(gate: GateRecorder) -> None:
    conn = _store()
    conn.libraries[_LIB]["type"] = "SOMETHING_NEW"
    out = await _lib_delete(conn, library_id=_LIB)
    assert out["status"] == "precondition_failed"
    assert "unsupported type" in out["guidance"]


async def test_library_delete_absent_and_ambiguous(gate: GateRecorder) -> None:
    conn = _store()
    assert (await _lib_delete(conn, library_id=_uuid(404)))["status"] == "unchanged"
    assert (await _lib_delete(conn, library_name="no-such-lib"))["status"] == "unchanged"
    conn.libraries[_uuid(77)] = {"name": "demo-lib", "type": "LOCAL"}
    ambiguous = await _lib_delete(conn, library_name="demo-lib")
    assert ambiguous["status"] == "invalid_request"
    Draft202012Validator(CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA).validate(ambiguous)
    assert conn.deletes() == []


async def test_library_delete_still_present_and_fault(gate: GateRecorder) -> None:
    conn = _store()
    conn.apply_deletes = False
    assert (await _lib_delete(conn, library_id=_LIB))["status"] == "still_present"
    conn.delete_fault = httpx.ConnectError("boom")
    with pytest.raises(httpx.ConnectError):
        await _lib_delete(conn, library_id=_LIB)


async def test_library_delete_park_keeps_delete_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = parked("DELETE:/content/local-library/{libraryId}")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    conn = _store()
    assert await _lib_delete(conn, library_id=_LIB) is verdict
    assert conn.deletes() == []


async def test_library_preview_lists_items_and_sizes() -> None:
    conn = _store(items=3)
    effect = await _library_delete.content_library_delete_preview(
        preview_ctx({"library_id": _LIB}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["item_count"] == 3
    assert block["object"]["total_size_bytes"] == 3003
    assert {c["name"] for c in block["children"]} == {"appliance-0", "appliance-1", "appliance-2"}
    assert block["refusal"]["status"] == "precondition_failed"
    assert conn.deletes() == []


def test_library_schema_needs_a_reference() -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA).validate(
            {"delete_items": True}
        )
    Draft202012Validator(CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA).validate({"library_id": _LIB})


# ---------------------------------------------------------------------------
# content_library.item.delete
# ---------------------------------------------------------------------------


async def test_item_delete_by_id(gate: GateRecorder) -> None:
    conn = _store(items=1)
    out = await _item_delete(conn, item_id=_uuid(0))
    assert out["status"] == "deleted"
    assert out["object"]["library_name"] == "demo-lib"
    assert out["object"]["size_bytes"] == 1000
    assert conn.deletes() == [f"/api/content/library/item/{_uuid(0)}"]
    assert [c["op_id"] for c in gate.calls] == ["DELETE:/content/library/item/{libraryItemId}"]
    Draft202012Validator(CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA).validate(out)


async def test_item_delete_by_name(gate: GateRecorder) -> None:
    conn = _store(items=2)
    out = await _item_delete(conn, item_name="appliance-1", library_name="demo-lib")
    assert out["status"] == "deleted"
    assert conn.deletes() == [f"/api/content/library/item/{_uuid(1)}"]


async def test_item_delete_ambiguous_name(gate: GateRecorder) -> None:
    conn = _store(items=1)
    conn.items[_ITEM_B] = {"name": "appliance-0", "type": "ovf", "size": 1, "library_id": _LIB}
    out = await _item_delete(conn, item_name="appliance-0", library_id=_LIB)
    assert out["status"] == "invalid_request"
    assert conn.deletes() == []


async def test_item_delete_refuses_subscribed_library_item(gate: GateRecorder) -> None:
    conn = _store()
    conn.libraries[_SUB] = {"name": "demo-sub", "type": "SUBSCRIBED"}
    conn.items[_ITEM] = {"name": "tkr", "type": "ovf", "size": 5, "library_id": _SUB}
    out = await _item_delete(conn, item_id=_ITEM)
    assert out["status"] == "precondition_failed"
    assert "subscribed library" in out["guidance"]
    assert conn.deletes() == []


@pytest.mark.parametrize("library_id", [None, "not-a-uuid", _uuid(404)])
async def test_item_delete_refuses_when_its_library_cannot_be_read_back(
    gate: GateRecorder, library_id: str | None
) -> None:
    conn = _store()
    conn.items[_ITEM] = {"name": "x", "type": "iso", "size": 5, "library_id": library_id}
    out = await _item_delete(conn, item_id=_ITEM)
    assert out["status"] == "precondition_failed"
    assert "local library" in out["guidance"]
    assert conn.deletes() == []


async def test_item_delete_refuses_while_a_vm_mounts_it(gate: GateRecorder) -> None:
    conn = _store(items=1)
    _mount(conn, _uuid(0))
    out = await _item_delete(conn, item_id=_uuid(0))
    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["name"] == "iso-user"
    assert conn.deletes() == []


async def test_item_delete_absent_is_unchanged(gate: GateRecorder) -> None:
    conn = _store(items=1)
    assert (await _item_delete(conn, item_id=_uuid(404)))["status"] == "unchanged"
    by_name = await _item_delete(conn, item_name="missing", library_id=_LIB)
    assert by_name["status"] == "unchanged"
    assert conn.deletes() == []


async def test_item_delete_still_present_fault_and_park(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _store(items=1)
    conn.apply_deletes = False
    assert (await _item_delete(conn, item_id=_uuid(0)))["status"] == "still_present"
    conn.delete_fault = httpx.ConnectError("boom")
    with pytest.raises(httpx.ConnectError):
        await _item_delete(conn, item_id=_uuid(0))
    verdict = parked("DELETE:/content/library/item/{libraryItemId}")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    fresh = _store(items=1)
    assert await _item_delete(fresh, item_id=_uuid(0)) is verdict
    assert fresh.deletes() == []


async def test_item_preview_lists_files() -> None:
    conn = _store(items=1)
    conn.items[_uuid(0)]["files"] = [
        {"name": "appliance.ovf", "size": 10},
        {"name": "disk-0.vmdk", "size": 990},
    ]
    effect = await _library_delete.content_library_item_delete_preview(
        preview_ctx({"item_id": _uuid(0)}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["children"] == [
        {"kind": "file", "name": "appliance.ovf", "size_bytes": 10},
        {"kind": "file", "name": "disk-0.vmdk", "size_bytes": 990},
    ]


@pytest.mark.parametrize("params", [{"item_name": "x"}, {"library_id": _LIB}, {}])
def test_item_schema_rejects(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA).validate(params)
