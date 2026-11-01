# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the wider ``vmware.snapshot.list``: every VM, age filter and size on disk.

* :func:`snapshot_size` on a hand-written ``layoutEx`` (counted like govc);
* the list of every VM through a container view (``tests._vmware_view_fake``):
  the rows of each VM, the ``layoutEx`` read for the VMs with snapshots only,
  the view destroyed, an unreadable VM in ``read_note``, a failed size read;
* ``older_than_days``: only the older snapshots, for every VM and for one VM.

All names and ids are made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest import typed_ops_snapshot_list
from meho_backplane.connectors.vmware_rest.snapshot_tree import snapshot_size
from meho_backplane.connectors.vmware_rest.typed_ops_snapshot_list import (
    VMWARE_SNAPSHOT_LIST_OP,
    snapshot_list_impl,
)
from tests._vmware_view_fake import ROOT, ViewFake, moref

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _operator() -> Operator:
    return Operator(
        sub="op-snapshot-list-all",
        name="Snapshot List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _node(snapshot_id: str, name: str, created: str, children: list[Any] | None = None) -> Any:
    node: dict[str, Any] = {
        "_typeName": "VirtualMachineSnapshotTree",
        "snapshot": moref("VirtualMachineSnapshot", snapshot_id),
        "name": name,
        "description": "",
        "createTime": created,
        "state": "poweredOff",
        "quiesced": False,
    }
    if children:
        node["childSnapshotList"] = children
    return node


def _info(roots: list[Any], current: str) -> dict[str, Any]:
    return {
        "_typeName": "VirtualMachineSnapshotInfo",
        "rootSnapshotList": roots,
        "currentSnapshot": moref("VirtualMachineSnapshot", current),
    }


def _files(*specs: tuple[int, str, int]) -> list[dict[str, Any]]:
    return [{"key": k, "type": t, "size": size, "name": f"file-{k}"} for k, t, size in specs]


def _chain(*links: list[int]) -> list[dict[str, Any]]:
    return [{"key": 2000, "chain": [{"fileKey": keys} for keys in links]}]


#: Files: base disk (1, 2), snapshot-1 .vmsn (3), delta 1 (4, 5), snapshot-2 .vmsn (6),
#: running delta (7, 8).
_LAYOUT_EX = {
    "_typeName": "VirtualMachineFileLayoutEx",
    "file": _files(
        (0, "config", 3),
        (1, "diskDescriptor", 500),
        (2, "diskExtent", 10_000),
        (3, "snapshotData", 100),
        (4, "diskDescriptor", 1),
        (5, "diskExtent", 2_000),
        (6, "snapshotData", 200),
        (7, "diskDescriptor", 1),
        (8, "diskExtent", 3_000),
    ),
    "disk": _chain([1, 2], [4, 5], [7, 8]),
    "snapshot": [
        {
            "key": moref("VirtualMachineSnapshot", "snapshot-1"),
            "dataKey": 3,
            "disk": _chain([1, 2]),
        },
        {
            "key": moref("VirtualMachineSnapshot", "snapshot-2"),
            "dataKey": 6,
            "disk": _chain([1, 2], [4, 5]),
        },
    ],
}

_OLD = "2026-10-01T08:00:00.000Z"
_NEW = "2026-10-18T08:00:00.000Z"


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typed_ops_snapshot_list, "_now", lambda: _NOW)


def _fake() -> ViewFake:
    fake = ViewFake()
    fake.add("Folder", "group-v3", parent=("Folder", ROOT), name="vm")
    fake.add(
        "VirtualMachine",
        "vm-1",
        parent=("Folder", "group-v3"),
        name="app-vm",
        snapshot=_info(
            [_node("snapshot-1", "base", _OLD, [_node("snapshot-2", "patch", _NEW)])],
            "snapshot-2",
        ),
        layoutEx=_LAYOUT_EX,
    )
    fake.add(
        "VirtualMachine",
        "vm-2",
        parent=("Folder", "group-v3"),
        name="db-vm",
        snapshot=_info([_node("snapshot-7", "before-upgrade", _OLD)], "snapshot-7"),
    )
    fake.add("VirtualMachine", "vm-3", parent=("Folder", "group-v3"), name="no-snaps")
    return fake


async def _list(fake: ViewFake, **params: Any) -> dict[str, Any]:
    return await snapshot_list_impl(fake, _operator(), _Target(), params)


# ---------------------------------------------------------------------------
# snapshot_size
# ---------------------------------------------------------------------------


def test_size_of_a_root_snapshot_is_its_vmsn_without_the_base_disk() -> None:
    assert snapshot_size(_LAYOUT_EX, "snapshot-1", None, current=False) == 100


def test_size_of_the_current_child_has_its_delta_and_the_running_delta() -> None:
    # .vmsn 200 + delta 1 (1 + 2000) + running delta (1 + 3000).
    assert snapshot_size(_LAYOUT_EX, "snapshot-2", "snapshot-1", current=True) == 5202


