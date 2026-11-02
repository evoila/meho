# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.snapshot.list`` typed read and the shared snapshot-tree rows.

* :func:`snapshot_rows` and :func:`descendants` on hand-written snapshot trees:
  tree order, parents, the current flag, a VM without snapshots, and the
  single-node shape the SOAP path of a single ESXi host produces;
* :func:`snapshot_list_impl` against a recording fake connector: the request,
  the answer, an unknown VM (``not_found``) and an unreadable snapshot property
  (an error, never "no snapshots");
* the real connector over mocked HTTP: the read lands on the VI-JSON path of
  the target's release, on vCenter 9.0 and 8.0;
* the operation metadata and its registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.snapshot_tree import descendants, snapshot_rows
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_snapshot_list import (
    VMWARE_SNAPSHOT_LIST_OP,
    snapshot_list_impl,
)
from meho_backplane.operations._validate import validate_params

_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _operator() -> Operator:
    return Operator(
        sub="op-snapshot-list",
        name="Snapshot List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _node(
    snapshot_id: str,
    name: str,
    children: list[dict[str, Any]] | None = None,
    *,
    state: str = "poweredOff",
    quiesced: bool = False,
) -> dict[str, Any]:
    """A ``VirtualMachineSnapshotTree`` node as VI-JSON returns it."""
    node: dict[str, Any] = {
        "_typeName": "VirtualMachineSnapshotTree",
        "snapshot": {
            "_typeName": "ManagedObjectReference",
            "type": "VirtualMachineSnapshot",
            "value": snapshot_id,
        },
        "vm": {"_typeName": "ManagedObjectReference", "type": "VirtualMachine", "value": "vm-42"},
        "name": name,
        "description": f"about {name}",
        "id": int(snapshot_id.rsplit("-", 1)[-1]),
        "createTime": "2026-10-01T08:00:00.000Z",
        "state": state,
        "quiesced": quiesced,
    }
    if children is not None:
        node["childSnapshotList"] = children
    return node


