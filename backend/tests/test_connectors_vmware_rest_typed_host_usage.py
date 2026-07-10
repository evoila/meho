# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.usage`` typed op (#2257).

``vmware.host.usage`` is the first vmware ``source_kind="typed"`` op: a
bound method on :class:`VmwareRestConnector` that reads per-host
utilisation directly on the connector session (no ``dispatch_child``, no
ingested descriptor), so it works on a fresh boot with zero catalog
ingest.

The handler logic is exercised against a fake connector that records
:meth:`mount_op_path` / :meth:`_get_json` / :meth:`_post_json` calls, so
the assertion targets are the call-shape contract (list-then-per-host,
both calls mounted, best-effort per host) and the parse/aggregation
contract, without a live httpx transport. The end-to-end transport +
modern/legacy mount routing is covered by the respx integration test in
``tests/integration/test_connectors_vmware_rest_vcsim.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_HOST_USAGE_OP,
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
    build_host_usage_retrieve_params,
    host_usage_impl,
)

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for the typed-op handler unit tests."""
    return Operator(
        sub="op-host-usage",
        name="Host Usage Test",
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


class _FakeConnector:
    """Records the transport calls ``host_usage_impl`` makes.

    Mirrors the surface of :class:`VmwareRestConnector` the handler uses:
    :meth:`mount_op_path` (prefixes the live mount), :meth:`_get_json`
    (the host listing), :meth:`_post_json` (per-host RetrievePropertiesEx).
    """

    def __init__(
        self,
        *,
        listing: Any,
        props_by_host: dict[str, Any] | None = None,
        post_error: Exception | None = None,
        mount_prefix: str = "/api",
    ) -> None:
        self._listing = listing
        self._props_by_host = props_by_host or {}
        self._post_error = post_error
        self._mount_prefix = mount_prefix
        self.mount_calls: list[str] = []
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

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
        return self._listing

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
        if self._post_error is not None:
            raise self._post_error
        moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
        return self._props_by_host[moid]


def _retrieve_result(moid: str, quick_stats: Any, hardware: Any, in_maint: Any) -> dict[str, Any]:
    """Build a RetrievePropertiesEx ``RetrieveResult`` for one host."""
    return {
        "objects": [
            {
                "obj": {"type": "HostSystem", "value": moid},
                "propSet": [
                    {"name": "summary.quickStats", "val": quick_stats},
                    {"name": "summary.hardware", "val": hardware},
                    {"name": "runtime.inMaintenanceMode", "val": in_maint},
                ],
            }
        ]
    }


_QS_FULL = {"overallCpuUsage": 4200, "overallMemoryUsage": 65536, "uptime": 864000}
_HW_FULL = {
    "cpuModel": "Intel(R) Xeon(R) Gold 6248R",
    "cpuMhz": 3000,
    "numCpuPkgs": 2,
    "numCpuCores": 48,
    "numCpuThreads": 96,
    "memorySize": 274877906944,
}


# ---------------------------------------------------------------------------
# Pure builders / parsers
# ---------------------------------------------------------------------------


def test_build_retrieve_params_shape_is_single_host_property_filter() -> None:
    body = build_host_usage_retrieve_params("host-42")
    assert set(body) == {"specSet", "options"}
    assert body["options"] == {}
    (spec,) = body["specSet"]
    (prop_spec,) = spec["propSet"]
    assert prop_spec["type"] == "HostSystem"
    assert prop_spec["pathSet"] == [
        "summary.quickStats",
        "summary.hardware",
        "runtime.inMaintenanceMode",
    ]
    (obj_spec,) = spec["objectSet"]
    assert obj_spec["obj"] == {"type": "HostSystem", "value": "host-42"}


# ---------------------------------------------------------------------------
# host_usage_impl — call-shape + aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_usage_lists_then_reads_each_host_mounted() -> None:
    listing = [
        {"host": "host-1", "name": "esx-1"},
        {"host": "host-2", "name": "esx-2"},
    ]
    props = {
        "host-1": _retrieve_result("host-1", _QS_FULL, _HW_FULL, False),
        "host-2": _retrieve_result("host-2", _QS_FULL, _HW_FULL, True),
    }
    conn = _FakeConnector(listing=listing, props_by_host=props)

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    # Both the host listing and the PropertyCollector path were mounted.
    assert conn.mount_calls[0] == "/vcenter/host"
    assert "/PropertyCollector/propertyCollector/RetrievePropertiesEx" in conn.mount_calls
    # The listing GET used the mounted (/api-prefixed) path.
    assert conn.get_calls[0][0] == "/api/vcenter/host"
    # One RetrievePropertiesEx POST per host, all against the mounted path.
    assert len(conn.post_calls) == 2
    assert all(
        path == "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        for path, _ in conn.post_calls
    )

    rows = out["hosts"]
    assert [r["id"] for r in rows] == ["host-1", "host-2"]
    assert rows[0]["name"] == "esx-1"
    assert rows[0]["quick_stats"] == {
        "overall_cpu_usage_mhz": 4200,
        "overall_memory_usage_mb": 65536,
        "uptime_seconds": 864000,
    }
    assert rows[0]["hardware"] == {
        "cpu_model": "Intel(R) Xeon(R) Gold 6248R",
        "cpu_mhz": 3000,
        "num_cpu_packages": 2,
        "num_cpu_cores": 48,
        "num_cpu_threads": 96,
        "memory_size_bytes": 274877906944,
    }
    assert rows[0]["in_maintenance_mode"] is False
    assert rows[1]["in_maintenance_mode"] is True


@pytest.mark.asyncio
async def test_host_usage_resolves_mount_once_not_per_host() -> None:
    """The RetrievePropertiesEx path is host-independent — mount it once."""
    listing = [{"host": f"host-{i}", "name": f"esx-{i}"} for i in range(3)]
    props = {
        f"host-{i}": _retrieve_result(f"host-{i}", _QS_FULL, _HW_FULL, False) for i in range(3)
    }
    conn = _FakeConnector(listing=listing, props_by_host=props)

    await host_usage_impl(conn, _make_operator(), _Target(), {})

    # Exactly two mount calls total: the host listing + one for the
    # per-host property path (resolved once, reused across the 3 hosts).
    assert conn.mount_calls == [
        "/vcenter/host",
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
    ]


@pytest.mark.asyncio
async def test_host_usage_filter_hosts_flows_into_listing_query() -> None:
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", _QS_FULL, _HW_FULL, False)},
    )

    await host_usage_impl(conn, _make_operator(), _Target(), {"filter_hosts": ["host-1", "host-2"]})

    assert conn.get_calls[0][1] == {"filter.hosts": ["host-1", "host-2"]}


@pytest.mark.asyncio
async def test_host_usage_no_filter_sends_no_query_params() -> None:
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", _QS_FULL, _HW_FULL, False)},
    )

    await host_usage_impl(conn, _make_operator(), _Target(), {})

    assert conn.get_calls[0][1] is None


@pytest.mark.asyncio
async def test_host_usage_tolerates_legacy_value_envelope_on_listing() -> None:
    conn = _FakeConnector(
        listing={"value": [{"host": "host-1", "name": "esx-1"}]},
        props_by_host={"host-1": _retrieve_result("host-1", _QS_FULL, _HW_FULL, False)},
    )

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    assert [r["id"] for r in out["hosts"]] == ["host-1"]


@pytest.mark.asyncio
async def test_host_usage_skips_malformed_listing_entries() -> None:
    listing = [
        "not-a-dict",
        {"name": "no-moid"},  # missing ``host`` key
        {"host": "host-3", "name": "esx-3"},
    ]
    conn = _FakeConnector(
        listing=listing,
        props_by_host={"host-3": _retrieve_result("host-3", _QS_FULL, _HW_FULL, False)},
    )

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    assert [r["id"] for r in out["hosts"]] == ["host-3"]
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_host_usage_raises_on_non_list_listing() -> None:
    conn = _FakeConnector(listing={"unexpected": "shape"})

    with pytest.raises(RuntimeError, match="expected a list of hosts"):
        await host_usage_impl(conn, _make_operator(), _Target(), {})


@pytest.mark.asyncio
async def test_host_usage_per_host_read_is_best_effort() -> None:
    """A failed RetrievePropertiesEx nulls the detail + records read_note."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        post_error=httpx.HTTPStatusError(
            "boom", request=httpx.Request("POST", "https://vc/api"), response=httpx.Response(500)
        ),
    )

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    (row,) = out["hosts"]
    assert row["id"] == "host-1"
    assert row["quick_stats"] is None
    assert row["hardware"] is None
    assert row["in_maintenance_mode"] is None
    assert "read_note" in row
    assert "RetrievePropertiesEx" in row["read_note"]


