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

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
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
    ) -> Any:
        # vmomi RetrievePropertiesEx read via the vmomi seam; the handler
        # passes the spec-relative path (the /sdk/vim25 mount is the
        # connector's job, #2466).
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
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
    assert prop_spec == {"type": "Datastore", "pathSet": ["summary.freeSpace", "summary.capacity"]}
    (obj_spec,) = spec["objectSet"]
    assert obj_spec == {"obj": {"type": "Datastore", "value": "datastore-5"}}
    # No traversal spec / selectSet anywhere -> cannot walk the inventory.
    assert "selectSet" not in obj_spec


# ---------------------------------------------------------------------------
# object_collect_impl — parse, across at least two MO types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_collect_reads_datastore_properties() -> None:
    conn = _FakeConnector(
        props_result=_object_content(
            "Datastore",
            "datastore-5",
            {"summary.freeSpace": 1073741824, "summary.capacity": 5368709120},
            [],
        )
    )

    out = await object_collect_impl(
        conn,
        _make_operator(),
        _Target(),
        {"type": "Datastore", "moid": "datastore-5", "properties": ["summary.freeSpace"]},
    )

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
