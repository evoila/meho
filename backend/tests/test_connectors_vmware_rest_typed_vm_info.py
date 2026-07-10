# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.vm.info`` typed op (#2300).

``vmware.vm.info`` is a ``source_kind="typed"`` incident-triage read: a
bound method on :class:`VmwareRestConnector` that reads one VM's live
guest / runtime / storage signals directly on the connector session (no
``dispatch_child``, no ingested descriptor), so it works on a fresh boot
with zero catalog ingest.

The handler logic is exercised against a fake connector that records
:meth:`mount_op_path` / :meth:`_get_json` / :meth:`_post_json` calls, so
the assertion targets are the call-shape contract (optional name->moid
resolve, then the single PropertyCollector read) and the parse contract,
without a live httpx transport. End-to-end transport + mount routing is
covered by the respx integration test in
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
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import (
    VMWARE_VM_INFO_OP,
    build_vm_info_retrieve_params,
    vm_info_impl,
)
from meho_backplane.operations._validate import validate_params


def _make_operator() -> Operator:
    return Operator(
        sub="op-vm-info",
        name="VM Info Test",
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
    """Records the transport calls ``vm_info_impl`` makes."""

    def __init__(
        self,
        *,
        listing: Any = None,
        props_result: Any = None,
        post_error: Exception | None = None,
        mount_prefix: str = "/api",
    ) -> None:
        self._listing = listing
        self._props_result = props_result
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
        return self._props_result


def _vm_retrieve_result(moid: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "VirtualMachine", "value": moid},
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
            }
        ]
    }


