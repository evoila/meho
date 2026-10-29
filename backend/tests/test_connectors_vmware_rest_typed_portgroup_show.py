# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.network.portgroup.show`` typed read.

* :func:`portgroup_show_impl` against a recording fake connector with saved
  VI-JSON answers: the full row (VLAN, security, teaming, traffic shaping),
  the trunk, private VLAN and missing VLAN shapes, the lookup by name on the
  ``/api`` and the legacy ``/rest`` mount, an unknown or ambiguous name, an
  unknown port group and an unreadable ``config`` property;
* the real connector over mocked HTTP: both reads land on the VI-JSON path of
  the target's release, on vCenter 9.0 and 8.0;
* the operation metadata: ``safe``, runs at once, in its own group.
"""

from __future__ import annotations

import copy
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
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_portgroup_show import (
    VMWARE_NETWORK_PORTGROUP_SHOW_OP,
    portgroup_show_impl,
)
from meho_backplane.operations._validate import validate_params

_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _operator() -> Operator:
    return Operator(
        sub="op-portgroup-show",
        name="Portgroup Show Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _bool(value: bool, *, inherited: bool = False) -> dict[str, Any]:
    return {"_typeName": "BoolPolicy", "inherited": inherited, "value": value}


def _long(value: int) -> dict[str, Any]:
    return {"_typeName": "LongPolicy", "inherited": False, "value": value}


def _shaping(enabled: bool, *, inherited: bool = True) -> dict[str, Any]:
    return {
        "_typeName": "DVSTrafficShapingPolicy",
        "inherited": inherited,
        "enabled": _bool(enabled, inherited=inherited),
        "averageBandwidth": _long(100000000),
        "peakBandwidth": _long(200000000),
        "burstSize": _long(104857600),
    }


#: A saved ``DVPortgroupConfigInfo`` as VI-JSON returns it: access VLAN 120.
_CONFIG: dict[str, Any] = {
    "_typeName": "DVPortgroupConfigInfo",
    "key": "dvportgroup-42",
    "name": "pg-app",
    "numPorts": 8,
    "distributedVirtualSwitch": {
        "_typeName": "ManagedObjectReference",
        "type": "VmwareDistributedVirtualSwitch",
        "value": "dvs-21",
    },
    "defaultPortConfig": {
        "_typeName": "VMwareDVSPortSetting",
        "blocked": _bool(False, inherited=True),
        "inShapingPolicy": _shaping(False),
        "outShapingPolicy": _shaping(True, inherited=False),
        "vlan": {
            "_typeName": "VmwareDistributedVirtualSwitchVlanIdSpec",
            "inherited": False,
            "vlanId": 120,
        },
        "uplinkTeamingPolicy": {
            "_typeName": "VmwareUplinkPortTeamingPolicy",
            "inherited": False,
            "policy": {
                "_typeName": "StringPolicy",
                "inherited": False,
                "value": "loadbalance_srcid",
            },
            "reversePolicy": _bool(True),
            "notifySwitches": _bool(True),
            "rollingOrder": _bool(False),
            "failureCriteria": {
                "_typeName": "DVSFailureCriteria",
                "inherited": True,
                "checkBeacon": _bool(False, inherited=True),
            },
            "uplinkPortOrder": {
                "_typeName": "VMwareUplinkPortOrderPolicy",
                "inherited": False,
                "activeUplinkPort": ["uplink1"],
                "standbyUplinkPort": ["uplink2"],
            },
        },
        "securityPolicy": {
            "_typeName": "DVSSecurityPolicy",
            "inherited": False,
            "allowPromiscuous": _bool(False),
            "macChanges": _bool(True),
            "forgedTransmits": _bool(True),
        },
    },
    "description": "app tier",
    "type": "earlyBinding",
    "policy": {"_typeName": "VMwareDVSPortgroupPolicy"},
    "configVersion": "7",
    "autoExpand": True,
    "uplink": False,
}

_EXPECTED_ROW: dict[str, Any] = {
    "portgroup": "dvportgroup-42",
    "name": "pg-app",
    "key": "dvportgroup-42",
    "description": "app tier",
    "dvs": "dvs-21",
    "dvs_name": "dvs-prod",
    "port_binding": "earlyBinding",
    "num_ports": 8,
    "auto_expand": True,
    "uplink_portgroup": False,
    "config_version": "7",
    "vlan": {"mode": "access", "vlan_id": 120},
    "security": {
        "allow_promiscuous": False,
        "mac_changes": True,
        "forged_transmits": True,
        "inherited": False,
    },
    "teaming": {
        "load_balancing": "loadbalance_srcid",
        "active_uplinks": ["uplink1"],
        "standby_uplinks": ["uplink2"],
        "failback": True,
        "notify_switches": True,
        "beacon_probing": False,
        "inherited": False,
    },
    "traffic_shaping": {
        "ingress": {
            "enabled": False,
            "average_bandwidth_bps": 100000000,
            "peak_bandwidth_bps": 200000000,
            "burst_size_bytes": 104857600,
            "inherited": True,
        },
        "egress": {
            "enabled": True,
            "average_bandwidth_bps": 100000000,
            "peak_bandwidth_bps": 200000000,
            "burst_size_bytes": 104857600,
            "inherited": False,
        },
    },
    "blocked": False,
}


def _answer(
    mo_type: str, moid: str, props: dict[str, Any], *, missing: list[str] | None = None
) -> dict[str, Any]:
    """A ``RetrieveResult`` for one object."""
    obj: dict[str, Any] = {
        "_typeName": "ObjectContent",
        "obj": {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid},
        "propSet": [
            {"_typeName": "DynamicProperty", "name": name, "val": val}
            for name, val in props.items()
        ],
    }
    if missing:
        obj["missingSet"] = [
            {
                "_typeName": "MissingProperty",
                "path": path,
                "fault": {
                    "_typeName": "LocalizedMethodFault",
                    "fault": {"_typeName": "NoPermission"},
                },
            }
            for path in missing
        ]
    return {"_typeName": "RetrieveResult", "objects": [obj]}


def _switch_answer() -> dict[str, Any]:
    # A primitive in DynamicProperty.val arrives boxed on vCenter 8.0.x.
    return _answer(
        "VmwareDistributedVirtualSwitch",
        "dvs-21",
        {"name": {"_typeName": "string", "_value": "dvs-prod"}},
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


class _FakeConnector:
    """Records the calls and answers them from saved answers."""

    def __init__(
        self,
        config_answer: Any,
        *,
        listing: Any = None,
        mount_prefix: str = "/api",
        error: Exception | None = None,
    ) -> None:
        self._config_answer = config_answer
        self._listing = listing
        self._mount_prefix = mount_prefix
        self._error = error
        self.vim_calls: list[dict[str, Any]] = []
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: dict[str, Any] | None = None
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
        json: dict[str, Any],
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        del target, operator
        assert path == _RETRIEVE_PATH
        self.vim_calls.append({"json": json, "promote": promote_managed_object_not_found})
        obj = json["specSet"][0]["objectSet"][0]["obj"]
        if obj["type"] == "DistributedVirtualPortgroup":
            if self._error is not None:
                raise self._error
            return self._config_answer
        return _switch_answer()


def _config_answer(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return _answer("DistributedVirtualPortgroup", "dvportgroup-42", {"config": config or _CONFIG})


async def _show(conn: _FakeConnector, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await portgroup_show_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        params or {"portgroup": "dvportgroup-42"},
    )


async def test_show_returns_every_setting_of_the_portgroup() -> None:
    conn = _FakeConnector(_config_answer())
    out = await _show(conn)
    assert out == _EXPECTED_ROW
    Draft202012Validator(VMWARE_NETWORK_PORTGROUP_SHOW_OP.response_schema).validate(out)


async def test_show_reads_the_whole_config_then_the_switch_name() -> None:
    conn = _FakeConnector(_config_answer())
    await _show(conn)
    first, second = conn.vim_calls
    spec = first["json"]["specSet"][0]
    assert spec["propSet"][0]["type"] == "DistributedVirtualPortgroup"
    assert spec["propSet"][0]["pathSet"] == ["config"]
    assert spec["objectSet"][0]["obj"]["value"] == "dvportgroup-42"
    assert first["promote"] is True
    switch_spec = second["json"]["specSet"][0]
    assert switch_spec["propSet"][0] == {
        "_typeName": "PropertySpec",
        "type": "VmwareDistributedVirtualSwitch",
        "pathSet": ["name"],
    }
    assert conn.get_calls == []


async def test_trunk_vlan_ranges() -> None:
    config = copy.deepcopy(_CONFIG)
    config["defaultPortConfig"]["vlan"] = {
        "_typeName": "VmwareDistributedVirtualSwitchTrunkVlanSpec",
        "inherited": False,
        "vlanId": [
            {"_typeName": "NumericRange", "start": 0, "end": 0},
            {"_typeName": "NumericRange", "start": 100, "end": 199},
        ],
    }
    out = await _show(_FakeConnector(_config_answer(config)))
    assert out["vlan"] == {
        "mode": "trunk",
        "ranges": [{"start": 0, "end": 0}, {"start": 100, "end": 199}],
    }
    Draft202012Validator(VMWARE_NETWORK_PORTGROUP_SHOW_OP.response_schema).validate(out)


async def test_private_vlan() -> None:
    config = copy.deepcopy(_CONFIG)
    config["defaultPortConfig"]["vlan"] = {
        "_typeName": "VmwareDistributedVirtualSwitchPvlanSpec",
        "inherited": False,
        "pvlanId": 301,
    }
    out = await _show(_FakeConnector(_config_answer(config)))
    assert out["vlan"] == {"mode": "pvlan", "pvlan_id": 301}


async def test_failback_is_the_inverse_of_rolling_order() -> None:
    config = copy.deepcopy(_CONFIG)
    config["defaultPortConfig"]["uplinkTeamingPolicy"]["rollingOrder"] = _bool(True)
    out = await _show(_FakeConnector(_config_answer(config)))
    assert out["teaming"]["failback"] is False


async def test_sparse_config_gives_nulls_not_errors() -> None:
    config = {"_typeName": "DVPortgroupConfigInfo", "key": "dvportgroup-42", "name": "pg-bare"}
    out = await _show(_FakeConnector(_config_answer(config)))
    assert out["name"] == "pg-bare"
    assert out["vlan"] is None
    assert out["dvs"] is None
    assert out["dvs_name"] is None
    assert out["teaming"]["active_uplinks"] == []
    assert out["security"]["allow_promiscuous"] is None
    Draft202012Validator(VMWARE_NETWORK_PORTGROUP_SHOW_OP.response_schema).validate(out)


async def test_by_name_finds_the_id_on_the_api_mount() -> None:
    listing = [{"network": "dvportgroup-42", "name": "pg-app", "type": "DISTRIBUTED_PORTGROUP"}]
    conn = _FakeConnector(_config_answer(), listing=listing)
    out = await _show(conn, {"name": "pg-app"})
    assert out["portgroup"] == "dvportgroup-42"
    assert conn.get_calls == [
        ("/api/vcenter/network", {"types": ["DISTRIBUTED_PORTGROUP"], "names": ["pg-app"]})
    ]


async def test_by_name_uses_the_prefixed_filter_on_the_legacy_rest_mount() -> None:
    listing = {"value": [{"network": "dvportgroup-42", "name": "pg-app"}]}
    conn = _FakeConnector(_config_answer(), listing=listing, mount_prefix="/rest")
    await _show(conn, {"name": "pg-app"})
    assert conn.get_calls == [
        (
            "/rest/vcenter/network",
            {"filter.types": ["DISTRIBUTED_PORTGROUP"], "filter.names": ["pg-app"]},
        )
    ]


async def test_unknown_name_is_not_found() -> None:
    conn = _FakeConnector(_config_answer(), listing=[])
    with pytest.raises(ConnectorResourceNotFoundError):
        await _show(conn, {"name": "pg-missing"})
    assert conn.vim_calls == []


async def test_ambiguous_name_is_an_error_naming_the_ids() -> None:
    listing = [
        {"network": "dvportgroup-42", "name": "pg-app"},
        {"network": "dvportgroup-77", "name": "pg-app"},
    ]
    conn = _FakeConnector(_config_answer(), listing=listing)
    with pytest.raises(RuntimeError, match="dvportgroup-42, dvportgroup-77"):
        await _show(conn, {"name": "pg-app"})


async def test_unknown_portgroup_is_not_found() -> None:
    conn = _FakeConnector(None, error=ConnectorResourceNotFoundError(["dvportgroup-9"], "gone"))
    with pytest.raises(ConnectorResourceNotFoundError):
        await _show(conn, {"portgroup": "dvportgroup-9"})


async def test_portgroup_missing_from_the_answer_is_not_found() -> None:
    conn = _FakeConnector({"_typeName": "RetrieveResult", "objects": []})
    with pytest.raises(ConnectorResourceNotFoundError):
        await _show(conn)


async def test_unreadable_config_is_an_error_not_empty_settings() -> None:
    answer = _answer("DistributedVirtualPortgroup", "dvportgroup-42", {}, missing=["config"])
    with pytest.raises(RuntimeError, match="could not read the settings"):
        await _show(_FakeConnector(answer))


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
async def test_show_rides_the_vijson_path_of_the_release(version: str, release: str) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": version})
            read = mock.post(f"/sdk/vim25/{release}{_RETRIEVE_PATH}").mock(
                side_effect=[
                    respx.MockResponse(200, json=_config_answer()),
                    respx.MockResponse(200, json=_switch_answer()),
                ]
            )
            out = await connector.network_portgroup_show(
                _operator(), _StubTarget(), {"portgroup": "dvportgroup-42"}
            )
        assert read.call_count == 2
        assert out == _EXPECTED_ROW
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_that_runs_at_once() -> None:
    op = VMWARE_NETWORK_PORTGROUP_SHOW_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.network.portgroup.show"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_exactly_one_of_portgroup_or_name() -> None:
    schema = VMWARE_NETWORK_PORTGROUP_SHOW_OP.parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
    assert validate_params(schema, {"portgroup": "dvportgroup-42"}) == []
    assert validate_params(schema, {"name": "pg-app"}) == []
    assert validate_params(schema, {}) != []
    assert validate_params(schema, {"portgroup": "dvportgroup-42", "name": "pg-app"}) != []
    assert validate_params(schema, {"portgroup": "dvportgroup-42", "dvs": "x"}) != []
