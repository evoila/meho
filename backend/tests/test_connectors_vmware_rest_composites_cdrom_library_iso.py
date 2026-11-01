# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the content-library ISO mount and unmount on a VM.

A REST fake stands in for the connector session. It answers the library
``?action=find`` calls, the ``iso/image`` mount and unmount, and the CD drive
reads with saved vSphere REST shapes. No live vCenter. The policy check of
the write seam is replaced by a recorder, so the tests show what is checked
and that a held write never reaches vCenter.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _vm_cdrom_library_iso as iso
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    _GOVERNED_SUBOP_MANIFEST,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import composite_resume_binding

_VM = "vm-42"
_ITEM = "0b6e3e0c-1f2a-4c55-9a51-2f6d7a8b9c01"
_LIBRARY = "5d1c7f20-8e2b-4a3f-b6c4-0a9e8d7c6b51"
_MOUNT = "/api/vcenter/iso/image?action=mount"
_UNMOUNT = "/api/vcenter/iso/image?action=unmount"
_FIND_ITEM = "/api/content/library/item?action=find"
_FIND_LIBRARY = "/api/content/library?action=find"
_CDROMS = f"/api/vcenter/vm/{_VM}/hardware/cdrom"


def _operator() -> Operator:
    return Operator(
        sub="op-library-iso",
        name="Library ISO Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0c3"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-test"


def _status_error(path: str, status: int, body: dict[str, Any]) -> httpx.HTTPError:
    request = httpx.Request("POST", f"https://vc.test{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _RestFake:
    """Recording double for the connector's REST session."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.gets: dict[str, Any] = {_CDROMS: [{"cdrom": "3000"}]}
        self.gets[f"{_CDROMS}/3000"] = {
            "type": "IDE",
            "label": "CD/DVD drive 1",
            "ide": {"primary": True, "master": True},
            "backing": {"type": "CLIENT_DEVICE"},
            "state": "NOT_CONNECTED",
            "start_connected": False,
            "allow_guest_control": False,
        }
        self.post_results: dict[str, Any] = {
            _MOUNT: "16002",
            _UNMOUNT: {},
            _FIND_ITEM: [_ITEM],
            _FIND_LIBRARY: [_LIBRARY],
        }
        self.post_errors: dict[str, httpx.HTTPError] = {}
        self.posts: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        if path not in self.gets:
            raise AssertionError(f"unexpected GET {path!r}")
        return copy.deepcopy(self.gets[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        assert verb == "POST"
        self.posts.append((path, copy.deepcopy(json)))
        if path in self.post_errors:
            raise self.post_errors[path]
        if path == _MOUNT:
            self._add_mounted_drive()
        return copy.deepcopy(self.post_results[path])

    def _add_mounted_drive(self) -> None:
        self.gets[_CDROMS] = [*self.gets[_CDROMS], {"cdrom": "16002"}]
        self.gets[f"{_CDROMS}/16002"] = {
            "type": "SATA",
            "label": "CD/DVD drive 2",
            "sata": {"bus": 0, "unit": 1},
            "backing": {"type": "ISO_FILE", "iso_file": "[ds1] contentlib/boot.iso"},
            "state": "CONNECTED",
            "start_connected": True,
            "allow_guest_control": False,
        }


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _assert_schema(schema: dict[str, Any], out: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(out)


async def _mount(conn: _RestFake, **params: Any) -> Any:
    return await iso.vm_cdrom_mount_library_iso_composite(
        operator=_operator(),
        target=_Target(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


async def _unmount(conn: _RestFake, **params: Any) -> Any:
    return await iso.vm_cdrom_unmount_library_iso_composite(
        operator=_operator(),
        target=_Target(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


def _ctx(conn: _RestFake | None, **params: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", [iso.OP_ID_MOUNT_LIBRARY_ISO, iso.OP_ID_UNMOUNT_LIBRARY_ISO])
def test_both_changes_are_caution_and_wait_for_approval_on_both_catalogs(op_id: str) -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == op_id)
    assert spec.safety_level == "caution"
    assert spec.requires_approval is True
    assert spec.llm_instructions
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _PREVIEW_BUILDERS
    assert op_id in _GOVERNED_SUBOP_MANIFEST


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------


async def test_mount_by_id_sends_the_iso_image_mount_and_reads_the_drive_back(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake()
    out = await _mount(conn, vm=_VM, library_item=_ITEM)
    _assert_schema(iso.MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert conn.posts == [(_MOUNT, {"library_item": _ITEM, "vm": _VM})]
    assert out["status"] == "mounted"
    assert out["library_item"] == _ITEM
    assert out["cdrom"] == "16002"
    assert out["cdrom_info"]["backing_type"] == "ISO_FILE"
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/iso/image?action=mount"
    assert call["params"] == {"library_item": _ITEM, "vm": _VM}


async def test_mount_by_name_looks_up_the_item_first(gate: _GateRecorder) -> None:
    conn = _RestFake()
    out = await _mount(conn, vm=_VM, library_item_name="boot.iso")
    assert conn.posts[0] == (_FIND_ITEM, {"name": "boot.iso"})
    assert conn.posts[1] == (_MOUNT, {"library_item": _ITEM, "vm": _VM})
    assert out["status"] == "mounted"
    assert out["library_item"] == _ITEM
    # A held write saves the id it resolved, so its resume mounts the same item.
    (call,) = gate.calls
    assert call["resume_state"] == {"library_item": _ITEM}


async def test_mount_by_name_after_approval_mounts_the_item_the_approver_saw(
    gate: _GateRecorder,
) -> None:
    """The name points at another item by the time the approved call runs."""
    conn = _RestFake()
    preview = await _PREVIEW_BUILDERS[iso.OP_ID_MOUNT_LIBRARY_ISO](
        _ctx(conn, vm=_VM, library_item_name="boot.iso")
    )
    assert preview is not None and preview["library_item"] == _ITEM
    conn.post_results[_FIND_ITEM] = ["item-replaced"]
    conn.posts.clear()
    with composite_resume_binding(
        subop_op_id=iso.OP_ID_MOUNT_LIBRARY_ISO,
        subop_params_hash="h",
        resume_parent=None,
        proposed_effect={"op_class": "write", "preview": preview},
    ):
        out = await _mount(conn, vm=_VM, library_item_name="boot.iso")
    assert conn.posts == [(_MOUNT, {"library_item": _ITEM, "vm": _VM})]
    assert out["status"] == "mounted"
    assert out["library_item"] == _ITEM


@pytest.mark.parametrize(
    "proposed_effect",
    [
        {"op_class": "write", "preview": {"status": "ambiguous_item", "candidates": ["a", "b"]}},
        {"op_class": "write", "preview": {"summary": "x", "read_error": "ConnectError: boom"}},
        {"op_class": "write", "preview_unavailable": True},
        None,
    ],
)
async def test_mount_by_name_after_approval_without_a_pinned_item_sends_nothing(
    gate: _GateRecorder, proposed_effect: dict[str, Any] | None
) -> None:
    conn = _RestFake()
    with composite_resume_binding(
        subop_op_id=iso.OP_ID_MOUNT_LIBRARY_ISO,
        subop_params_hash="h",
        resume_parent=None,
        proposed_effect=proposed_effect,
    ):
        out = await _mount(conn, vm=_VM, library_item_name="boot.iso")
    _assert_schema(iso.MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert out["status"] == "item_not_pinned"
    assert "library_item" in out["guidance"]
    assert conn.posts == []
    assert gate.calls == []


async def test_mount_by_id_after_approval_ignores_the_preview(gate: _GateRecorder) -> None:
    conn = _RestFake()
    with composite_resume_binding(
        subop_op_id=iso.OP_ID_MOUNT_LIBRARY_ISO,
        subop_params_hash="h",
        resume_parent=None,
        proposed_effect={"op_class": "write", "preview": {"library_item": "other"}},
    ):
        out = await _mount(conn, vm=_VM, library_item=_ITEM)
    assert conn.posts == [(_MOUNT, {"library_item": _ITEM, "vm": _VM})]
    assert out["library_item"] == _ITEM


async def test_mount_resume_of_a_held_write_mounts_the_saved_item(gate: _GateRecorder) -> None:
    """A held mount saved the id; its resume does not look the name up again."""
    conn = _RestFake()
    conn.post_results[_FIND_ITEM] = ["item-replaced"]
    with composite_resume_binding(
        subop_op_id="POST:/vcenter/iso/image?action=mount",
        subop_params_hash="h",
        resume_parent={
            "op_id": iso.OP_ID_MOUNT_LIBRARY_ISO,
            "params": {"vm": _VM, "library_item_name": "boot.iso"},
            "state": {"library_item": _ITEM},
        },
        proposed_effect={"op_class": "write", "preview": {"params": {"library_item": _ITEM}}},
    ):
        out = await _mount(conn, vm=_VM, library_item_name="boot.iso")
    assert conn.posts == [(_MOUNT, {"library_item": _ITEM, "vm": _VM})]
    assert out["status"] == "mounted"


async def test_mount_by_name_in_one_library(gate: _GateRecorder) -> None:
    conn = _RestFake()
    await _mount(conn, vm=_VM, library_item_name="boot.iso", library_name="isos")
    assert conn.posts[:2] == [
        (_FIND_LIBRARY, {"name": "isos"}),
        (_FIND_ITEM, {"name": "boot.iso", "library_id": _LIBRARY}),
    ]


@pytest.mark.parametrize(
    ("path", "ids", "status"),
    [
        (_FIND_ITEM, [], "item_not_found"),
        (_FIND_ITEM, [_ITEM, "item-2"], "ambiguous_item"),
        (_FIND_LIBRARY, [], "library_not_found"),
        (_FIND_LIBRARY, [_LIBRARY, "library-2"], "ambiguous_library"),
    ],
)
async def test_mount_name_lookup_failure_sends_nothing(
    gate: _GateRecorder, path: str, ids: list[str], status: str
) -> None:
    conn = _RestFake()
    conn.post_results[path] = ids
    out = await _mount(conn, vm=_VM, library_item_name="boot.iso", library_name="isos")
    _assert_schema(iso.MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert out["status"] == status
    assert out["candidates"] == (ids if len(ids) > 1 else None)
    assert _MOUNT not in [p for p, _ in conn.posts]
    assert gate.calls == []


@pytest.mark.parametrize(
    ("http_status", "error_type", "status"),
    [(404, "NOT_FOUND", "not_found"), (400, "INVALID_ARGUMENT", "refused")],
)
async def test_mount_vendor_error_becomes_a_structured_answer(
    gate: _GateRecorder, http_status: int, error_type: str, status: str
) -> None:
    conn = _RestFake()
    conn.post_errors[_MOUNT] = _status_error(
        _MOUNT,
        http_status,
        {"error_type": error_type, "messages": [{"default_message": "no such item"}]},
    )
    out = await _mount(conn, vm=_VM, library_item=_ITEM)
    _assert_schema(iso.MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert out["status"] == status
    assert out["error_type"] == error_type
    assert "nothing changed" in out["guidance"]
    assert "no such item" in out["guidance"]
    assert out["cdrom"] is None


async def test_mount_server_error_raises(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_errors[_MOUNT] = _status_error(_MOUNT, 500, {})
    with pytest.raises(httpx.HTTPStatusError):
        await _mount(conn, vm=_VM, library_item=_ITEM)


async def test_mount_held_by_policy_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vcenter/iso/image?action=mount",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake()
    assert await _mount(conn, vm=_VM, library_item=_ITEM) is held
    assert conn.posts == []


async def test_mount_without_a_drive_id_still_says_mounted(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_results[_MOUNT] = {}
    out = await _mount(conn, vm=_VM, library_item=_ITEM)
    assert out["status"] == "mounted"
    assert out["cdrom"] is None
    assert "vmware.vm.hardware.show" in out["guidance"]


async def test_mount_refuses_a_vm_id_that_is_not_a_moid(gate: _GateRecorder) -> None:
    with pytest.raises(ValueError, match="not a vCenter VM id"):
        await _mount(_RestFake(), vm="web-01", library_item=_ITEM)


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42", "library_item": _ITEM}, True),
        ({"vm": "vm-42", "library_item_name": "boot.iso"}, True),
        ({"vm": "vm-42", "library_item_name": "boot.iso", "library_name": "isos"}, True),
        ({"vm": "vm-42", "library_item": _ITEM, "library_item_name": "boot.iso"}, False),
        ({"vm": "vm-42"}, False),
        ({"library_item": _ITEM}, False),
        ({"vm": "web-01", "library_item": _ITEM}, False),
        ({"vm": "vm-42", "library_item": "a/b"}, False),
    ],
)
def test_mount_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(iso.MOUNT_LIBRARY_ISO_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


# ---------------------------------------------------------------------------
# Unmount
# ---------------------------------------------------------------------------


async def test_unmount_sends_the_iso_image_unmount(gate: _GateRecorder) -> None:
    conn = _RestFake()
    out = await _unmount(conn, vm=_VM, cdrom="16002")
    _assert_schema(iso.UNMOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert conn.posts == [(_UNMOUNT, {"vm": _VM, "cdrom": "16002"})]
    assert out["status"] == "unmounted"
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/iso/image?action=unmount"
    assert call["params"] == {"vm": _VM, "cdrom": "16002"}


@pytest.mark.parametrize(
    ("http_status", "error_type", "status"),
    [(404, "NOT_FOUND", "not_found"), (400, "NOT_ALLOWED_IN_CURRENT_STATE", "refused")],
)
async def test_unmount_vendor_error_becomes_a_structured_answer(
    gate: _GateRecorder, http_status: int, error_type: str, status: str
) -> None:
    conn = _RestFake()
    conn.post_errors[_UNMOUNT] = _status_error(_UNMOUNT, http_status, {"error_type": error_type})
    out = await _unmount(conn, vm=_VM, cdrom="9999")
    _assert_schema(iso.UNMOUNT_LIBRARY_ISO_RESPONSE_SCHEMA, out)
    assert out["status"] == status
    assert out["error_type"] == error_type


async def test_unmount_held_by_policy_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vcenter/iso/image?action=unmount",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake()
    assert await _unmount(conn, vm=_VM, cdrom="16002") is held
    assert conn.posts == []


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42", "cdrom": "16002"}, True),
        ({"vm": "vm-42"}, False),
        ({"cdrom": "16002"}, False),
        ({"vm": "vm-42", "cdrom": "../x"}, False),
    ],
)
def test_unmount_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(iso.UNMOUNT_LIBRARY_ISO_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


async def test_mount_preview_shows_the_item_the_vm_and_its_drives() -> None:
    conn = _RestFake()
    ctx = _ctx(conn, vm=_VM, library_item_name="boot.iso")
    preview = await _PREVIEW_BUILDERS[iso.OP_ID_MOUNT_LIBRARY_ISO](ctx)
    assert preview is not None
    assert preview["vm"] == _VM
    assert preview["library_item"] == _ITEM
    assert [row["cdrom"] for row in preview["current_cdroms"]] == ["3000"]
    assert "new CD drive" in preview["summary"]
    assert _MOUNT not in [p for p, _ in conn.posts]


async def test_mount_preview_says_an_ambiguous_name_sends_nothing() -> None:
    conn = _RestFake()
    conn.post_results[_FIND_ITEM] = [_ITEM, "item-2"]
    preview = await _PREVIEW_BUILDERS[iso.OP_ID_MOUNT_LIBRARY_ISO](
        _ctx(conn, vm=_VM, library_item_name="boot.iso")
    )
    assert preview is not None
    assert preview["status"] == "ambiguous_item"
    assert preview["candidates"] == [_ITEM, "item-2"]


async def test_mount_preview_reports_a_read_error() -> None:
    preview = await _PREVIEW_BUILDERS[iso.OP_ID_MOUNT_LIBRARY_ISO](
        _ctx(_RestFake(), vm="web-01", library_item=_ITEM)
    )
    assert preview is not None
    assert "read_error" in preview


@pytest.mark.parametrize(("cdrom", "found"), [("3000", True), ("9999", False)])
async def test_unmount_preview_shows_the_drive(cdrom: str, found: bool) -> None:
    preview = await _PREVIEW_BUILDERS[iso.OP_ID_UNMOUNT_LIBRARY_ISO](
        _ctx(_RestFake(), vm=_VM, cdrom=cdrom)
    )
    assert preview is not None
    assert preview["cdrom"] == cdrom
    assert (preview["cdrom_info"] is not None) is found
    assert ("not found" in preview["summary"]) is not found


async def test_previews_without_a_connector_decline() -> None:
    for op_id in (iso.OP_ID_MOUNT_LIBRARY_ISO, iso.OP_ID_UNMOUNT_LIBRARY_ISO):
        assert await _PREVIEW_BUILDERS[op_id](_ctx(None, vm=_VM, cdrom="1")) is None