_POWERED_ON_NO_IP = {
    "name": "hung-appliance",
    "runtime.powerState": "poweredOn",
    "guestHeartbeatStatus": "red",
    "guest.toolsStatus": "toolsOk",
    "guest.toolsRunningStatus": "guestToolsRunning",
    # guest.ipAddress + guest.hostName deliberately absent -> the hung tell.
    "storage.perDatastoreUsage": [
        {
            "datastore": {"type": "Datastore", "value": "datastore-9"},
            "committed": 42949672960,
            "uncommitted": 10737418240,
            "unshared": 42949672960,
        }
    ],
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def test_build_retrieve_params_is_single_vm_property_filter() -> None:
    body = build_vm_info_retrieve_params("vm-42")
    assert set(body) == {"specSet", "options"}
    (spec,) = body["specSet"]
    (prop_spec,) = spec["propSet"]
    assert prop_spec["type"] == "VirtualMachine"
    assert "runtime.powerState" in prop_spec["pathSet"]
    assert "guest.ipAddress" in prop_spec["pathSet"]
    assert "guestHeartbeatStatus" in prop_spec["pathSet"]
    assert "storage.perDatastoreUsage" in prop_spec["pathSet"]
    (obj_spec,) = spec["objectSet"]
    assert obj_spec["obj"] == {"type": "VirtualMachine", "value": "vm-42"}


# ---------------------------------------------------------------------------
# vm_info_impl — call shape + parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm_info_by_moid_skips_listing_and_reads_props() -> None:
    conn = _FakeConnector(props_result=_vm_retrieve_result("vm-42", _POWERED_ON_NO_IP))

    out = await vm_info_impl(conn, _make_operator(), _Target(), {"vm": "vm-42"})

    # Addressed by moid: no name->moid listing GET was issued.
    assert conn.get_calls == []
    # Exactly one mounted PropertyCollector read.
    assert conn.mount_calls == ["/PropertyCollector/propertyCollector/RetrievePropertiesEx"]
    assert len(conn.post_calls) == 1
    assert conn.post_calls[0][0] == "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"

    assert out["vm"] == "vm-42"
    assert out["name"] == "hung-appliance"
    # The hung-appliance shape: poweredOn but no guest IP, red heartbeat.
    assert out["power_state"] == "poweredOn"
    assert out["guest_ip"] is None
    assert out["heartbeat_status"] == "red"
    assert out["tools_running_status"] == "guestToolsRunning"
    assert out["per_datastore_usage"] == [
        {
            "datastore": "datastore-9",
            "committed_bytes": 42949672960,
            "uncommitted_bytes": 10737418240,
            "unshared_bytes": 42949672960,
        }
    ]


@pytest.mark.asyncio
async def test_vm_info_by_name_resolves_then_reads() -> None:
    conn = _FakeConnector(
        listing=[{"vm": "vm-7", "name": "web-01"}],
        props_result=_vm_retrieve_result("vm-7", {"name": "web-01", "guest.ipAddress": "10.0.0.5"}),
    )

    out = await vm_info_impl(conn, _make_operator(), _Target(), {"name": "web-01"})

    # The listing GET forwarded the name as filter.names, mounted onto /api.
    assert conn.get_calls[0] == ("/api/vcenter/vm", {"filter.names": ["web-01"]})
    # Then the PropertyCollector read used the resolved moid.
    body = conn.post_calls[0][1]
    assert body["specSet"][0]["objectSet"][0]["obj"]["value"] == "vm-7"
    assert out["vm"] == "vm-7"
    assert out["guest_ip"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_vm_info_by_name_tolerates_legacy_value_envelope() -> None:
    conn = _FakeConnector(
        listing={"value": [{"vm": "vm-7", "name": "web-01"}]},
        props_result=_vm_retrieve_result("vm-7", {"name": "web-01"}),
    )

    out = await vm_info_impl(conn, _make_operator(), _Target(), {"name": "web-01"})

    assert out["vm"] == "vm-7"


@pytest.mark.asyncio
async def test_vm_info_by_name_unknown_raises() -> None:
    conn = _FakeConnector(listing=[])

    with pytest.raises(RuntimeError, match="no VM named 'ghost'"):
        await vm_info_impl(conn, _make_operator(), _Target(), {"name": "ghost"})


@pytest.mark.asyncio
async def test_vm_info_by_name_ambiguous_raises() -> None:
    conn = _FakeConnector(listing=[{"vm": "vm-1", "name": "dup"}, {"vm": "vm-2", "name": "dup"}])

    with pytest.raises(RuntimeError, match="ambiguous"):
        await vm_info_impl(conn, _make_operator(), _Target(), {"name": "dup"})


@pytest.mark.asyncio
async def test_vm_info_property_read_failure_propagates() -> None:
    """The single-object read is load-bearing — a failure is not swallowed."""
    conn = _FakeConnector(
        props_result=None,
        post_error=httpx.HTTPStatusError(
            "boom", request=httpx.Request("POST", "https://vc/api"), response=httpx.Response(500)
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await vm_info_impl(conn, _make_operator(), _Target(), {"vm": "vm-42"})


@pytest.mark.asyncio
async def test_vm_info_absent_properties_map_to_none_and_empty() -> None:
    conn = _FakeConnector(props_result=_vm_retrieve_result("vm-42", {}))

    out = await vm_info_impl(conn, _make_operator(), _Target(), {"vm": "vm-42"})

    assert out["power_state"] is None
    assert out["guest_ip"] is None
    assert out["heartbeat_status"] is None
    assert out["per_datastore_usage"] == []


@pytest.mark.asyncio
async def test_vm_info_coerces_numeric_string_usage_counters() -> None:
    conn = _FakeConnector(
        props_result=_vm_retrieve_result(
            "vm-42",
            {
                "storage.perDatastoreUsage": [
                    {"datastore": "datastore-3", "committed": "1024", "unshared": "512"}
                ]
            },
        )
    )

    out = await vm_info_impl(conn, _make_operator(), _Target(), {"vm": "vm-42"})

    (usage,) = out["per_datastore_usage"]
    assert usage["datastore"] == "datastore-3"
    assert usage["committed_bytes"] == 1024
    assert usage["uncommitted_bytes"] is None
    assert usage["unshared_bytes"] == 512


# ---------------------------------------------------------------------------
# Parameter-schema contract (oneOf vm|name)
# ---------------------------------------------------------------------------


def test_schema_accepts_exactly_one_of_vm_or_name() -> None:
    schema = VMWARE_VM_INFO_OP.parameter_schema
    assert validate_params(schema, {"vm": "vm-1"}) == []
    assert validate_params(schema, {"name": "web-01"}) == []


def test_schema_rejects_neither_and_both() -> None:
    schema = VMWARE_VM_INFO_OP.parameter_schema
    assert validate_params(schema, {}) != []
    assert validate_params(schema, {"vm": "vm-1", "name": "web-01"}) != []


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_vm_info_op_is_a_registered_typed_op() -> None:
    assert VMWARE_VM_INFO_OP in VMWARE_TYPED_OPS
    assert VMWARE_VM_INFO_OP.op_id == "vmware.vm.info"
    assert VMWARE_VM_INFO_OP.safety_level == "safe"
    assert VMWARE_VM_INFO_OP.requires_approval is False


def test_vm_info_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_VM_INFO_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_vm_info_group_has_non_empty_when_to_use() -> None:
    group_key = VMWARE_VM_INFO_OP.group_key
    assert group_key is not None
    blurb = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(group_key)
    assert isinstance(blurb, str)
    assert blurb.strip()


def test_vm_info_handler_signature_has_no_dispatch_child() -> None:
    import inspect

    params = inspect.signature(VmwareRestConnector.vm_info).parameters
    assert "dispatch_child" not in params
    assert "operator" in params
