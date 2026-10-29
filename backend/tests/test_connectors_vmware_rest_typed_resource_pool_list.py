# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.resource_pool.list`` typed read.

* :func:`resource_pool_list_impl` against a recording fake connector with
  saved answers: the rows and settings, the parent of each pool (another
  pool, a cluster or a standalone host), the filters on the ``/api`` and the
  legacy ``/rest`` mount, the name pattern, a failed pool read and a failed
  pool list;
* the real connector over mocked HTTP: every call lands on ``/api`` with the
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
from meho_backplane.connectors.vmware_rest.typed_ops_resource_pool_list import (
    VMWARE_RESOURCE_POOL_LIST_OP,
    resource_pool_list_impl,
)
from meho_backplane.operations._validate import validate_params


def _alloc(reservation: int, limit: int, level: str, shares: int) -> dict[str, Any]:
    return {
        "reservation": reservation,
        "expandable_reservation": True,
        "limit": limit,
        "shares": {"level": level, "shares": shares},
    }


#: Saved answers of a small vCenter: one cluster with a pool tree, one
#: standalone host with only its root pool.
_POOLS = [
    {"resource_pool": "resgroup-8", "name": "Resources"},
    {"resource_pool": "resgroup-20", "name": "prod"},
    {"resource_pool": "resgroup-21", "name": "dev"},
    {"resource_pool": "resgroup-30", "name": "prod-db"},
    {"resource_pool": "resgroup-41", "name": "Resources"},
]
_INFOS: dict[str, dict[str, Any]] = {
    "resgroup-8": {
        "name": "Resources",
        "resource_pools": ["resgroup-20", "resgroup-21"],
        "cpu_allocation": _alloc(0, -1, "NORMAL", 4000),
        "memory_allocation": _alloc(0, -1, "NORMAL", 163840),
    },
    "resgroup-20": {
        "name": "prod",
        "resource_pools": ["resgroup-30"],
        "cpu_allocation": _alloc(2000, 8000, "HIGH", 8000),
        "memory_allocation": _alloc(4096, 16384, "CUSTOM", 50000),
    },
    "resgroup-21": {
        "name": "dev",
        "resource_pools": [],
        "cpu_allocation": _alloc(0, -1, "LOW", 2000),
        "memory_allocation": _alloc(0, -1, "LOW", 81920),
    },
    "resgroup-30": {"name": "prod-db", "resource_pools": []},
    "resgroup-41": {
        "name": "Resources",
        "resource_pools": [],
        "cpu_allocation": _alloc(0, -1, "NORMAL", 4000),
        "memory_allocation": _alloc(0, -1, "NORMAL", 163840),
    },
}
_CLUSTERS = [{"cluster": "domain-c8", "name": "cl-1"}]
_CLUSTER_INFO = {"domain-c8": {"name": "cl-1", "resource_pool": "resgroup-8"}}
_HOSTS = [{"host": "host-10", "name": "esx-10"}, {"host": "host-40", "name": "esx-40"}]
#: The answer of each filtered list: (spec path, bare filter, value) -> ids.
_FILTERED: dict[tuple[str, str, str], list[str]] = {
    ("/vcenter/host", "clusters", "domain-c8"): ["host-10"],
    ("/vcenter/resource-pool", "hosts", "host-40"): ["resgroup-41"],
    ("/vcenter/resource-pool", "hosts", "host-10"): [
        "resgroup-8",
        "resgroup-20",
        "resgroup-21",
        "resgroup-30",
    ],
    ("/vcenter/resource-pool", "clusters", "domain-c8"): [
        "resgroup-8",
        "resgroup-20",
        "resgroup-21",
        "resgroup-30",
    ],
    ("/vcenter/resource-pool", "parent_resource_pools", "resgroup-20"): ["resgroup-30"],
}


