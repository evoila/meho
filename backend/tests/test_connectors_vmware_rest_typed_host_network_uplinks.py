# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.network_uplinks`` typed op (#2258).

Re-shipped from the former ``vmware.composite.host.network_uplinks``
read: a ``source_kind="typed"`` bound method on
:class:`VmwareRestConnector` that lists hosts then reads
``config.network.pnic`` + ``config.network.proxySwitch`` per host
directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a fresh boot with zero catalog ingest.

The handler logic is exercised against a fake connector that records
:meth:`mount_op_path` / :meth:`_get_json` / :meth:`_post_json` calls, so
the assertion targets are the call-shape contract (list-then-per-host,
both calls mounted, best-effort per host) and the parse/aggregation
contract, without a live httpx transport. The end-to-end transport +
modern/legacy mount routing is covered by the respx integration test in
``tests/integration/test_connectors_vmware_rest_vcsim.py``. Output parity
against the composite version is preserved byte-for-byte by these
assertions (they mirror the pre-#2258 composite-read coverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks import (
    HOST_NETWORK_UPLINKS_GROUP_KEY,
    VMWARE_HOST_NETWORK_UPLINKS_OP,
    build_host_network_uplinks_retrieve_params,
    host_network_uplinks_impl,
)

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for the typed-op handler unit tests."""
    return Operator(
        sub="op-host-uplinks",
        name="Host Uplinks Test",
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
    """Records the transport calls ``host_network_uplinks_impl`` makes.

    Serves the host listing on :meth:`_get_json` and a per-host
    RetrievePropertiesEx result keyed by the host MoRef on
    :meth:`_post_json` (the moid rides the request body's objectSet).
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

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        # Exercise the real mount-flavor adaptation against this fake's
        # mount prefix so the recorded query matches the wire form.
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
        return self._listing

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # The vmomi RetrievePropertiesEx read routes through the vmomi seam
        # with the spec-relative path; the /sdk/vim25 mount is the
        # connector's job (#2466).
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
        if self._post_error is not None:
            raise self._post_error
        moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
        return self._props_by_host[moid]


def _retrieve_result(moid: str, pnics: list[Any], proxy_switches: list[Any]) -> dict[str, Any]:
    """Build a RetrievePropertiesEx ``RetrieveResult`` for one host."""
    return {
        "objects": [
            {
                "obj": {"type": "HostSystem", "value": moid},
                "propSet": [
                    {"name": "config.network.pnic", "val": pnics},
                    {"name": "config.network.proxySwitch", "val": proxy_switches},
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_build_retrieve_params_shape_is_single_host_property_filter() -> None:
    body = build_host_network_uplinks_retrieve_params("host-42")
    assert set(body) == {"specSet", "options"}
    assert body["options"] == {}
    (spec,) = body["specSet"]
    (prop_spec,) = spec["propSet"]
    assert prop_spec["type"] == "HostSystem"
    assert prop_spec["pathSet"] == ["config.network.pnic", "config.network.proxySwitch"]
    (obj_spec,) = spec["objectSet"]
    assert obj_spec["obj"] == {"type": "HostSystem", "value": "host-42"}


# ---------------------------------------------------------------------------
# host_network_uplinks_impl — call-shape + aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_then_reads_props_per_host_mounted() -> None:
    listing = [
        {"host": "host-1", "name": "esx-1"},
        {"host": "host-2", "name": "esx-2"},
    ]
    props = {
        "host-1": _retrieve_result(
            "host-1",
            pnics=[
                {
                    "device": "vmnic0",
                    "mac": "aa:bb:cc:00:00:00",
                    "driver": "ixgbe",
                    "linkSpeed": {"speedMb": 10000, "duplex": True},
                },
                {
                    "device": "vmnic1",
                    "mac": "aa:bb:cc:00:00:01",
                    "driver": "ixgbe",
                    # No linkSpeed -> link down.
                },
            ],
            proxy_switches=[
                {
                    "key": "key-vim.host.HostProxySwitch-1",
                    "dvsName": "DVS-A",
                    "dvsUuid": "50 01 aa bb",
                    "pnic": ["key-vim.host.PhysicalNic-vmnic0"],
                }
            ],
        ),
        "host-2": _retrieve_result("host-2", pnics=[], proxy_switches=[]),
    }
    conn = _FakeConnector(listing=listing, props_by_host=props)

    out = await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    # The host listing was mounted (Automation /vcenter path); the per-host
    # vmomi read routes through _post_vmomi_json with the spec-relative path.
    assert conn.mount_calls == ["/vcenter/host"]
    assert conn.get_calls[0][0] == "/api/vcenter/host"
    assert len(conn.post_calls) == 2
    assert all(
        path == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        for path, _ in conn.post_calls
    )
    # The per-host property read requests the two host-network config paths.
    spec = conn.post_calls[0][1]["specSet"][0]
    assert spec["propSet"][0]["pathSet"] == [
        "config.network.pnic",
        "config.network.proxySwitch",
    ]
    assert spec["objectSet"][0]["obj"] == {"type": "HostSystem", "value": "host-1"}

    hosts = out["hosts"]
    assert len(hosts) == 2
    h1 = hosts[0]
    assert h1["id"] == "host-1"
    assert h1["name"] == "esx-1"
    assert h1["pnics"] == [
        {
            "device": "vmnic0",
            "mac": "aa:bb:cc:00:00:00",
            "driver": "ixgbe",
            "link_up": True,
            "speed_mb": 10000,
            "duplex": True,
        },
        {
            "device": "vmnic1",
            "mac": "aa:bb:cc:00:00:01",
            "driver": "ixgbe",
            "link_up": False,
            "speed_mb": None,
            "duplex": None,
        },
    ]
    assert h1["proxy_switches"] == [
        {
            "key": "key-vim.host.HostProxySwitch-1",
            "dvs_name": "DVS-A",
            "dvs_uuid": "50 01 aa bb",
            "uplink_pnics": ["vmnic0"],
        }
    ]
    assert "read_note" not in h1
    assert hosts[1]["pnics"] == []
    assert hosts[1]["proxy_switches"] == []


@pytest.mark.asyncio
async def test_mounts_only_the_listing_via_mount_op_path() -> None:
    """Only /vcenter/host is mounted via mount_op_path.

    The per-host RetrievePropertiesEx read routes through the vmomi seam
    (:meth:`_post_vmomi_json`) -- one POST per host -- so mount_op_path is
    called exactly once (the listing), not per host (#2466).
    """
    listing = [{"host": f"host-{i}", "name": f"esx-{i}"} for i in range(3)]
    props = {f"host-{i}": _retrieve_result(f"host-{i}", [], []) for i in range(3)}
    conn = _FakeConnector(listing=listing, props_by_host=props)

    await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    assert conn.mount_calls == ["/vcenter/host"]
    assert len(conn.post_calls) == 3


@pytest.mark.asyncio
async def test_filter_hosts_bare_on_modern_mount() -> None:
    """On the modern ``/api`` mount the host filter is sent bare (#2298)."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", [], [])},
        mount_prefix="/api",
    )

    await host_network_uplinks_impl(
        conn, _make_operator(), _Target(), {"filter_hosts": ["host-9", "host-10"]}
    )

    assert conn.get_calls[0][1] == {"hosts": ["host-9", "host-10"]}


@pytest.mark.asyncio
async def test_filter_hosts_prefixed_on_legacy_mount() -> None:
    """On the legacy/vcsim ``/rest`` mount the filter keeps the prefix (#2298)."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", [], [])},
        mount_prefix="/rest",
    )

    await host_network_uplinks_impl(
        conn, _make_operator(), _Target(), {"filter_hosts": ["host-9", "host-10"]}
    )

    assert conn.get_calls[0][1] == {"filter.hosts": ["host-9", "host-10"]}


@pytest.mark.asyncio
async def test_no_filter_sends_no_query_params() -> None:
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={"host-1": _retrieve_result("host-1", [], [])},
    )

    await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    assert conn.get_calls[0][1] is None


@pytest.mark.asyncio
async def test_property_read_is_best_effort_on_error() -> None:
    """A failed per-host property read keeps the row; pnics/proxy_switches null + note."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        post_error=httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("POST", "https://vc/api"),
            response=httpx.Response(400),
        ),
    )

    out = await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    (row,) = out["hosts"]
    assert row["id"] == "host-1"
    assert row["pnics"] is None
    assert row["proxy_switches"] is None
    assert "read_note" in row
    assert "RetrievePropertiesEx" in row["read_note"]


@pytest.mark.asyncio
async def test_tolerates_legacy_value_envelope_on_listing_and_props() -> None:
    conn = _FakeConnector(
        listing={"value": [{"host": "host-1", "name": "esx-1"}]},
        props_by_host={"host-1": {"value": _retrieve_result("host-1", [{"device": "vmnic0"}], [])}},
    )

    out = await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    assert out["hosts"][0]["pnics"] == [
        {
            "device": "vmnic0",
            "mac": None,
            "driver": None,
            "link_up": False,
            "speed_mb": None,
            "duplex": None,
        }
    ]


@pytest.mark.asyncio
async def test_skips_malformed_listing_entries() -> None:
    listing = [
        "not-a-dict",
        {"name": "no-moid"},  # missing ``host`` key
        {"host": "host-3", "name": "esx-3"},
    ]
    conn = _FakeConnector(
        listing=listing,
        props_by_host={"host-3": _retrieve_result("host-3", [], [])},
    )

    out = await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    assert [r["id"] for r in out["hosts"]] == ["host-3"]
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_raises_on_non_list_listing() -> None:
    conn = _FakeConnector(listing={"unexpected": "shape"})

    with pytest.raises(RuntimeError, match="expected a list of hosts"):
        await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})


@pytest.mark.asyncio
async def test_proxy_switch_pnic_keys_map_to_device_names() -> None:
    """Uplink pnic WS-API keys are surfaced as bare device names."""
    conn = _FakeConnector(
        listing=[{"host": "host-1", "name": "esx-1"}],
        props_by_host={
            "host-1": _retrieve_result(
                "host-1",
                pnics=[],
                proxy_switches=[
                    {
                        "key": "k",
                        "dvsName": "DVS",
                        "dvsUuid": "u",
                        "pnic": [
                            "key-vim.host.PhysicalNic-vmnic2",
                            "vmnic3",  # already a bare name
                            42,  # non-str -> skipped
                        ],
                    }
                ],
            )
        },
    )

    out = await host_network_uplinks_impl(conn, _make_operator(), _Target(), {})

    assert out["hosts"][0]["proxy_switches"][0]["uplink_pnics"] == ["vmnic2", "vmnic3"]


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_op() -> None:
    assert VMWARE_HOST_NETWORK_UPLINKS_OP in VMWARE_TYPED_OPS
    assert VMWARE_HOST_NETWORK_UPLINKS_OP.op_id == "vmware.host.network_uplinks"
    assert VMWARE_HOST_NETWORK_UPLINKS_OP.safety_level == "safe"
    assert VMWARE_HOST_NETWORK_UPLINKS_OP.requires_approval is False
    assert VMWARE_HOST_NETWORK_UPLINKS_OP.group_key == HOST_NETWORK_UPLINKS_GROUP_KEY


def test_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_HOST_NETWORK_UPLINKS_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_handler_signature_has_no_dispatch_child() -> None:
    """Typed handler must NOT accept dispatch_child (else it'd be a composite)."""
    import inspect

    params = inspect.signature(VmwareRestConnector.host_network_uplinks).parameters
    assert "dispatch_child" not in params
    assert "operator" in params
