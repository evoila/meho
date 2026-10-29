# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.cluster.list`` typed read.

* :func:`cluster_list_impl` against a recording fake connector with saved
  answers: the rows, the datacenter of each cluster, the host count and vSAN
  state from one property read, the filters on the ``/api`` and the legacy
  ``/rest`` mount, the name pattern, a failed property read (``null`` with
  ``read_note``), a property vCenter could not read, and a failed list (an
  error);
* the real connector over mocked HTTP: the REST calls land on ``/api`` with
  the bare filter names, the property read on the VI-JSON base;
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
from meho_backplane.connectors.vmware_rest.typed_ops_cluster_list import (
    VMWARE_CLUSTER_LIST_OP,
    cluster_list_impl,
)
from meho_backplane.operations._validate import validate_params

#: Saved answers of a small vCenter: two datacenters, three clusters.
_DATACENTERS = [
    {"datacenter": "datacenter-3", "name": "Prod-DC"},
    {"datacenter": "datacenter-21", "name": "lab-dc"},
]
_CLUSTERS: dict[str, list[dict[str, Any]]] = {
    "datacenter-3": [
        {"cluster": "domain-c8", "name": "prod-a", "ha_enabled": True, "drs_enabled": True},
        {"cluster": "domain-c9", "name": "prod-b", "ha_enabled": False, "drs_enabled": True},
    ],
    "datacenter-21": [
        {"cluster": "domain-c30", "name": "Lab", "ha_enabled": False, "drs_enabled": False},
    ],
}
#: VI-JSON answers per cluster: host count (boxed, as vCenter sends it) and
#: the vSAN part of ``configurationEx``.
_PROPS: dict[str, tuple[int, dict[str, Any]]] = {
    "domain-c8": (4, {"_typeName": "VsanClusterConfigInfo", "enabled": True}),
    "domain-c9": (2, {"_typeName": "VsanClusterConfigInfo", "enabled": False}),
    "domain-c30": (1, {"_typeName": "VsanClusterConfigInfo", "enabled": False}),
}


def _object_content(moid: str) -> dict[str, Any]:
    hosts, vsan = _PROPS[moid]
    return {
        "_typeName": "ObjectContent",
        "obj": {
            "_typeName": "ManagedObjectReference",
            "type": "ClusterComputeResource",
            "value": moid,
        },
        "propSet": [
            {"name": "summary.numHosts", "val": {"_typeName": "int", "_value": hosts}},
            {
                "name": "configurationEx",
                "val": {
                    "_typeName": "ClusterConfigInfoEx",
                    "dasConfig": {"enabled": True},
                    "vsanConfigInfo": vsan,
                },
            },
        ],
    }


def _operator() -> Operator:
    return Operator(
        sub="op-cluster-list",
        name="Cluster List Test",
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
        fail_path: str | None = None,
        fail_properties: bool = False,
        objects: list[dict[str, Any]] | None = None,
        envelope: bool = False,
    ) -> None:
        self._mount_prefix = mount_prefix
        self._fail_path = fail_path
        self._fail_properties = fail_properties
        self._objects = objects
        self._envelope = envelope
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.vim_bodies: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

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
        if spec_path == self._fail_path:
            raise _http_error(path)
        prefix = "filter." if self._mount_prefix == "/rest" else ""
        wanted = (params or {}).get(f"{prefix}datacenters")
        if spec_path == "/vcenter/datacenter":
            answer: Any = [
                dc for dc in _DATACENTERS if wanted is None or dc["datacenter"] in wanted
            ]
        else:
            assert wanted is not None and len(wanted) == 1
            answer = _CLUSTERS.get(wanted[0], [])
        return {"value": answer} if self._envelope else answer

    async def _post_vmomi_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        del target
        self.vim_bodies.append(kwargs["json"])
        assert path.endswith("/RetrievePropertiesEx")
        if self._fail_properties:
            raise _http_error(path)
        if self._objects is not None:
            return {"objects": self._objects}
        moids = [spec["obj"]["value"] for spec in kwargs["json"]["specSet"][0]["objectSet"]]
        return {"objects": [_object_content(moid) for moid in moids]}


