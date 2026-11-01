# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.network.list`` typed read.

* :func:`network_list_impl` against a recording fake connector with saved
  answers: one row per network of every type with its datacenter, the switch
  and VLAN of the distributed port groups (access and trunk), an NSX segment
  (a distributed port group with backing type ``nsx``), the filters on
  the ``/api`` and the legacy ``/rest`` mount, the name pattern, a failed vim
  read and a failed list;
* the real connector over mocked HTTP: the calls land on ``/api`` with the
  bare filter names (the same on vCenter 8 and 9);
* the operation metadata: ``safe``, runs at once, on both catalogs.
"""

from __future__ import annotations

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
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_network_list import (
    VMWARE_NETWORK_LIST_OP,
    network_list_impl,
)
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.operations._validate import validate_params

_DVPG = "DistributedVirtualPortgroup"
_VDS = "VmwareDistributedVirtualSwitch"

#: Saved answers of a small vCenter with two datacenters.
_DATACENTERS = [
    {"datacenter": "datacenter-3", "name": "dc-a"},
    {"datacenter": "datacenter-50", "name": "dc-b"},
]
_NETWORKS: dict[str, list[dict[str, Any]]] = {
    "datacenter-3": [
        {"network": "network-12", "name": "VM Network", "type": "STANDARD_PORTGROUP"},
        {"network": "dvportgroup-20", "name": "prod-web", "type": "DISTRIBUTED_PORTGROUP"},
        {"network": "dvportgroup-21", "name": "prod-trunk", "type": "DISTRIBUTED_PORTGROUP"},
    ],
    "datacenter-50": [
        {"network": "network-o-7", "name": "seg-app", "type": "OPAQUE_NETWORK"},
        {"network": "dvportgroup-60", "name": "lab", "type": "DISTRIBUTED_PORTGROUP"},
        # An NSX segment on a distributed switch (vSphere 7.0 and later).
        {"network": "dvportgroup-70", "name": "seg-web", "type": "DISTRIBUTED_PORTGROUP"},
    ],
}
#: The networks in folder ``group-n5`` (only some of datacenter-3).
_FOLDER = {"group-n5": {"network-12", "dvportgroup-20"}}


def _vlan_access(vlan_id: int) -> dict[str, Any]:
    return {
        "_typeName": "VMwareDVSPortSetting",
        "vlan": {"_typeName": "VmwareDistributedVirtualSwitchVlanIdSpec", "vlanId": vlan_id},
    }


def _vlan_trunk(ranges: list[tuple[int, int]]) -> dict[str, Any]:
    return {
        "_typeName": "VMwareDVSPortSetting",
        "vlan": {
            "_typeName": "VmwareDistributedVirtualSwitchTrunkVlanSpec",
            "vlanId": [{"_typeName": "NumericRange", "start": s, "end": e} for s, e in ranges],
        },
    }


_VIM: dict[str, dict[str, dict[str, Any]]] = {
    _DVPG: {
        "dvportgroup-20": {
            "config.distributedVirtualSwitch": vim_moref(_VDS, "dvs-1"),
            "config.defaultPortConfig": _vlan_access(110),
            "config.backingType": "standard",
        },
        "dvportgroup-21": {
            "config.distributedVirtualSwitch": vim_moref(_VDS, "dvs-1"),
            "config.defaultPortConfig": _vlan_trunk([(300, 300), (100, 200)]),
        },
        # A private VLAN: the switch is known, the VLAN shape is not shown.
        "dvportgroup-60": {
            "config.distributedVirtualSwitch": vim_moref(_VDS, "dvs-2"),
            "config.defaultPortConfig": {
                "_typeName": "VMwareDVSPortSetting",
                "vlan": {"_typeName": "VmwareDistributedVirtualSwitchPvlanSpec", "pvlanId": 5},
            },
        },
        # Made by NSX Manager: backing type nsx, VLAN 0 on the switch.
        "dvportgroup-70": {
            "config.distributedVirtualSwitch": vim_moref(_VDS, "dvs-2"),
            "config.defaultPortConfig": _vlan_access(0),
            "config.backingType": "nsx",
        },
    },
    _VDS: {"dvs-1": {"name": "dvs-prod"}, "dvs-2": {"name": "dvs-lab"}},
}


def _operator() -> Operator:
    return Operator(
        sub="op-network-list",
        name="Network List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _http_error(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://vc.test.invalid{path}")
    return httpx.HTTPStatusError(
        "503 Service Unavailable", request=request, response=httpx.Response(503, request=request)
    )


class _FakeConnector:
    """Records the calls and answers them from the saved answers."""

    def __init__(
        self,
        *,
        mount_prefix: str = "/api",
        fail_paths: tuple[str, ...] = (),
        fail_vim: bool = False,
        networks_answer: Any = None,
        envelope: bool = False,
    ) -> None:
        self._mount_prefix = mount_prefix
        self._fail_paths = fail_paths
        self._fail_vim = fail_vim
        self._networks_answer = networks_answer
        self._envelope = envelope
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.vim_reads: list[tuple[str, list[str]]] = []
        self.vim_paths: list[list[str]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

    def _answer(self, spec_path: str, params: dict[str, Any] | None) -> Any:
        if spec_path == "/vcenter/datacenter":
            return _DATACENTERS
        assert spec_path == "/vcenter/network"
        if self._networks_answer is not None:
            return self._networks_answer
        bare = {key.removeprefix("filter."): values for key, values in (params or {}).items()}
        (dc_id,) = bare["datacenters"]
        rows = _NETWORKS.get(dc_id, [])
        if "types" in bare:
            rows = [row for row in rows if row["type"] in bare["types"]]
        if "folders" in bare:
            (folder,) = bare["folders"]
            rows = [row for row in rows if row["network"] in _FOLDER[folder]]
        return rows

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        self.get_calls.append((path, params))
        spec_path = path.removeprefix(self._mount_prefix)
        if spec_path in self._fail_paths:
            raise _http_error(path)
        answer = self._answer(spec_path, params)
        return {"value": answer} if self._envelope else answer

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: dict[str, Any] | None = None
    ) -> Any:
        del target, operator
        assert path == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        assert json is not None
        spec = json["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        path_set = spec["propSet"][0]["pathSet"]
        moids = [o["obj"]["value"] for o in spec["objectSet"]]
        self.vim_reads.append((mo_type, moids))
        self.vim_paths.append(path_set)
        if self._fail_vim:
            raise _http_error(path)
        objects = [
            {
                "obj": vim_moref(mo_type, moid),
                "propSet": [
                    {"name": name, "val": value}
                    for name, value in _VIM[mo_type][moid].items()
                    if name in path_set
                ],
            }
            for moid in moids
        ]
        return {"_typeName": "RetrieveResult", "objects": objects}


def _by_id(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in out["networks"]}


async def test_lists_every_network_of_every_type_with_its_datacenter() -> None:
    conn = _FakeConnector()
    out = await network_list_impl(conn, _operator(), _Target(), {})
    Draft202012Validator(VMWARE_NETWORK_LIST_OP.response_schema).validate(out)
    rows = _by_id(out)
    assert list(rows) == [
        "network-12",
        "dvportgroup-20",
        "dvportgroup-21",
        "network-o-7",
        "dvportgroup-60",
        "dvportgroup-70",
    ]
    assert rows["network-12"] == {
        "id": "network-12",
        "name": "VM Network",
        "type": "STANDARD_PORTGROUP",
        "datacenter": "datacenter-3",
        "datacenter_name": "dc-a",
        "switch_id": None,
        "switch_name": None,
        "vlan_mode": None,
        "vlan_id": None,
        "vlan_trunk_ranges": None,
        "nsx_segment": None,
        "read_note": None,
    }
    assert rows["dvportgroup-20"] == {
        "id": "dvportgroup-20",
        "name": "prod-web",
        "type": "DISTRIBUTED_PORTGROUP",
        "datacenter": "datacenter-3",
        "datacenter_name": "dc-a",
        "switch_id": "dvs-1",
        "switch_name": "dvs-prod",
        "vlan_mode": "access",
        "vlan_id": 110,
        "vlan_trunk_ranges": None,
        "nsx_segment": False,
        "read_note": None,
    }
    trunk = rows["dvportgroup-21"]
    assert (trunk["vlan_mode"], trunk["vlan_id"], trunk["vlan_trunk_ranges"]) == (
        "trunk",
        None,
        "100-200,300",
    )
    opaque = rows["network-o-7"]
    assert (opaque["type"], opaque["datacenter"], opaque["datacenter_name"]) == (
        "OPAQUE_NETWORK",
        "datacenter-50",
        "dc-b",
    )
    pvlan = rows["dvportgroup-60"]
    assert (pvlan["switch_name"], pvlan["vlan_mode"], pvlan["read_note"]) == (
        "dvs-lab",
        None,
        None,
    )
    # No backingType in the answer: the vendor default is standard.
    assert trunk["nsx_segment"] is False
    assert pvlan["nsx_segment"] is False


async def test_an_nsx_segment_is_a_distributed_portgroup_with_nsx_segment_true() -> None:
    conn = _FakeConnector()
    out = await network_list_impl(
        conn, _operator(), _Target(), {"type": "DISTRIBUTED_PORTGROUP", "name": "seg-*"}
    )
    Draft202012Validator(VMWARE_NETWORK_LIST_OP.response_schema).validate(out)
    (segment,) = out["networks"]
    assert segment == {
        "id": "dvportgroup-70",
        "name": "seg-web",
        "type": "DISTRIBUTED_PORTGROUP",
        "datacenter": "datacenter-50",
        "datacenter_name": "dc-b",
        "switch_id": "dvs-2",
        "switch_name": "dvs-lab",
        "vlan_mode": "access",
        "vlan_id": 0,
        "vlan_trunk_ranges": None,
        "nsx_segment": True,
        "read_note": None,
    }
    (pg_read, _) = conn.vim_reads
    assert pg_read == (_DVPG, ["dvportgroup-70"])
    assert "config.backingType" in conn.vim_paths[0]


def test_the_portgroup_audit_does_not_read_the_backing_type() -> None:
    from meho_backplane.connectors.vmware_rest.composites._read import _PORTGROUP_AUDIT_PATHS

    assert "config.backingType" not in _PORTGROUP_AUDIT_PATHS


async def test_one_network_call_per_datacenter_and_vim_reads_only_portgroups() -> None:
    conn = _FakeConnector()
    await network_list_impl(conn, _operator(), _Target(), {})
    assert conn.get_calls == [
        ("/api/vcenter/datacenter", None),
        ("/api/vcenter/network", {"datacenters": ["datacenter-3"]}),
        ("/api/vcenter/network", {"datacenters": ["datacenter-50"]}),
    ]
    assert conn.vim_reads[0] == (
        _DVPG,
        ["dvportgroup-20", "dvportgroup-21", "dvportgroup-60", "dvportgroup-70"],
    )
    assert conn.vim_reads[1] == (_VDS, ["dvs-1", "dvs-2"])


async def test_no_vim_read_without_distributed_portgroups() -> None:
    conn = _FakeConnector()
    out = await network_list_impl(conn, _operator(), _Target(), {"type": "OPAQUE_NETWORK"})
    assert [row["id"] for row in out["networks"]] == ["network-o-7"]
    assert conn.vim_reads == []


async def test_filters_use_the_bare_names_on_api() -> None:
    conn = _FakeConnector()
    out = await network_list_impl(
        conn,
        _operator(),
        _Target(),
        {"type": "DISTRIBUTED_PORTGROUP", "datacenter": "datacenter-3", "folder": "group-n5"},
    )
    assert conn.get_calls[1:] == [
        (
            "/api/vcenter/network",
            {
                "types": ["DISTRIBUTED_PORTGROUP"],
                "folders": ["group-n5"],
                "datacenters": ["datacenter-3"],
            },
        )
    ]
    (row,) = out["networks"]
    assert (row["id"], row["datacenter_name"]) == ("dvportgroup-20", "dc-a")


async def test_filters_use_the_prefixed_names_on_the_legacy_rest_mount() -> None:
    conn = _FakeConnector(mount_prefix="/rest", envelope=True)
    out = await network_list_impl(
        conn, _operator(), _Target(), {"datacenter": "datacenter-50", "type": "OPAQUE_NETWORK"}
    )
    assert conn.get_calls[1:] == [
        (
            "/rest/vcenter/network",
            {"filter.types": ["OPAQUE_NETWORK"], "filter.datacenters": ["datacenter-50"]},
        )
    ]
    assert [row["id"] for row in out["networks"]] == ["network-o-7"]


async def test_an_unknown_datacenter_gives_no_rows_and_no_name() -> None:
    conn = _FakeConnector()
    out = await network_list_impl(conn, _operator(), _Target(), {"datacenter": "datacenter-99"})
    assert out == {"networks": []}
    assert conn.get_calls[1] == ("/api/vcenter/network", {"datacenters": ["datacenter-99"]})


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("prod*", ["dvportgroup-20", "dvportgroup-21"]),
        ("vm network", ["network-12"]),
        ("?ab", ["dvportgroup-60"]),
        ("nothing*", []),
    ],
)
async def test_name_pattern_keeps_only_matching_networks(pattern: str, expected: list[str]) -> None:
    conn = _FakeConnector()
    out = await network_list_impl(conn, _operator(), _Target(), {"name": pattern})
    assert [row["id"] for row in out["networks"]] == expected
    if not expected:
        assert conn.vim_reads == []


async def test_a_failed_vim_read_keeps_the_rows_with_a_note() -> None:
    conn = _FakeConnector(fail_vim=True)
    out = await network_list_impl(conn, _operator(), _Target(), {})
    Draft202012Validator(VMWARE_NETWORK_LIST_OP.response_schema).validate(out)
    rows = _by_id(out)
    assert len(rows) == 6
    pg = rows["dvportgroup-20"]
    assert (pg["switch_id"], pg["vlan_mode"], pg["nsx_segment"]) == (None, None, None)
    assert "switch, VLAN and backing not read" in pg["read_note"]
    assert "HTTPStatusError" in pg["read_note"]
    # Only distributed port groups have a switch and VLAN, so only they get a note.
    assert rows["network-12"]["read_note"] is None
    assert rows["network-o-7"]["read_note"] is None


@pytest.mark.parametrize("path", ["/vcenter/datacenter", "/vcenter/network"])
async def test_a_failed_list_fails_the_operation(path: str) -> None:
    conn = _FakeConnector(fail_paths=(path,))
    with pytest.raises(httpx.HTTPStatusError):
        await network_list_impl(conn, _operator(), _Target(), {})


async def test_a_network_list_that_is_not_a_list_fails_the_operation() -> None:
    conn = _FakeConnector(networks_answer={"unexpected": "shape"})
    with pytest.raises(RuntimeError, match="expected a list"):
        await network_list_impl(conn, _operator(), _Target(), {})


async def test_entries_without_an_id_are_skipped() -> None:
    conn = _FakeConnector(networks_answer=[{"name": "no-id"}, "junk", _NETWORKS["datacenter-3"][0]])
    out = await network_list_impl(conn, _operator(), _Target(), {"datacenter": "datacenter-3"})
    assert [row["id"] for row in out["networks"]] == ["network-12"]


# ---------------------------------------------------------------------------
# The real connector over mocked HTTP
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


async def test_real_connector_reads_api_paths_with_the_bare_filters() -> None:
    """The REST paths are the same on vCenter 8 and 9; no release lookup is needed."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/vcenter/datacenter").respond(200, json=_DATACENTERS[:1])
            networks = mock.get(
                "/api/vcenter/network",
                params={"datacenters": "datacenter-3", "types": "STANDARD_PORTGROUP"},
            ).respond(200, json=_NETWORKS["datacenter-3"][:1])
            out = await connector.network_list(
                _operator(), _StubTarget(), {"type": "STANDARD_PORTGROUP"}
            )
        assert networks.call_count == 1
        (row,) = out["networks"]
        assert (row["id"], row["type"], row["datacenter_name"]) == (
            "network-12",
            "STANDARD_PORTGROUP",
            "dc-a",
        )
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_that_runs_at_once() -> None:
    op = VMWARE_NETWORK_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.network.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_only_the_optional_filters() -> None:
    schema = VMWARE_NETWORK_LIST_OP.parameter_schema
    assert schema["properties"]["type"]["enum"] == [
        "STANDARD_PORTGROUP",
        "DISTRIBUTED_PORTGROUP",
        "OPAQUE_NETWORK",
    ]
    assert validate_params(schema, {}) == []
    assert (
        validate_params(
            schema,
            {
                "type": "OPAQUE_NETWORK",
                "datacenter": "datacenter-3",
                "folder": "group-n5",
                "name": "a*",
            },
        )
        == []
    )
    assert validate_params(schema, {"type": "VLAN"}) != []
    assert validate_params(schema, {"name": ""}) != []
    assert validate_params(schema, {"datacenter": ""}) != []
    assert validate_params(schema, {"cluster": "x"}) != []
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
