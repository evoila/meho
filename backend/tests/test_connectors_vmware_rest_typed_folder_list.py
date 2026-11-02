# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.folder.list`` typed read.

The real connector runs over mocked HTTP with saved vCenter answers (no live
vCenter):

* the folder rows with their datacenter and parent, on vCenter 9.0 and 8.0;
* the filters reach the vCenter query by their ``/api`` names;
* a failed datacenter or parent read leaves those fields ``null`` and says so
  in ``read_note``, also when the HTTP call succeeds but vCenter could not read
  the parent of one folder (a ``missingSet`` fault); a failed folder listing
  fails the operation;
* an empty list sends no extra reads;
* the operation metadata: a ``safe`` read with no approval, in its own group.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_folder_list import (
    FOLDER_TYPES,
    VMWARE_FOLDER_LIST_OP,
)
from meho_backplane.operations._validate import validate_params

_BASE = "https://vc.example.test"
_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: Saved ``GET /api/vcenter/folder`` answer: the root folder, the datacenters
#: folder below it, and three folders in one datacenter.
_FOLDERS = [
    {"folder": "group-d1", "name": "Datacenters", "type": "DATACENTER"},
    {"folder": "group-v4", "name": "vm", "type": "VIRTUAL_MACHINE"},
    {"folder": "group-v10", "name": "prod", "type": "VIRTUAL_MACHINE"},
    {"folder": "group-h5", "name": "host", "type": "HOST"},
]
_DATACENTERS = [{"datacenter": "datacenter-3", "name": "dc-a"}]
_FOLDERS_IN_DC = [row for row in _FOLDERS if row["folder"] != "group-d1"]
_PARENTS = {
    "group-v4": ("Datacenter", "datacenter-3"),
    "group-v10": ("Folder", "group-v4"),
    "group-h5": ("Datacenter", "datacenter-3"),
}