@pytest.mark.asyncio
async def test_host_usage_partial_props_map_to_none_fields() -> None:
    """Absent quickStats/hardware leaves map to None, not KeyError."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", {}, {}, None)},
    )

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    (row,) = out["hosts"]
    assert row["quick_stats"] == {
        "overall_cpu_usage_mhz": None,
        "overall_memory_usage_mb": None,
        "uptime_seconds": None,
    }
    assert row["hardware"]["memory_size_bytes"] is None
    assert row["in_maintenance_mode"] is None


@pytest.mark.asyncio
async def test_host_usage_coerces_numeric_string_memory_size() -> None:
    """A 64-bit memorySize rendered as a JSON string still parses to int."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={
            "host-1": _retrieve_result(
                "host-1", _QS_FULL, {**_HW_FULL, "memorySize": "274877906944"}, False
            )
        },
    )

    out = await host_usage_impl(conn, _make_operator(), _Target(), {})

    assert out["hosts"][0]["hardware"]["memory_size_bytes"] == 274877906944


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_host_usage_op_is_a_registered_typed_op() -> None:
    assert VMWARE_HOST_USAGE_OP in VMWARE_TYPED_OPS
    assert VMWARE_HOST_USAGE_OP.op_id == "vmware.host.usage"
    assert VMWARE_HOST_USAGE_OP.safety_level == "safe"
    assert VMWARE_HOST_USAGE_OP.requires_approval is False


def test_host_usage_handler_attr_resolves_to_a_connector_bound_method() -> None:
    """handler_attr must name a real async method (dispatcher binds it)."""
    handler = getattr(VmwareRestConnector, VMWARE_HOST_USAGE_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_host_usage_group_has_non_empty_when_to_use() -> None:
    """A grouped typed op needs a non-empty when_to_use (typed_register)."""
    group_key = VMWARE_HOST_USAGE_OP.group_key
    assert group_key is not None
    blurb = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(group_key)
    assert isinstance(blurb, str)
    assert blurb.strip()


def test_host_usage_handler_signature_has_no_dispatch_child() -> None:
    """Typed handler must NOT accept dispatch_child (else it'd be a composite)."""
    import inspect

    params = inspect.signature(VmwareRestConnector.host_usage).parameters
    assert "dispatch_child" not in params
    assert "operator" in params