def _operator() -> Operator:
    return Operator(
        sub="op-resource-pool-list",
        name="Resource Pool List Test",
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


def _summaries(path: str, ids: list[str]) -> list[dict[str, Any]]:
    if path == "/vcenter/host":
        return [host for host in _HOSTS if host["host"] in ids]
    return [pool for pool in _POOLS if pool["resource_pool"] in ids]


class _FakeConnector:
    """Records the REST calls and answers them from the saved answers."""

    def __init__(
        self,
        *,
        mount_prefix: str = "/api",
        pools: Any = None,
        fail_paths: tuple[str, ...] = (),
        envelope: bool = False,
    ) -> None:
        self._mount_prefix = mount_prefix
        self._pools = _POOLS if pools is None else pools
        self._fail_paths = fail_paths
        self._envelope = envelope
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

    def _answer(self, spec_path: str, params: dict[str, Any] | None) -> Any:
        if params:
            ((key, values),) = params.items()
            bare = key.removeprefix("filter.")
            if bare == "clusters" and spec_path == "/vcenter/host":
                ids = [h for c in values for h in _FILTERED[(spec_path, bare, c)]]
            else:
                ids = _FILTERED[(spec_path, bare, values[0])]
            return _summaries(spec_path, ids)
        if spec_path == "/vcenter/resource-pool":
            return self._pools
        if spec_path.startswith("/vcenter/resource-pool/"):
            return _INFOS[spec_path.rsplit("/", 1)[1]]
        if spec_path == "/vcenter/cluster":
            return _CLUSTERS
        if spec_path.startswith("/vcenter/cluster/"):
            return _CLUSTER_INFO[spec_path.rsplit("/", 1)[1]]
        assert spec_path == "/vcenter/host"
        return _HOSTS

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
            request = httpx.Request("GET", f"https://vc.test.invalid{path}")
            raise httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=request,
                response=httpx.Response(503, request=request),
            )
        answer = self._answer(spec_path, params)
        return {"value": answer} if self._envelope else answer


def _by_id(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in out["resource_pools"]}


async def test_lists_each_pool_with_parent_and_settings() -> None:
    conn = _FakeConnector()
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {})
    Draft202012Validator(VMWARE_RESOURCE_POOL_LIST_OP.response_schema).validate(out)
    rows = _by_id(out)
    assert list(rows) == ["resgroup-8", "resgroup-20", "resgroup-21", "resgroup-30", "resgroup-41"]
    assert rows["resgroup-20"] == {
        "id": "resgroup-20",
        "name": "prod",
        "parent_type": "resource_pool",
        "parent_id": "resgroup-8",
        "cpu_reservation_mhz": 2000,
        "cpu_limit_mhz": 8000,
        "cpu_expandable_reservation": True,
        "cpu_shares_level": "HIGH",
        "cpu_shares": 8000,
        "memory_reservation_mb": 4096,
        "memory_limit_mb": 16384,
        "memory_expandable_reservation": True,
        "memory_shares_level": "CUSTOM",
        "memory_shares": 50000,
        "read_note": None,
    }
    assert (rows["resgroup-30"]["parent_type"], rows["resgroup-30"]["parent_id"]) == (
        "resource_pool",
        "resgroup-20",
    )
    assert (rows["resgroup-8"]["parent_type"], rows["resgroup-8"]["parent_id"]) == (
        "cluster",
        "domain-c8",
    )
    assert (rows["resgroup-41"]["parent_type"], rows["resgroup-41"]["parent_id"]) == (
        "host",
        "host-40",
    )
    assert all(row["read_note"] is None for row in rows.values())


async def test_a_pool_without_allocation_fields_has_null_settings() -> None:
    conn = _FakeConnector()
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {"name": "prod-db"})
    (row,) = out["resource_pools"]
    assert row["cpu_limit_mhz"] is None
    assert row["memory_shares_level"] is None
    assert row["read_note"] is None


