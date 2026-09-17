# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.object.collect`` typed op (#2300).

``vmware.object.collect`` is a ``source_kind="typed"`` bounded generic
PropertyCollector read: given a ``(type, moid)`` and a caller-specified
property-path list, it returns those properties as typed rows, reading
directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a fresh boot with zero catalog ingest.

Two assertion targets: (1) the call-shape + parse contract via a fake
connector, and (2) the declarative size / shape bound enforced through
``parameter_schema`` -- oversized / malformed requests fail
``validate_params`` (the dispatcher's ``invalid_params`` gate) before any
read is issued.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.soap import parse_retrieve_result
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_object_collect import (
    VMWARE_OBJECT_COLLECT_OP,
    build_object_collect_retrieve_params,
    object_collect_impl,
)
from meho_backplane.operations._validate import validate_params


def _make_operator() -> Operator:
    return Operator(
        sub="op-object-collect",
        name="Object Collect Test",
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
    def __init__(self, *, props_result: Any, mount_prefix: str = "/api") -> None:
        self._props_result = props_result
        self._mount_prefix = mount_prefix
        self.mount_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.promote_missing_calls: list[bool] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        # vmomi RetrievePropertiesEx read via the vmomi seam; the handler
        # passes the spec-relative path (the /sdk/vim25 mount is the
        # connector's job, #2466).
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
        self.promote_missing_calls.append(promote_managed_object_not_found)
        return self._props_result


def _object_content(mo_type: str, moid: str, props: dict[str, Any], missing: list[str]) -> dict:
    return {
        "objects": [
            {
                "obj": {"type": mo_type, "value": moid},
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
                "missingSet": [{"path": p} for p in missing],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Builder — single object, no traversal
# ---------------------------------------------------------------------------


def test_build_params_is_single_object_no_traversal() -> None:
    body = build_object_collect_retrieve_params(
        "Datastore", "datastore-5", ["summary.freeSpace", "summary.capacity"]
    )
    (spec,) = body["specSet"]
    (prop_spec,) = spec["propSet"]
    assert prop_spec == {
        "_typeName": "PropertySpec",
        "type": "Datastore",
        "pathSet": ["summary.freeSpace", "summary.capacity"],
    }
    (obj_spec,) = spec["objectSet"]
    assert obj_spec == {
        "_typeName": "ObjectSpec",
        "obj": {
            "_typeName": "ManagedObjectReference",
            "type": "Datastore",
            "value": "datastore-5",
        },
    }
    # No traversal spec / selectSet anywhere -> cannot walk the inventory.
    assert "selectSet" not in obj_spec


# ---------------------------------------------------------------------------
# object_collect_impl — parse, across at least two MO types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_collect_reads_datastore_properties() -> None:
    """Boxed primitive vals (#3106): live VI-JSON boxes ``Any``-placeholder longs."""
    conn = _FakeConnector(
        props_result=_object_content(
            "Datastore",
            "datastore-5",
            {
                "summary.freeSpace": {"_typeName": "long", "_value": 1073741824},
                "summary.capacity": {"_typeName": "long", "_value": 5368709120},
            },
            [],
        )
    )

    out = await object_collect_impl(
        conn,
        _make_operator(),
        _Target(),
        {"type": "Datastore", "moid": "datastore-5", "properties": ["summary.freeSpace"]},
    )
    assert conn.promote_missing_calls == [True]

    # The vmomi read routes through _post_vmomi_json (not mount_op_path);
    # the handler addresses it by the spec-relative path (#2466).
    assert conn.mount_calls == []
    assert conn.post_calls[0][0] == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    assert out["type"] == "Datastore"
    assert out["moid"] == "datastore-5"
    assert out["properties"]["summary.freeSpace"] == 1073741824
    assert out["missing"] == []


@pytest.mark.asyncio
async def test_object_collect_reads_resourcepool_properties_and_missing() -> None:
    """Second MO type + a missingSet path the collector could not read."""
    conn = _FakeConnector(
        props_result=_object_content(
            "ResourcePool",
            "resgroup-8",
            {"runtime.memory.overallUsage": 2048},
            ["config.entity"],
        )
    )

    out = await object_collect_impl(
        conn,
        _make_operator(),
        _Target(),
        {
            "type": "ResourcePool",
            "moid": "resgroup-8",
            "properties": ["runtime.memory.overallUsage", "config.entity"],
        },
    )

    assert out["type"] == "ResourcePool"
    assert out["properties"]["runtime.memory.overallUsage"] == 2048
    assert out["missing"] == ["config.entity"]


# ---------------------------------------------------------------------------
# missingSet fault types — surface type names + counts, never fault text (#3708)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_collect_surfaces_missingset_fault_types_never_text() -> None:
    """A recorded RetrievePropertiesEx answer whose objects carry propSet
    entries AND missingSet entries with mixed fault types (one fault bearing
    a message string and a nested payload) surfaces the fault *type names* and
    counts, leaks no fault text/payload/property values, keeps every existing
    field unchanged, and yields an empty summary for an empty missingSet."""
    # Realistic VI-JSON RetrieveResult (9.x LocalizedMethodFault wrapper shape,
    # ``_typeName``-annotated as on the wire): one readable boxed property, two
    # NoPermission faults + one InvalidProperty fault, and one genuinely-unset
    # property (no ``fault``). The InvalidProperty fault carries a message
    # string and a nested faultMessage payload; the NoPermission faults carry
    # a localized message and a nested privilege payload — none of which may
    # appear in the returned envelope.
    recorded = {
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": {
                    "_typeName": "ManagedObjectReference",
                    "type": "HostSystem",
                    "value": "host-42",
                },
                "propSet": [
                    {
                        "_typeName": "DynamicProperty",
                        "name": "summary.overallStatus",
                        "val": {"_typeName": "ManagedEntityStatus", "_value": "green"},
                    }
                ],
                "missingSet": [
                    {
                        "_typeName": "MissingProperty",
                        "path": "config.storageDevice",
                        "fault": {
                            "_typeName": "LocalizedMethodFault",
                            "fault": {
                                "_typeName": "NoPermission",
                                "object": {
                                    "_typeName": "ManagedObjectReference",
                                    "type": "HostSystem",
                                    "value": "host-42",
                                },
                                "privilegeId": "System.Read",
                            },
                            "localizedMessage": "Permission to perform this operation was denied.",
                        },
                    },
                    {
                        "_typeName": "MissingProperty",
                        "path": "config.network.dnsConfig",
                        "fault": {
                            "_typeName": "LocalizedMethodFault",
                            "fault": {
                                "_typeName": "NoPermission",
                                "privilegeId": "Host.Config.Network",
                            },
                            "localizedMessage": "Permission to perform this operation was denied.",
                        },
                    },
                    {
                        "_typeName": "MissingProperty",
                        "path": "hardware.systemInfo.serialNumber",
                        "fault": {
                            "_typeName": "LocalizedMethodFault",
                            "fault": {
                                "_typeName": "InvalidProperty",
                                "name": "hardware.systemInfo.serialNumber",
                                "faultMessage": [
                                    {
                                        "_typeName": "LocalizableMessage",
                                        "key": "com.vmware.vim.invalidProperty",
                                        "message": "Invalid property: SECRET-SERIAL-9931",
                                    }
                                ],
                            },
                            "localizedMessage": "An invalid property was specified.",
                        },
                    },
                    # No ``fault`` — property genuinely unset, not faulted.
                    {"_typeName": "MissingProperty", "path": "config.product.build"},
                ],
            }
        ]
    }
    conn = _FakeConnector(props_result=recorded)

    out = await object_collect_impl(
        conn,
        _make_operator(),
        _Target(),
        {
            "type": "HostSystem",
            "moid": "host-42",
            "properties": [
                "summary.overallStatus",
                "config.storageDevice",
                "config.network.dnsConfig",
                "hardware.systemInfo.serialNumber",
                "config.product.build",
            ],
        },
    )

    # Fault type names + counts surfaced correctly.
    assert out["missing_fault_summary"] == {"NoPermission": 2, "InvalidProperty": 1}
    assert out["missing_properties"] == [
        {"path": "config.storageDevice", "fault_type": "NoPermission"},
        {"path": "config.network.dnsConfig", "fault_type": "NoPermission"},
        {"path": "hardware.systemInfo.serialNumber", "fault_type": "InvalidProperty"},
        # Genuinely-unset property: reported with no fault_type ("absent" vs "faulted").
        {"path": "config.product.build"},
    ]

    # Existing fields are byte-identical to the pre-#3708 shape.
    assert out["type"] == "HostSystem"
    assert out["moid"] == "host-42"
    assert out["properties"] == {"summary.overallStatus": "green"}
    assert out["missing"] == [
        "config.storageDevice",
        "config.network.dnsConfig",
        "hardware.systemInfo.serialNumber",
        "config.product.build",
    ]

    # No fault message text, no fault payload field, no fault payload value
    # anywhere in the returned envelope — names and counts only (#3708).
    serialized = json.dumps(out)
    for forbidden in (
        "SECRET-SERIAL-9931",
        "System.Read",
        "Host.Config.Network",
        "Permission to perform this operation was denied.",
        "An invalid property was specified.",
        "privilegeId",
        "faultMessage",
        "localizedMessage",
        "faultstring",
        "LocalizableMessage",
    ):
        assert forbidden not in serialized, f"fault content {forbidden!r} leaked into the envelope"

    # An answer with an empty missingSet yields an empty summary / no entries.
    empty = _FakeConnector(
        props_result=_object_content("VirtualMachine", "vm-9", {"name": "web01"}, [])
    )
    out_empty = await object_collect_impl(
        empty,
        _make_operator(),
        _Target(),
        {"type": "VirtualMachine", "moid": "vm-9", "properties": ["name"]},
    )
    assert out_empty["missing"] == []
    assert out_empty["missing_properties"] == []
    assert out_empty["missing_fault_summary"] == {}


# A recorded standalone-ESXi RetrievePropertiesEx SOAP response with exactly
# ONE object carrying exactly ONE unreadable property. The single ``missingSet``
# element is the case the codec force-list (#3708) repairs: without it the codec
# parses ``missingSet`` to a bare dict and object.collect's ``isinstance(miss,
# dict)`` guard silently drops the lone missing property. The per-property fault
# is a ``LocalizedMethodFault`` wrapping a concrete ``NoPermission`` (xsi:type),
# carrying a localized message + a privilege payload that must NOT leak.
_SOAP_SINGLE_MISSING_RETRIEVE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Body>
    <RetrievePropertiesExResponse xmlns="urn:vim25">
      <returnval>
        <objects>
          <obj type="HostSystem">ha-host</obj>
          <propSet>
            <name>summary.overallStatus</name>
            <val xsi:type="ManagedEntityStatus">green</val>
          </propSet>
          <missingSet>
            <path>config.storageDevice</path>
            <fault xsi:type="LocalizedMethodFault">
              <fault xsi:type="NoPermission">
                <object type="HostSystem">ha-host</object>
                <privilegeId>Host.Config.Storage</privilegeId>
              </fault>
              <localizedMessage>Permission to perform this operation was denied.</localizedMessage>
            </fault>
          </missingSet>
        </objects>
      </returnval>
    </RetrievePropertiesExResponse>
  </soapenv:Body>
</soapenv:Envelope>"""


@pytest.mark.asyncio
async def test_object_collect_surfaces_single_soap_missing_fault_type() -> None:
    """The standalone-ESXi SOAP path: a lone missing property (one ``missingSet``
    element) is force-listed by the codec (#3708) so object.collect surfaces its
    fault type name — not silently dropped — and no fault text/payload leaks."""
    parsed = parse_retrieve_result(_SOAP_SINGLE_MISSING_RETRIEVE)

    # The codec force-list keeps a single missingSet element list-shaped; without
    # it this is a bare dict and the object.collect loop drops the property.
    missing_set = parsed["objects"][0]["missingSet"]
    assert isinstance(missing_set, list)
    assert len(missing_set) == 1

    # The SOAP transport returns the same parsed dict object.collect consumes.
    conn = _FakeConnector(props_result=parsed)
    out = await object_collect_impl(
        conn,
        _make_operator(),
        _Target(),
        {"type": "HostSystem", "moid": "ha-host", "properties": ["config.storageDevice"]},
    )

    assert out["missing"] == ["config.storageDevice"]
    assert out["missing_properties"] == [
        {"path": "config.storageDevice", "fault_type": "NoPermission"}
    ]
    assert out["missing_fault_summary"] == {"NoPermission": 1}
    assert out["properties"] == {"summary.overallStatus": "green"}

    serialized = json.dumps(out)
    for forbidden in (
        "Host.Config.Storage",
        "Permission to perform this operation was denied.",
        "privilegeId",
        "localizedMessage",
    ):
        assert forbidden not in serialized, f"fault content {forbidden!r} leaked into the envelope"


# ---------------------------------------------------------------------------
# The bound — oversized / malformed requests are structured invalid_params
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    return VMWARE_OBJECT_COLLECT_OP.parameter_schema


def test_schema_accepts_a_reasonable_request() -> None:
    assert (
        validate_params(
            _schema(),
            {"type": "VirtualMachine", "moid": "vm-1", "properties": ["runtime.powerState"]},
        )
        == []
    )


def test_schema_rejects_too_many_properties() -> None:
    errors = validate_params(
        _schema(),
        {"type": "VirtualMachine", "moid": "vm-1", "properties": [f"p{i}" for i in range(65)]},
    )
    assert errors
    assert any(e["validator"] == "maxItems" for e in errors)


def test_schema_rejects_empty_property_list() -> None:
    errors = validate_params(
        _schema(), {"type": "VirtualMachine", "moid": "vm-1", "properties": []}
    )
    assert errors


def test_schema_rejects_wildcard_and_index_paths() -> None:
    for bad in ["*", "config.hardware.device[0]", "guest.net.*"]:
        errors = validate_params(
            _schema(), {"type": "VirtualMachine", "moid": "vm-1", "properties": [bad]}
        )
        assert errors, f"expected {bad!r} to be rejected"


def test_schema_rejects_pathological_depth() -> None:
    deep = ".".join(f"seg{i}" for i in range(20))  # 20 segments > 16 cap
    errors = validate_params(
        _schema(), {"type": "VirtualMachine", "moid": "vm-1", "properties": [deep]}
    )
    assert errors


def test_schema_rejects_traversal_field_and_additional_props() -> None:
    errors = validate_params(
        _schema(),
        {
            "type": "VirtualMachine",
            "moid": "vm-1",
            "properties": ["runtime"],
            "objectSet": [{}],
        },
    )
    assert errors
    assert any(e["validator"] == "additionalProperties" for e in errors)


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_object_collect_op_is_a_registered_typed_op() -> None:
    assert VMWARE_OBJECT_COLLECT_OP in VMWARE_TYPED_OPS
    assert VMWARE_OBJECT_COLLECT_OP.op_id == "vmware.object.collect"
    assert VMWARE_OBJECT_COLLECT_OP.safety_level == "safe"
    assert VMWARE_OBJECT_COLLECT_OP.requires_approval is False


def test_object_collect_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_OBJECT_COLLECT_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_object_collect_group_has_non_empty_when_to_use() -> None:
    group_key = VMWARE_OBJECT_COLLECT_OP.group_key
    assert group_key is not None
    blurb = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(group_key)
    assert isinstance(blurb, str)
    assert blurb.strip()