@pytest.mark.parametrize("layout", [None, {}, {"file": []}, "junk"])
def test_size_is_unknown_without_a_layout(layout: Any) -> None:
    assert snapshot_size(layout, "snapshot-1", None, current=False) is None


def test_size_is_unknown_for_a_snapshot_not_in_the_layout() -> None:
    assert snapshot_size(_LAYOUT_EX, "snapshot-9", None, current=False) is None


# ---------------------------------------------------------------------------
# Every VM
# ---------------------------------------------------------------------------


async def test_every_vm_lists_the_snapshots_of_all_vms() -> None:
    fake = _fake()
    out = await _list(fake)

    assert out["vm"] is None
    assert out["current_snapshot_id"] is None
    assert out["read_note"] is None
    assert [(r["vm"], r["vm_name"], r["snapshot_id"]) for r in out["snapshots"]] == [
        ("vm-1", "app-vm", "snapshot-1"),
        ("vm-1", "app-vm", "snapshot-2"),
        ("vm-2", "db-vm", "snapshot-7"),
    ]
    assert out["snapshot_count"] == 3
    by_id = {r["snapshot_id"]: r for r in out["snapshots"]}
    assert by_id["snapshot-1"]["age_days"] == 19
    assert by_id["snapshot-2"]["age_days"] == 2
    assert by_id["snapshot-2"]["size_bytes"] == 5202
    assert by_id["snapshot-2"]["memory"] is False
    # vm-2 has no layoutEx in the fake: its size is unknown, not zero.
    assert by_id["snapshot-7"]["size_bytes"] is None
    Draft202012Validator(VMWARE_SNAPSHOT_LIST_OP.response_schema).validate(out)

    # One view of all VMs below the root folder, read, then destroyed.
    [view] = fake.views.values()
    assert view["container"]["value"] == ROOT
    assert view["type"] == ["VirtualMachine"]
    assert fake.destroyed == list(fake.views)
    # layoutEx is read for the VMs with snapshots only.
    [layout_read] = [
        body
        for path, body in fake.calls
        if path.endswith("RetrievePropertiesEx")
        and body["specSet"][0]["objectSet"][0]["obj"]["type"] == "VirtualMachine"
    ]
    assert layout_read["specSet"][0]["propSet"][0]["pathSet"] == ["layoutEx"]
    assert sorted(o["obj"]["value"] for o in layout_read["specSet"][0]["objectSet"]) == [
        "vm-1",
        "vm-2",
    ]


async def test_older_than_7_days_returns_only_those_snapshots() -> None:
    out = await _list(_fake(), older_than_days=7)
    assert [r["snapshot_id"] for r in out["snapshots"]] == ["snapshot-1", "snapshot-7"]
    assert all(r["age_days"] > 7 for r in out["snapshots"])
    assert out["snapshot_count"] == 2


async def test_every_vm_reads_all_pages() -> None:
    fake = _fake()
    fake.page_size = 1
    out = await _list(fake)
    assert out["snapshot_count"] == 3
    assert fake.continue_calls


async def test_a_vm_whose_snapshots_cannot_be_read_is_named_in_the_note() -> None:
    fake = _fake()
    fake.unreadable[("VirtualMachine", "vm-2")] = {"snapshot"}
    out = await _list(fake)
    assert [r["vm"] for r in out["snapshots"]] == ["vm-1", "vm-1"]
    assert out["read_note"] is not None
    assert "1 VM(s) could not be read" in out["read_note"]


async def test_a_failed_size_read_keeps_the_rows_without_sizes() -> None:
    fake = _fake()
    fake.fail_reads["VirtualMachine"] = RuntimeError("read failed")
    out = await _list(fake)
    assert out["snapshot_count"] == 3
    assert all(r["size_bytes"] is None for r in out["snapshots"])
    assert out["read_note"] == "snapshot sizes could not be read (RuntimeError)"


# ---------------------------------------------------------------------------
# One VM with the age filter
# ---------------------------------------------------------------------------


class _OneVm:
    """Answers the one-VM read with vm-1 of :func:`_fake`."""

    def __init__(self) -> None:
        self.props = _fake().objects[("VirtualMachine", "vm-1")]

    async def _post_vmomi_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        del target, path
        names = kwargs["json"]["specSet"][0]["propSet"][0]["pathSet"]
        return {
            "objects": [
                {
                    "obj": {"type": "VirtualMachine", "value": "vm-1"},
                    "propSet": [
                        {"name": n, "val": self.props[n]} for n in names if n in self.props
                    ],
                }
            ]
        }


async def test_one_vm_with_the_age_filter_and_sizes() -> None:
    out = await snapshot_list_impl(
        _OneVm(),  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-1", "older_than_days": 7},
    )
    assert out["vm"] == "vm-1"
    assert out["current_snapshot_id"] == "snapshot-2"
    assert [r["snapshot_id"] for r in out["snapshots"]] == ["snapshot-1"]
    assert out["snapshots"][0]["size_bytes"] == 100
    Draft202012Validator(VMWARE_SNAPSHOT_LIST_OP.response_schema).validate(out)