def _operator() -> Operator:
    return Operator(
        sub="op-folder-list",
        name="Folder List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


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


def _parent_answer(body: dict[str, Any]) -> dict[str, Any]:
    """A ``RetrievePropertiesEx`` answer with the parent of each asked folder."""
    objects = []
    for spec in body["specSet"][0]["objectSet"]:
        moid = spec["obj"]["value"]
        obj: dict[str, Any] = {"obj": {"type": "Folder", "value": moid}, "propSet": []}
        if moid in _PARENTS:
            mo_type, value = _PARENTS[moid]
            obj["propSet"] = [
                {
                    "name": "parent",
                    "val": {"_typeName": "ManagedObjectReference", "type": mo_type, "value": value},
                }
            ]
        objects.append(obj)
    return {"objects": objects}


def _folder_answer(request: httpx.Request) -> httpx.Response:
    """In one datacenter, only its folders; otherwise the whole list."""
    if request.url.params.get_list("datacenters"):
        return httpx.Response(200, json=_FOLDERS_IN_DC)
    return httpx.Response(200, json=_FOLDERS)


@dataclass
class _Vcenter:
    """The mocked routes, kept so a test can read the calls."""

    folder: respx.Route
    datacenter: respx.Route
    retrieve: respx.Route


@pytest.fixture
async def connector() -> AsyncIterator[VmwareRestConnector]:
    conn = VmwareRestConnector(session_loader=_stub_loader)
    yield conn
    conn._session_tokens.clear()
    for client in conn._clients.values():
        await client.aclose()
    conn._clients.clear()


def _mock_vcenter(
    mock: respx.MockRouter,
    *,
    version: str = "9.0.0",
    release: str = "9.0.0.0",
    folder: Callable[[httpx.Request], httpx.Response] = _folder_answer,
    datacenter: httpx.Response | None = None,
    retrieve: httpx.Response | None = None,
) -> _Vcenter:
    mock.post("/api/session").respond(200, json="tok")
    mock.get("/api/about").respond(200, json={"version": version})
    folder_route = mock.get("/api/vcenter/folder").mock(side_effect=folder)
    dc_route = mock.get("/api/vcenter/datacenter").mock(
        return_value=datacenter or httpx.Response(200, json=_DATACENTERS)
    )
    retrieve_route = mock.post(f"/sdk/vim25/{release}{_RETRIEVE_PATH}")
    if retrieve is None:
        retrieve_route.mock(
            side_effect=lambda request: httpx.Response(
                200, json=_parent_answer(json.loads(request.content))
            )
        )
    else:
        retrieve_route.mock(return_value=retrieve)
    return _Vcenter(folder_route, dc_route, retrieve_route)


async def _list(conn: VmwareRestConnector, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await conn.folder_list(_operator(), _StubTarget(), params or {})


@pytest.mark.parametrize(("version", "release"), [("9.0.0", "9.0.0.0"), ("8.0.3", "8.0.3.0")])
async def test_rows_carry_type_datacenter_and_parent(
    connector: VmwareRestConnector, version: str, release: str
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        calls = _mock_vcenter(mock, version=version, release=release)
        out = await _list(connector)

    assert out["read_note"] is None
    assert out["folders"] == [
        {
            "id": "group-d1",
            "name": "Datacenters",
            "type": "DATACENTER",
            "datacenter_id": None,
            "datacenter_name": None,
            "parent_id": None,
            "parent_type": None,
        },
        {
            "id": "group-v4",
            "name": "vm",
            "type": "VIRTUAL_MACHINE",
            "datacenter_id": "datacenter-3",
            "datacenter_name": "dc-a",
            "parent_id": "datacenter-3",
            "parent_type": "Datacenter",
        },
        {
            "id": "group-v10",
            "name": "prod",
            "type": "VIRTUAL_MACHINE",
            "datacenter_id": "datacenter-3",
            "datacenter_name": "dc-a",
            "parent_id": "group-v4",
            "parent_type": "Folder",
        },
        {
            "id": "group-h5",
            "name": "host",
            "type": "HOST",
            "datacenter_id": "datacenter-3",
            "datacenter_name": "dc-a",
            "parent_id": "datacenter-3",
            "parent_type": "Datacenter",
        },
    ]
    Draft202012Validator(VMWARE_FOLDER_LIST_OP.response_schema).validate(out)
    # One listing, one per datacenter, one datacenter list, one property read.
    assert calls.folder.call_count == 2
    assert calls.datacenter.call_count == 1
    assert calls.retrieve.call_count == 1
    spec = json.loads(calls.retrieve.calls[0].request.content)["specSet"][0]
    assert spec["propSet"][0] == {
        "_typeName": "PropertySpec",
        "type": "Folder",
        "pathSet": ["parent"],
    }
    assert [o["obj"]["value"] for o in spec["objectSet"]] == [r["folder"] for r in _FOLDERS]


async def test_filters_reach_vcenter_by_their_api_names(connector: VmwareRestConnector) -> None:
    params = {
        "type": "VIRTUAL_MACHINE",
        "names": ["prod"],
        "parent_folders": ["group-v4"],
        "datacenters": ["datacenter-3"],
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        calls = _mock_vcenter(mock)
        out = await _list(connector, params)

    first = calls.folder.calls[0].request.url.params
    assert first.get("type") == "VIRTUAL_MACHINE"
    assert first.get_list("names") == ["prod"]
    assert first.get_list("parent_folders") == ["group-v4"]
    assert first.get_list("datacenters") == ["datacenter-3"]
    assert not any(key.startswith("filter.") for key in first)
    per_dc = calls.folder.calls[1].request.url.params
    assert per_dc.get("type") == "VIRTUAL_MACHINE"
    assert per_dc.get_list("datacenters") == ["datacenter-3"]
    assert calls.datacenter.calls[0].request.url.params.get_list("datacenters") == ["datacenter-3"]
    assert out["read_note"] is None


async def test_failed_datacenter_read_keeps_the_list(connector: VmwareRestConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_vcenter(mock, datacenter=httpx.Response(403, json={"error_type": "UNAUTHORIZED"}))
        out = await _list(connector)

    assert [row["id"] for row in out["folders"]] == [r["folder"] for r in _FOLDERS]
    assert all(row["datacenter_id"] is None for row in out["folders"])
    assert out["folders"][2]["parent_id"] == "group-v4"
    assert out["read_note"].startswith("the datacenter read failed")
    Draft202012Validator(VMWARE_FOLDER_LIST_OP.response_schema).validate(out)


async def test_failed_parent_read_keeps_the_list(connector: VmwareRestConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_vcenter(mock, retrieve=httpx.Response(403, json={"error": "denied"}))
        out = await _list(connector)

    assert all(row["parent_id"] is None for row in out["folders"])
    assert out["folders"][1]["datacenter_id"] == "datacenter-3"
    assert out["read_note"].startswith("the parent read failed")
    Draft202012Validator(VMWARE_FOLDER_LIST_OP.response_schema).validate(out)


async def test_unreadable_parent_of_one_folder_is_reported(connector: VmwareRestConnector) -> None:
    """HTTP 200, but the ``parent`` of one folder is in its ``missingSet``.

    That folder must not look like a folder with no parent: its parent fields
    are ``null`` and ``read_note`` names the fault type (never the fault text).
    The other folders keep their parent.
    """

    def _answer(request: httpx.Request) -> httpx.Response:
        answer = _parent_answer(json.loads(request.content))
        for obj in answer["objects"]:
            if obj["obj"]["value"] == "group-v10":
                obj["propSet"] = []
                obj["missingSet"] = [
                    {
                        "path": "parent",
                        "fault": {
                            "_typeName": "LocalizedMethodFault",
                            "fault": {"_typeName": "NoPermission", "object": "group-v10"},
                            "localizedMessage": "Permission to perform this operation was denied.",
                        },
                    }
                ]
        return httpx.Response(200, json=answer)

    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        vcenter = _mock_vcenter(mock)
        vcenter.retrieve.mock(side_effect=_answer)
        out = await _list(connector)

    by_id = {row["id"]: row for row in out["folders"]}
    assert by_id["group-v10"]["parent_id"] is None
    assert by_id["group-v10"]["parent_type"] is None
    assert by_id["group-v10"]["datacenter_id"] == "datacenter-3"
    assert by_id["group-v4"]["parent_id"] == "datacenter-3"
    assert by_id["group-h5"]["parent_id"] == "datacenter-3"
    assert out["read_note"] == "the parent read failed for 1 folder(s) with NoPermission"
    assert "denied" not in out["read_note"]
    Draft202012Validator(VMWARE_FOLDER_LIST_OP.response_schema).validate(out)


async def test_failed_folder_listing_fails_the_operation(connector: VmwareRestConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_vcenter(mock, folder=lambda _request: httpx.Response(403, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            await _list(connector)


async def test_empty_list_sends_no_extra_reads(connector: VmwareRestConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        calls = _mock_vcenter(mock, folder=lambda _request: httpx.Response(200, json=[]))
        out = await _list(connector, {"names": ["missing"]})

    assert out == {"folders": [], "read_note": None}
    assert calls.datacenter.call_count == 0
    assert calls.retrieve.call_count == 0


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_without_approval() -> None:
    op = VMWARE_FOLDER_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.folder.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_parameter_schema_checks_the_filters() -> None:
    schema = VMWARE_FOLDER_LIST_OP.parameter_schema
    assert schema["properties"]["type"]["enum"] == list(FOLDER_TYPES)
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
    assert validate_params(schema, {"type": "VM"}) != []
    assert validate_params(schema, {"names": []}) != []
    assert validate_params(schema, {"folder": "group-v4"}) != []