async def test_no_root_lookup_when_every_pool_has_a_parent_pool() -> None:
    conn = _FakeConnector()
    await resource_pool_list_impl(conn, _operator(), _Target(), {"name": "prod*"})
    paths = [path for path, _ in conn.get_calls]
    assert "/api/vcenter/cluster" not in paths
    assert "/api/vcenter/host" not in paths
    # One list call, then one detail read per pool in the vCenter.
    assert len(conn.get_calls) == 1 + len(_POOLS)


async def test_cluster_root_does_not_read_the_hosts() -> None:
    conn = _FakeConnector()
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {"cluster": "domain-c8"})
    assert [row["id"] for row in out["resource_pools"]] == [
        "resgroup-8",
        "resgroup-20",
        "resgroup-21",
        "resgroup-30",
    ]
    assert conn.get_calls[1] == ("/api/vcenter/resource-pool", {"clusters": ["domain-c8"]})
    assert "/api/vcenter/cluster/domain-c8" in [path for path, _ in conn.get_calls]
    assert "/api/vcenter/host" not in [path for path, _ in conn.get_calls]


@pytest.mark.parametrize(
    ("params", "query"),
    [
        ({"cluster": "domain-c8"}, {"clusters": ["domain-c8"]}),
        ({"host": "host-40"}, {"hosts": ["host-40"]}),
        ({"parent_pool": "resgroup-20"}, {"parent_resource_pools": ["resgroup-20"]}),
    ],
)
async def test_filters_use_the_bare_name_on_api(
    params: dict[str, str], query: dict[str, list[str]]
) -> None:
    conn = _FakeConnector()
    await resource_pool_list_impl(conn, _operator(), _Target(), params)
    assert conn.get_calls[0] == ("/api/vcenter/resource-pool", None)
    assert conn.get_calls[1] == ("/api/vcenter/resource-pool", query)


async def test_filters_use_the_prefixed_name_on_the_legacy_rest_mount() -> None:
    conn = _FakeConnector(mount_prefix="/rest", envelope=True)
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {"host": "host-40"})
    assert conn.get_calls[1] == ("/rest/vcenter/resource-pool", {"filter.hosts": ["host-40"]})
    (row,) = out["resource_pools"]
    assert (row["id"], row["parent_type"], row["parent_id"]) == ("resgroup-41", "host", "host-40")
    assert row["cpu_shares_level"] == "NORMAL"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("prod*", ["resgroup-20", "resgroup-30"]),
        ("DEV", ["resgroup-21"]),
        ("?ev", ["resgroup-21"]),
        ("nothing*", []),
    ],
)
async def test_name_pattern_keeps_only_matching_pools(pattern: str, expected: list[str]) -> None:
    conn = _FakeConnector()
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {"name": pattern})
    assert [row["id"] for row in out["resource_pools"]] == expected
    if not expected:
        # No match costs no detail read.
        assert len(conn.get_calls) == 1


async def test_a_failed_pool_read_is_null_with_a_note_and_the_rest_stays() -> None:
    conn = _FakeConnector(fail_paths=("/vcenter/resource-pool/resgroup-21",))
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {})
    Draft202012Validator(VMWARE_RESOURCE_POOL_LIST_OP.response_schema).validate(out)
    rows = _by_id(out)
    assert rows["resgroup-21"]["cpu_limit_mhz"] is None
    assert "settings not read" in rows["resgroup-21"]["read_note"]
    assert "HTTPStatusError" in rows["resgroup-21"]["read_note"]
    # Its parent is still known: the parent's own read worked.
    assert rows["resgroup-21"]["parent_id"] == "resgroup-8"
    assert rows["resgroup-20"]["read_note"] is None
    # The cluster root is exact; the host check is skipped, so the host root is unknown.
    assert rows["resgroup-8"]["parent_type"] == "cluster"
    assert rows["resgroup-41"]["parent_type"] is None
    assert "parent not known" in rows["resgroup-41"]["read_note"]
    assert "/api/vcenter/host" not in [path for path, _ in conn.get_calls]