def _info(roots: Any, current: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {"_typeName": "VirtualMachineSnapshotInfo", "rootSnapshotList": roots}
    if current is not None:
        info["currentSnapshot"] = {"type": "VirtualMachineSnapshot", "value": current}
    return info


#: root -> (child-a -> grandchild), child-b; the VM runs on grandchild.
_TREE = _info(
    [
        _node(
            "snapshot-1",
            "base",
            [
                _node("snapshot-2", "child-a", [_node("snapshot-4", "grandchild")]),
                _node("snapshot-3", "child-b", state="poweredOn", quiesced=True),
            ],
        )
    ],
    current="snapshot-4",
)


# ---------------------------------------------------------------------------
# snapshot_rows / descendants
# ---------------------------------------------------------------------------


def test_rows_are_flat_in_tree_order_with_parents() -> None:
    current, rows = snapshot_rows(_TREE)
    assert current == "snapshot-4"
    assert [(r["snapshot_id"], r["parent_snapshot_id"]) for r in rows] == [
        ("snapshot-1", None),
        ("snapshot-2", "snapshot-1"),
        ("snapshot-4", "snapshot-2"),
        ("snapshot-3", "snapshot-1"),
    ]
    assert [r["snapshot_id"] for r in rows if r["current"]] == ["snapshot-4"]


def test_row_fields() -> None:
    _current, rows = snapshot_rows(_TREE)
    assert rows[3] == {
        "snapshot_id": "snapshot-3",
        "name": "child-b",
        "description": "about child-b",
        "created": "2026-10-01T08:00:00.000Z",
        "power_state": "poweredOn",
        "quiesced": True,
        "current": False,
        "parent_snapshot_id": "snapshot-1",
    }


@pytest.mark.parametrize("value", [None, {}, {"rootSnapshotList": []}, "junk"])
def test_no_snapshots_gives_no_rows(value: Any) -> None:
    assert snapshot_rows(value) == (None, [])


def test_a_lone_node_as_a_dict_is_read() -> None:
    """The SOAP path turns a one-element list into a plain dict."""
    root = _node("snapshot-7", "only")
    root["childSnapshotList"] = _node("snapshot-8", "child")
    _current, rows = snapshot_rows(_info(root, current="snapshot-8"))
    assert [(r["snapshot_id"], r["parent_snapshot_id"]) for r in rows] == [
        ("snapshot-7", None),
        ("snapshot-8", "snapshot-7"),
    ]


def test_descendants_are_the_whole_subtree() -> None:
    _current, rows = snapshot_rows(_TREE)
    assert [r["snapshot_id"] for r in descendants(rows, "snapshot-1")] == [
        "snapshot-2",
        "snapshot-4",
        "snapshot-3",
    ]
    assert [r["snapshot_id"] for r in descendants(rows, "snapshot-2")] == ["snapshot-4"]
    assert descendants(rows, "snapshot-4") == []


# ---------------------------------------------------------------------------
# snapshot_list_impl
# ---------------------------------------------------------------------------


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


class _FakeConnector:
    """Records the VI-JSON calls and serves one canned answer."""

    def __init__(self, answer: Any = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, Any, bool]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        del target, operator
        self.calls.append((path, json, promote_managed_object_not_found))
        if self.error is not None:
            raise self.error
        return self.answer


def _answer(props: dict[str, Any], *, missing: list[str] | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "obj": {"type": "VirtualMachine", "value": "vm-42"},
        "propSet": [{"name": k, "val": v} for k, v in props.items()],
    }
    if missing:
        obj["missingSet"] = [{"path": p, "fault": {"_typeName": "NoPermission"}} for p in missing]
    return {"objects": [obj]}


async def _list(conn: _FakeConnector, vm: str = "vm-42") -> dict[str, Any]:
    return await snapshot_list_impl(conn, _operator(), _Target(), {"vm": vm})  # type: ignore[arg-type]


async def test_list_reads_name_and_snapshot_of_one_vm() -> None:
    conn = _FakeConnector(_answer({"name": "app-vm", "snapshot": _TREE}))
    out = await _list(conn)

    [(path, body, promote)] = conn.calls
    assert path == _RETRIEVE_PATH
    assert promote is True
    spec = body["specSet"][0]
    assert spec["propSet"][0]["type"] == "VirtualMachine"
    assert spec["propSet"][0]["pathSet"] == ["name", "snapshot", "layoutEx"]
    assert spec["objectSet"][0]["obj"]["value"] == "vm-42"

    assert out["vm"] == "vm-42"
    assert out["vm_name"] == "app-vm"
    assert out["current_snapshot_id"] == "snapshot-4"
    assert out["snapshot_count"] == 4
    assert [r["name"] for r in out["snapshots"]] == ["base", "child-a", "grandchild", "child-b"]
    Draft202012Validator(VMWARE_SNAPSHOT_LIST_OP.response_schema).validate(out)


async def test_list_of_a_vm_without_snapshots_is_empty() -> None:
    conn = _FakeConnector(_answer({"name": "app-vm"}))
    out = await _list(conn)
    assert out == {
        "vm": "vm-42",
        "vm_name": "app-vm",
        "current_snapshot_id": None,
        "snapshot_count": 0,
        "read_note": None,
        "snapshots": [],
    }
    Draft202012Validator(VMWARE_SNAPSHOT_LIST_OP.response_schema).validate(out)


async def test_list_unwraps_boxed_values() -> None:
    boxed = {"_typeName": "VirtualMachineSnapshotInfo", "_value": _TREE}
    conn = _FakeConnector(
        _answer({"name": {"_typeName": "string", "_value": "app-vm"}, "snapshot": boxed})
    )
    out = await _list(conn)
    assert out["vm_name"] == "app-vm"
    assert out["snapshot_count"] == 4


async def test_unknown_vm_is_not_found() -> None:
    conn = _FakeConnector(error=ConnectorResourceNotFoundError(["vm-404"], "gone"))
    with pytest.raises(ConnectorResourceNotFoundError):
        await _list(conn, "vm-404")


async def test_vm_missing_from_the_answer_is_not_found() -> None:
    conn = _FakeConnector({"objects": []})
    with pytest.raises(ConnectorResourceNotFoundError):
        await _list(conn)


async def test_unreadable_snapshot_property_is_an_error_not_an_empty_list() -> None:
    conn = _FakeConnector(_answer({"name": "app-vm"}, missing=["snapshot"]))
    with pytest.raises(RuntimeError, match="could not read the snapshots"):
        await _list(conn)


# ---------------------------------------------------------------------------
# The real connector over mocked HTTP (vCenter 9.0 and 8.0 paths)
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


@pytest.mark.parametrize(("version", "release"), [("9.0.0", "9.0.0.0"), ("8.0.3", "8.0.3.0")])
async def test_list_rides_the_vijson_path_of_the_release(version: str, release: str) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": version})
            read = mock.post(f"/sdk/vim25/{release}{_RETRIEVE_PATH}").respond(
                200, json=_answer({"name": "app-vm", "snapshot": _TREE})
            )
            out = await connector.snapshot_list(_operator(), _StubTarget(), {"vm": "vm-42"})
        assert read.call_count == 1
        assert out["snapshot_count"] == 4
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_in_its_own_group() -> None:
    op = VMWARE_SNAPSHOT_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.snapshot.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_an_optional_vm_and_age() -> None:
    schema = VMWARE_SNAPSHOT_LIST_OP.parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
    assert validate_params(schema, {"vm": "vm-42"}) == []
    assert validate_params(schema, {}) == []
    assert validate_params(schema, {"older_than_days": 7}) == []
    assert validate_params(schema, {"older_than_days": -1}) != []
    assert validate_params(schema, {"vm": "vm-42", "name": "x"}) != []