async def test_lists_each_cluster_with_its_datacenter_and_state() -> None:
    conn = _FakeConnector()
    out = await cluster_list_impl(conn, _operator(), _Target(), {})
    assert out["clusters"][0] == {
        "id": "domain-c8",
        "name": "prod-a",
        "datacenter_id": "datacenter-3",
        "datacenter_name": "Prod-DC",
        "host_count": 4,
        "ha_enabled": True,
        "drs_enabled": True,
        "vsan_enabled": True,
        "read_note": None,
    }
    assert [(row["id"], row["datacenter_id"]) for row in out["clusters"]] == [
        ("domain-c8", "datacenter-3"),
        ("domain-c9", "datacenter-3"),
        ("domain-c30", "datacenter-21"),
    ]
    assert [row["host_count"] for row in out["clusters"]] == [4, 2, 1]
    assert [row["vsan_enabled"] for row in out["clusters"]] == [True, False, False]
    Draft202012Validator(VMWARE_CLUSTER_LIST_OP.response_schema).validate(out)


async def test_one_property_read_for_all_clusters() -> None:
    conn = _FakeConnector()
    await cluster_list_impl(conn, _operator(), _Target(), {})
    assert len(conn.vim_bodies) == 1
    spec = conn.vim_bodies[0]["specSet"][0]
    assert spec["propSet"][0]["type"] == "ClusterComputeResource"
    assert spec["propSet"][0]["pathSet"] == ["summary.numHosts", "configurationEx"]
    assert [entry["obj"]["value"] for entry in spec["objectSet"]] == [
        "domain-c8",
        "domain-c9",
        "domain-c30",
    ]


async def test_clusters_are_listed_per_datacenter_with_the_bare_filter_on_api() -> None:
    conn = _FakeConnector()
    await cluster_list_impl(conn, _operator(), _Target(), {})
    assert conn.get_calls == [
        ("/api/vcenter/datacenter", None),
        ("/api/vcenter/cluster", {"datacenters": ["datacenter-3"]}),
        ("/api/vcenter/cluster", {"datacenters": ["datacenter-21"]}),
    ]


async def test_filters_use_the_prefixed_names_on_the_legacy_rest_mount() -> None:
    conn = _FakeConnector(mount_prefix="/rest", envelope=True)
    out = await cluster_list_impl(
        conn, _operator(), _Target(), {"datacenter": "datacenter-3", "folder": "group-h4"}
    )
    assert conn.get_calls == [
        ("/rest/vcenter/datacenter", {"filter.datacenters": ["datacenter-3"]}),
        (
            "/rest/vcenter/cluster",
            {"filter.datacenters": ["datacenter-3"], "filter.folders": ["group-h4"]},
        ),
    ]
    assert [row["id"] for row in out["clusters"]] == ["domain-c8", "domain-c9"]


async def test_datacenter_and_folder_filters_on_api() -> None:
    conn = _FakeConnector()
    await cluster_list_impl(
        conn, _operator(), _Target(), {"datacenter": "datacenter-21", "folder": "group-h4"}
    )
    assert conn.get_calls == [
        ("/api/vcenter/datacenter", {"datacenters": ["datacenter-21"]}),
        ("/api/vcenter/cluster", {"datacenters": ["datacenter-21"], "folders": ["group-h4"]}),
    ]


async def test_an_unknown_datacenter_gives_an_empty_list_and_no_property_read() -> None:
    conn = _FakeConnector()
    out = await cluster_list_impl(conn, _operator(), _Target(), {"datacenter": "datacenter-99"})
    assert out == {"clusters": []}
    assert conn.vim_bodies == []


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("prod*", ["domain-c8", "domain-c9"]),
        ("LAB", ["domain-c30"]),
        ("prod-?", ["domain-c8", "domain-c9"]),
        ("nothing*", []),
    ],
)
async def test_name_pattern_keeps_only_matching_clusters(pattern: str, expected: list[str]) -> None:
    conn = _FakeConnector()
    out = await cluster_list_impl(conn, _operator(), _Target(), {"name": pattern})
    assert [row["id"] for row in out["clusters"]] == expected
    read = [e["obj"]["value"] for b in conn.vim_bodies for e in b["specSet"][0]["objectSet"]]
    assert read == expected