async def test_a_failed_parent_read_leaves_the_child_parent_unknown() -> None:
    conn = _FakeConnector(fail_paths=("/vcenter/resource-pool/resgroup-20",))
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {"name": "prod-db"})
    (row,) = out["resource_pools"]
    assert (row["parent_type"], row["parent_id"]) == (None, None)
    assert "parent not known" in row["read_note"]


async def test_a_failed_cluster_read_leaves_the_root_parent_unknown() -> None:
    conn = _FakeConnector(fail_paths=("/vcenter/cluster",))
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {})
    rows = _by_id(out)
    assert rows["resgroup-8"]["parent_type"] is None
    assert "parent not read" in rows["resgroup-8"]["read_note"]
    assert rows["resgroup-20"]["parent_id"] == "resgroup-8"
    assert rows["resgroup-20"]["read_note"] is None


async def test_a_failed_pool_list_fails_the_operation() -> None:
    conn = _FakeConnector(fail_paths=("/vcenter/resource-pool",))
    with pytest.raises(httpx.HTTPStatusError):
        await resource_pool_list_impl(conn, _operator(), _Target(), {})


async def test_a_pool_list_that_is_not_a_list_fails_the_operation() -> None:
    conn = _FakeConnector(pools={"unexpected": "shape"})
    with pytest.raises(RuntimeError, match="expected a list"):
        await resource_pool_list_impl(conn, _operator(), _Target(), {})


async def test_entries_without_an_id_are_skipped() -> None:
    conn = _FakeConnector(pools=[{"name": "no-id"}, "junk", _POOLS[2]])
    out = await resource_pool_list_impl(conn, _operator(), _Target(), {})
    assert [row["id"] for row in out["resource_pools"]] == ["resgroup-21"]


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


async def test_real_connector_reads_api_paths_with_the_bare_filter() -> None:
    """The REST paths are the same on vCenter 8 and 9; no release lookup is needed."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    cluster_pools = _summaries("/vcenter/resource-pool", ["resgroup-8", "resgroup-20"])
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            filtered = mock.get(
                "/api/vcenter/resource-pool", params={"clusters": "domain-c8"}
            ).respond(200, json=cluster_pools)
            mock.get("/api/vcenter/resource-pool").respond(200, json=cluster_pools)
            mock.get("/api/vcenter/resource-pool/resgroup-8").respond(
                200, json={**_INFOS["resgroup-8"], "resource_pools": ["resgroup-20"]}
            )
            mock.get("/api/vcenter/resource-pool/resgroup-20").respond(
                200, json={**_INFOS["resgroup-20"], "resource_pools": []}
            )
            mock.get("/api/vcenter/cluster").respond(200, json=_CLUSTERS)
            mock.get("/api/vcenter/cluster/domain-c8").respond(200, json=_CLUSTER_INFO["domain-c8"])
            out = await connector.resource_pool_list(
                _operator(), _StubTarget(), {"cluster": "domain-c8"}
            )
        assert filtered.call_count == 1
        rows = _by_id(out)
        assert (rows["resgroup-8"]["parent_type"], rows["resgroup-8"]["parent_id"]) == (
            "cluster",
            "domain-c8",
        )
        assert rows["resgroup-20"]["parent_id"] == "resgroup-8"
        assert rows["resgroup-20"]["cpu_reservation_mhz"] == 2000
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_that_runs_at_once() -> None:
    op = VMWARE_RESOURCE_POOL_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.resource_pool.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_only_the_optional_filters() -> None:
    schema = VMWARE_RESOURCE_POOL_LIST_OP.parameter_schema
    assert validate_params(schema, {}) == []
    assert (
        validate_params(
            schema,
            {"cluster": "domain-c8", "host": "host-1", "parent_pool": "resgroup-8", "name": "a*"},
        )
        == []
    )
    assert validate_params(schema, {"name": ""}) != []
    assert validate_params(schema, {"cluster": ""}) != []
    assert validate_params(schema, {"datacenter": "x"}) != []
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
