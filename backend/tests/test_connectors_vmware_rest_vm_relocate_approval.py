# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM moves that copy disks wait for approval and show the copy size (#4749).

The VMware floor (``ingest_safety.py``) makes the generic relocate (both
forms) and the older-API ``RelocateVM_Task`` ``dangerous`` with approval on
the 8.0 and 9.0 catalogs. ``vm_relocate_preview.py`` registers a preview
builder under every op_id spelling; with a datastore in the call it shows the
data copied now, the most the disks can grow to, the source and target
datastores, and the free space of the target before and after the copy.

The pinned-spec lanes check that each floor rule matches an operation in the
vendor files on the spec shelf; they skip when the shelf is not configured
(see ``tests/_spec_shelf.py``) and run in CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest import vm_relocate_preview as previews
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.ingest_safety import (
    is_vmware_vm_relocate,
    vmware_rest_safety_floor,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec
from tests._vmware_teardown_fake import VimFake, moref, preview_ctx

_REST_RELOCATE = "POST:/vcenter/vm/{vm}?action=relocate"
_REST_RELOCATE_TASK = "POST:/vcenter/vm/{vm}?action=relocate&vmw-task=true"
_VIM_RELOCATE = "POST:/VirtualMachine/{moId}/RelocateVM_Task"
_RELOCATE_OP_IDS = frozenset({_REST_RELOCATE, _REST_RELOCATE_TASK, _VIM_RELOCATE})
_UNRELATED_POSTS = frozenset(
    {"POST:/PropertyCollector/{moId}/RetrievePropertiesEx", "POST:/cis/tasks?action=list"}
)

_GIB = 1024**3
_VM = "vm-101"
_SOURCE = "datastore-11"
_TARGET = "datastore-22"
_OTHER_TARGET = "datastore-33"


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _floored(
    protos: list[EndpointDescriptorProto], version: str
) -> dict[str, EndpointDescriptorProto]:
    return {
        p.op_id: apply_safety_floor(
            product="vmware", version=version, impl_id="vmware-rest", proto=p
        )
        for p in protos
    }


def _waits_for_approval(proto: EndpointDescriptorProto) -> bool:
    return proto.safety_level == "dangerous" and proto.requires_approval is True


# ---------------------------------------------------------------------------
# The floor rule
# ---------------------------------------------------------------------------

_HAND_MADE_SPEC = """
openapi: 3.0.3
info: {title: vm relocate fixture, version: '1.0'}
paths:
  /vcenter/vm/{vm}?action=relocate:
    post:
      responses: {'204': {description: ok}}
  /vcenter/vm/{vm}?action=relocate&vmw-task=true:
    post:
      responses: {'202': {description: ok}}
  /VirtualMachine/{moId}/RelocateVM_Task:
    post:
      responses: {'200': {description: ok}}
  /PropertyCollector/{moId}/RetrievePropertiesEx:
    post:
      responses: {'200': {description: ok}}
  /cis/tasks?action=list:
    post:
      responses: {'200': {description: ok}}
"""


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_hand_made_spec_relocate_ops_wait_for_approval(version: str) -> None:
    """Both generic relocate forms and ``RelocateVM_Task`` land ``dangerous`` with approval."""
    rows = parse_openapi("file:///vm-relocate.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, version)

    assert set(ops) == _RELOCATE_OP_IDS | _UNRELATED_POSTS
    for op_id in _RELOCATE_OP_IDS:
        assert _waits_for_approval(ops[op_id]), op_id


@pytest.mark.parametrize("op_id", sorted(_UNRELATED_POSTS))
def test_an_unrelated_post_keeps_its_level(op_id: str) -> None:
    """A read sent as ``POST`` stays ``caution`` without approval."""
    rows = parse_openapi("file:///vm-relocate.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, "9.0")

    assert ops[op_id].safety_level == "caution"
    assert ops[op_id].requires_approval is False


#: ``(method, path, the rule accepts it)``. Every accepted spelling must also
#: carry the preview builder (checked below), or the call would wait for
#: approval without the copy size.
_SAFETY_RULE_CASES: list[tuple[str, str, bool]] = [
    ("POST", "/vcenter/vm/{vm}?action=relocate", True),
    ("POST", "/vcenter/vm/{vm}?action=relocate&vmw-task=true", True),
    ("POST", "/api/vcenter/vm/{vm}?action=relocate", True),
    ("POST", "/api/vcenter/vm/{vm}?action=relocate&vmw-task=true", True),
    ("POST", "/vcenter/vm/{vm_id}?action=relocate", True),
    ("POST", "/vcenter/vm/{vm_id}?action=relocate&vmw-task=true", True),
    ("POST", "/api/vcenter/vm/{vm_id}?action=relocate", True),
    ("POST", "/api/vcenter/vm/{vm_id}?action=relocate&vmw-task=true", True),
    ("POST", "/VirtualMachine/{moId}/RelocateVM_Task", True),
    ("POST", "/api/VirtualMachine/{moId}/RelocateVM_Task", True),
    ("POST", "/VirtualMachine/{mo_id}/RelocateVM_Task", True),
    ("post", "/api/VirtualMachine/{mo_id}/RelocateVM_Task", True),
    ("GET", "/vcenter/vm/{vm}?action=relocate", False),
    ("POST", "/vcenter/vm/{vm}?action=unregister", False),
    ("POST", "/vcenter/vm/{vm}?action=relocate&other=1", False),
    ("POST", "/vcenter/vm?action=relocate", False),
    ("POST", "/VirtualMachine/{moId}/CloneVM_Task", False),
    ("POST", "/VirtualMachine/{moId}/RelocateVM_Task?x=1", False),
    ("POST", "/VirtualApp/{moId}/RelocateVM_Task", False),
]


@pytest.mark.parametrize(("method", "path", "expected"), _SAFETY_RULE_CASES)
def test_is_vmware_vm_relocate(method: str, path: str, expected: bool) -> None:
    assert is_vmware_vm_relocate(method, path) is expected


def test_floor_never_lowers_a_destructive_relocate() -> None:
    proto = EndpointDescriptorProto(
        op_id=_REST_RELOCATE,
        method="POST",
        path="/vcenter/vm/{vm}?action=relocate",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


def test_typed_vm_migrate_is_dangerous_with_approval() -> None:
    """The typed move keeps its level; the generic relocate matches it."""
    spec = next(s for s in _COMPOSITES if s.op_id == "vmware.composite.vm.migrate")

    assert spec.safety_level == "dangerous"
    assert spec.requires_approval is True


# ---------------------------------------------------------------------------
# The pinned vendor files serve every rule (shelf-gated, runs in CI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product_dir", "filename", "version", "expected"),
    [
        ("vcenter-8.0", "vcenter.yaml", "8.0", {_REST_RELOCATE, _REST_RELOCATE_TASK}),
        ("vcenter-9.0", "vcenter.yaml", "9.0", {_REST_RELOCATE, _REST_RELOCATE_TASK}),
        ("vcenter-9.0", "vi-json.yaml", "9.0", {_VIM_RELOCATE}),
    ],
)
def test_pinned_vendor_spec_serves_the_relocate_rules(
    product_dir: str, filename: str, version: str, expected: set[str]
) -> None:
    """Each floor rule matches an operation in the pinned file, and lands gated."""
    spec_path = require_shelf_spec(product_dir, filename)
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served(expected, set(ops), spec_label=f"{product_dir}/{filename}")
    assert all(_waits_for_approval(ops[op_id]) for op_id in expected)
    assert all(op_id in previews.VM_RELOCATE_OP_IDS for op_id in expected)


# ---------------------------------------------------------------------------
# The copy-size preview
# ---------------------------------------------------------------------------


class _Connector:
    """Connector double: only the VI-JSON seam, answered by a ``VimFake``."""

    def __init__(self) -> None:
        self.vim = VimFake()

    async def _post_vmomi_json(self, *args: Any, **kwargs: Any) -> Any:
        return await self.vim._post_vmomi_json(*args, **kwargs)


def _vcenter(*, with_vm: bool = True, with_target: bool = True) -> _Connector:
    conn = _Connector()
    if with_vm:
        conn.vim.add(
            "VirtualMachine",
            _VM,
            name="nested-esxi-01",
            **{
                "runtime.powerState": "poweredOff",
                "datastore": [moref("Datastore", _SOURCE)],
                "summary.storage": {
                    "_typeName": "VirtualMachineStorageSummary",
                    "committed": 365 * _GIB,
                    "uncommitted": 738 * _GIB,
                    "unshared": 365 * _GIB,
                },
            },
        )
    conn.vim.add("Datastore", _SOURCE, summary={"name": "local-a", "capacity": 4000 * _GIB})
    if with_target:
        conn.vim.add(
            "Datastore",
            _TARGET,
            summary={"name": "local-b", "capacity": 4000 * _GIB, "freeSpace": 2000 * _GIB},
        )
    conn.vim.add(
        "Datastore",
        _OTHER_TARGET,
        summary={"name": "local-c", "capacity": 1000 * _GIB, "freeSpace": 500 * _GIB},
    )
    return conn


def _rest_params(body: dict[str, Any]) -> dict[str, Any]:
    return {"vm": _VM, "body": body}


def _vim_params(spec: dict[str, Any]) -> dict[str, Any]:
    return {"moId": _VM, "body": {"spec": spec, "priority": "defaultPriority"}}


@pytest.mark.parametrize("op_id", sorted(previews.VM_RELOCATE_OP_IDS))
def test_every_relocate_spelling_has_a_preview_builder(op_id: str) -> None:
    assert _PREVIEW_BUILDERS[op_id] is previews.vm_relocate_preview
    assert is_vmware_vm_relocate(*op_id.split(":", 1))


@pytest.mark.parametrize(
    ("method", "path"),
    [(method, path) for method, path, accepted in _SAFETY_RULE_CASES if accepted],
)
def test_every_spelling_the_rule_accepts_has_a_preview_builder(method: str, path: str) -> None:
    """A call the floor parks must show the copy size: no spelling without a builder."""
    op_id = f"{method.upper()}:{path}"

    assert op_id in previews.VM_RELOCATE_OP_IDS
    assert _PREVIEW_BUILDERS[op_id] is previews.vm_relocate_preview


@pytest.mark.parametrize("name", ["vm", "vm_id", "moId", "mo_id"])
async def test_preview_reads_the_vm_id_under_every_parameter_spelling(name: str) -> None:
    conn = _vcenter()
    params = {name: _VM, "body": {"placement": {"datastore": _TARGET}}}

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["vm"] == _VM
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert conn.vim.methods() == ["RetrievePropertiesEx", "RetrievePropertiesEx"]


@pytest.mark.parametrize(
    ("body", "home", "per_disk"),
    [
        ({"placement": {"datastore": _TARGET}}, _TARGET, []),
        (
            {"disks": {"2000": {"datastore": _TARGET}, "2001": {"datastore": _TARGET}}},
            None,
            [_TARGET],
        ),
        ({"placement": {"host": "host-1"}, "disks": {}}, None, []),
        ({"spec": {"datastore": moref("Datastore", _TARGET)}}, _TARGET, []),
        (
            {"spec": {"disk": [{"diskId": 2000, "datastore": moref("Datastore", _OTHER_TARGET)}]}},
            None,
            [_OTHER_TARGET],
        ),
        ({"spec": {"pool": moref("ResourcePool", "resgroup-1")}}, None, []),
        ({}, None, []),
    ],
)
def test_relocate_target_datastores_reads_both_body_shapes(
    body: dict[str, Any], home: str | None, per_disk: list[str]
) -> None:
    assert previews.relocate_target_datastores(body) == (home, per_disk)


@pytest.mark.parametrize(
    "params",
    [
        _rest_params({"placement": {"datastore": _TARGET, "host": "host-2"}}),
        _vim_params({"datastore": moref("Datastore", _TARGET)}),
    ],
    ids=["rest", "older-api"],
)
async def test_whole_vm_move_shows_size_datastores_and_free_space(
    params: dict[str, Any],
) -> None:
    conn = _vcenter()

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copies_disks"] is True
    assert preview["copy_scope"] == "whole_vm"
    assert preview["copy_size_is_upper_bound"] is False
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert preview["copy_max_bytes"] == 1103 * _GIB
    assert preview["power_state"] == "poweredOff"
    assert preview["name"] == "nested-esxi-01"
    assert preview["source_datastores"] == [{"id": _SOURCE, "name": "local-a"}]
    assert preview["target_datastores"] == [
        {
            "id": _TARGET,
            "name": "local-b",
            "capacity_bytes": 4000 * _GIB,
            "free_space_bytes": 2000 * _GIB,
            "free_space_after_copy_bytes": 1635 * _GIB,
            "free_space_after_max_copy_bytes": 897 * _GIB,
        }
    ]
    assert "size_unavailable_reason" not in preview
    assert preview["summary"] == (
        "Copies 365.0 GiB now, up to 1103.0 GiB if the disks fill up, from local-a "
        f"({_SOURCE}) to local-b ({_TARGET}). local-b ({_TARGET}) has 2000.0 GiB free "
        "before the copy and 1635.0 GiB after it. The VM is stopped."
    )
    assert conn.vim.methods() == ["RetrievePropertiesEx", "RetrievePropertiesEx"]


async def test_some_disks_move_says_the_copy_is_at_most_this_size() -> None:
    conn = _vcenter()
    params = _rest_params({"disks": {"2000": {"datastore": _TARGET}}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copy_scope"] == "some_disks"
    assert preview["copy_size_is_upper_bound"] is True
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert preview["summary"].startswith("Copies at most 365.0 GiB now")
    assert "Only some disks move, so the real copy can be smaller." in preview["summary"]


async def test_disks_to_two_datastores_lists_both_targets() -> None:
    conn = _vcenter()
    params = _vim_params(
        {
            "datastore": moref("Datastore", _TARGET),
            "disk": [{"diskId": 2001, "datastore": moref("Datastore", _OTHER_TARGET)}],
        }
    )

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copy_scope"] == "whole_vm"
    assert preview["copy_size_is_upper_bound"] is True
    assert [t["id"] for t in preview["target_datastores"]] == [_TARGET, _OTHER_TARGET]
    assert preview["target_datastores"][1]["free_space_after_copy_bytes"] == 135 * _GIB
    assert "more than one datastore" in preview["summary"]
    assert conn.vim.methods() == ["RetrievePropertiesEx", "RetrievePropertiesEx"]


@pytest.mark.parametrize(
    "params",
    [
        _rest_params({"placement": {"host": "host-2", "cluster": "domain-c1"}}),
        _vim_params({"host": moref("HostSystem", "host-2")}),
        {"vm": _VM},
    ],
    ids=["rest", "older-api", "no-body"],
)
async def test_a_move_without_a_datastore_copies_no_disks(params: dict[str, Any]) -> None:
    conn = _vcenter()

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview == {
        "summary": "No disks are copied: the call names no datastore.",
        "vm": _VM,
        "copies_disks": False,
        "copy_scope": "none",
        "target_datastores": [],
    }
    assert conn.vim.calls == []


async def test_a_failed_vm_read_keeps_the_target_and_says_why_the_size_is_missing() -> None:
    conn = _vcenter(with_vm=False)
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copies_disks"] is True
    assert preview["copied_now_bytes"] is None
    assert preview["copy_max_bytes"] is None
    assert preview["source_datastores"] == []
    assert preview["target_datastores"][0]["free_space_bytes"] == 2000 * _GIB
    assert preview["target_datastores"][0]["free_space_after_copy_bytes"] is None
    assert preview["size_unavailable_reason"].startswith("the VM read failed (")
    assert _VM in preview["size_unavailable_reason"]
    assert preview["summary"].startswith("The copy size is missing: the VM read failed (")
    assert f"local-b ({_TARGET}) has 2000.0 GiB free before the copy." in preview["summary"]


async def test_a_failed_datastore_read_keeps_the_size_and_says_why() -> None:
    conn = _vcenter(with_target=False)
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert preview["source_datastores"] == [{"id": _SOURCE, "name": None}]
    assert preview["target_datastores"] == [
        {
            "id": _TARGET,
            "name": None,
            "capacity_bytes": None,
            "free_space_bytes": None,
            "free_space_after_copy_bytes": None,
            "free_space_after_max_copy_bytes": None,
        }
    ]
    assert preview["size_unavailable_reason"].startswith("the datastore read failed (")
    assert f"The free space of {_TARGET} is unknown." in preview["summary"]


async def test_an_unreadable_vm_storage_summary_keeps_the_rest_and_names_the_fault() -> None:
    """vCenter answered, but could not read ``summary.storage`` (a ``missingSet`` entry)."""
    conn = _vcenter()
    conn.vim.unreadable[("VirtualMachine", _VM)] = {"summary.storage"}
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copies_disks"] is True
    assert preview["name"] == "nested-esxi-01"
    assert preview["power_state"] == "poweredOff"
    assert preview["source_datastores"] == [{"id": _SOURCE, "name": "local-a"}]
    assert preview["copied_now_bytes"] is None
    assert preview["copy_max_bytes"] is None
    assert preview["target_datastores"][0]["free_space_bytes"] == 2000 * _GIB
    assert preview["target_datastores"][0]["free_space_after_copy_bytes"] is None
    assert preview["size_unavailable_reason"] == (
        "vCenter could not read summary.storage (NoPermission) of the VM"
    )
    assert preview["summary"] == (
        "The copy size is missing: vCenter could not read summary.storage (NoPermission) "
        f"of the VM. local-b ({_TARGET}) has 2000.0 GiB free before the copy. "
        "The VM is stopped."
    )


async def test_an_unreadable_datastore_summary_keeps_the_size_and_names_the_fault() -> None:
    """vCenter answered, but could not read the target's ``summary``."""
    conn = _vcenter()
    conn.vim.unreadable[("Datastore", _TARGET)] = {"summary"}
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert preview["source_datastores"] == [{"id": _SOURCE, "name": "local-a"}]
    assert preview["target_datastores"] == [
        {
            "id": _TARGET,
            "name": None,
            "capacity_bytes": None,
            "free_space_bytes": None,
            "free_space_after_copy_bytes": None,
            "free_space_after_max_copy_bytes": None,
        }
    ]
    assert preview["size_unavailable_reason"] == (
        f"vCenter could not read summary (NoPermission) of datastore {_TARGET}"
    )
    assert preview["summary"].startswith("Copies 365.0 GiB now")
    assert f"The free space of {_TARGET} is unknown." in preview["summary"]


async def test_unreadable_vm_and_datastore_properties_are_both_reported() -> None:
    conn = _vcenter()
    conn.vim.unreadable[("VirtualMachine", _VM)] = {"summary.storage"}
    conn.vim.unreadable[("Datastore", _TARGET)] = {"summary"}
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["size_unavailable_reason"] == (
        "vCenter could not read summary.storage (NoPermission) of the VM; "
        f"vCenter could not read summary (NoPermission) of datastore {_TARGET}"
    )


async def test_a_vm_without_a_storage_summary_says_the_size_was_not_reported() -> None:
    """An unset property has no ``missingSet`` entry; the preview still says why."""
    conn = _vcenter()
    del conn.vim.objects[("VirtualMachine", _VM)]["summary.storage"]
    params = _rest_params({"placement": {"datastore": _TARGET}})

    preview = await previews.vm_relocate_preview(preview_ctx(params, conn))

    assert preview is not None
    assert preview["copied_now_bytes"] is None
    assert preview["size_unavailable_reason"] == "vCenter did not report the storage size of the VM"
    assert preview["summary"].startswith(
        "The copy size is missing: vCenter did not report the storage size of the VM."
    )


async def test_preview_declines_without_a_connector_or_a_vm_id() -> None:
    params = _rest_params({"placement": {"datastore": _TARGET}})

    assert await previews.vm_relocate_preview(preview_ctx(params, None)) is None
    assert await previews.vm_relocate_preview(preview_ctx({"body": {}}, _vcenter())) is None


async def test_read_helpers_return_the_shared_numbers() -> None:
    """The typed datastore move reuses these reads; pin their shape."""
    conn = _vcenter()
    ctx = preview_ctx({}, conn)

    size = await previews.read_vm_copy_size(conn, ctx.target, ctx.operator, _VM)  # type: ignore[arg-type]
    spaces = await previews.read_datastore_space(
        conn,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        [_TARGET, _SOURCE],
    )

    assert size == previews.VmCopySize(
        vm=_VM,
        name="nested-esxi-01",
        power_state="poweredOff",
        source_datastores=(_SOURCE,),
        copied_now_bytes=365 * _GIB,
        copy_max_bytes=1103 * _GIB,
        unreadable_properties=(),
    )
    assert spaces == {
        _TARGET: previews.DatastoreSpace(
            id=_TARGET,
            name="local-b",
            capacity_bytes=4000 * _GIB,
            free_space_bytes=2000 * _GIB,
            unreadable_properties=(),
        ),
        _SOURCE: previews.DatastoreSpace(
            id=_SOURCE,
            name="local-a",
            capacity_bytes=4000 * _GIB,
            free_space_bytes=None,
            unreadable_properties=(),
        ),
    }


async def test_read_helpers_name_the_properties_vcenter_could_not_read() -> None:
    """The shared reads keep a successful answer and name the unreadable properties."""
    conn = _vcenter()
    conn.vim.unreadable[("VirtualMachine", _VM)] = {"summary.storage", "datastore"}
    conn.vim.unreadable[("Datastore", _TARGET)] = {"summary"}
    ctx = preview_ctx({}, conn)

    size = await previews.read_vm_copy_size(conn, ctx.target, ctx.operator, _VM)  # type: ignore[arg-type]
    spaces = await previews.read_datastore_space(
        conn,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        [_TARGET, _SOURCE],
    )

    assert size.name == "nested-esxi-01"
    assert size.source_datastores == ()
    assert size.copied_now_bytes is None
    assert size.unreadable_properties == (
        "datastore (NoPermission)",
        "summary.storage (NoPermission)",
    )
    assert spaces[_TARGET].free_space_bytes is None
    assert spaces[_TARGET].unreadable_properties == ("summary (NoPermission)",)
    assert spaces[_SOURCE].name == "local-a"
    assert spaces[_SOURCE].unreadable_properties == ()