async def test_a_failed_property_read_is_null_with_a_note_and_the_rest_stays() -> None:
    conn = _FakeConnector(fail_properties=True)
    out = await cluster_list_impl(conn, _operator(), _Target(), {})
    first = out["clusters"][0]
    assert first["host_count"] is None
    assert first["vsan_enabled"] is None
    assert first["ha_enabled"] is True
    assert "RetrievePropertiesEx failed with HTTPStatusError" in first["read_note"]
    Draft202012Validator(VMWARE_CLUSTER_LIST_OP.response_schema).validate(out)


async def test_a_property_vcenter_could_not_read_is_named_in_the_note() -> None:
    partial = _object_content("domain-c8")
    partial["propSet"] = partial["propSet"][:1]
    partial["missingSet"] = [
        {"path": "configurationEx", "fault": {"_typeName": "NoPermission"}},
    ]
    conn = _FakeConnector(objects=[partial, _object_content("domain-c9")])
    out = await cluster_list_impl(conn, _operator(), _Target(), {})
    by_id = {row["id"]: row for row in out["clusters"]}
    assert by_id["domain-c8"]["host_count"] == 4
    assert by_id["domain-c8"]["vsan_enabled"] is None
    assert (
        by_id["domain-c8"]["read_note"] == "vCenter could not read configurationEx (NoPermission)"
    )
    assert by_id["domain-c9"]["read_note"] is None
    # vCenter sent nothing for domain-c30.
    assert by_id["domain-c30"]["host_count"] is None
    assert "sent no host count" in by_id["domain-c30"]["read_note"]


async def test_a_cluster_config_without_a_vsan_part_means_vsan_is_off() -> None:
    content = _object_content("domain-c8")
    del content["propSet"][1]["val"]["vsanConfigInfo"]
    conn = _FakeConnector(objects=[content])
    out = await cluster_list_impl(conn, _operator(), _Target(), {"name": "prod-a"})
    assert out["clusters"][0]["vsan_enabled"] is False


@pytest.mark.parametrize("fail_path", ["/vcenter/datacenter", "/vcenter/cluster"])
async def test_a_failed_list_fails_the_operation(fail_path: str) -> None:
    conn = _FakeConnector(fail_path=fail_path)
    with pytest.raises(httpx.HTTPStatusError):
        await cluster_list_impl(conn, _operator(), _Target(), {})


async def test_entries_without_a_moid_are_skipped() -> None:
    conn = _FakeConnector()
    saved = _CLUSTERS["datacenter-21"]
    _CLUSTERS["datacenter-21"] = [{"name": "no-id"}, "junk", *saved]  # type: ignore[list-item]
    try:
        out = await cluster_list_impl(conn, _operator(), _Target(), {"datacenter": "datacenter-21"})
    finally:
        _CLUSTERS["datacenter-21"] = saved
    assert [row["id"] for row in out["clusters"]] == ["domain-c30"]


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


async def test_real_connector_reads_rest_on_api_and_properties_on_vi_json() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/vcenter/datacenter").respond(200, json=_DATACENTERS[:1])
            clusters = mock.get(
                "/api/vcenter/cluster", params={"datacenters": "datacenter-3"}
            ).respond(200, json=_CLUSTERS["datacenter-3"])
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            props = mock.post(
                "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
            ).respond(
                200,
                json={"objects": [_object_content("domain-c8"), _object_content("domain-c9")]},
            )
            out = await connector.cluster_list(_operator(), _StubTarget(), {})
        assert clusters.call_count == 1
        assert props.call_count == 1
        assert [row["host_count"] for row in out["clusters"]] == [4, 2]
        assert [row["read_note"] for row in out["clusters"]] == [None, None]
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_that_runs_at_once() -> None:
    op = VMWARE_CLUSTER_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.cluster.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_only_the_three_optional_filters() -> None:
    schema = VMWARE_CLUSTER_LIST_OP.parameter_schema
    assert validate_params(schema, {}) == []
    assert validate_params(schema, {"datacenter": "datacenter-3", "folder": "group-h4"}) == []
    assert validate_params(schema, {"name": "prod*"}) == []
    assert validate_params(schema, {"name": ""}) != []
    assert validate_params(schema, {"cluster": "domain-c8"}) != []
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
