# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check ``vm.migrate_cross_vcenter`` against the pinned VI-JSON files.

* An always-on test pins the child-op list, so a change to it is seen.
* The shelf tests read the pinned 9.0 ``vi-json.yaml`` and the 8.0 U1, U2 and
  U3 files: every method is a POST path there, every request field the
  handler sends is a property of its vendor type, and the enum values it
  sends exist. They skip when the spec shelf is not configured and run in CI.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _vm_cross_vcenter
from meho_backplane.connectors.vmware_rest.composites._vm_cross_vcenter_schemas import (
    VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA,
)
from tests._spec_shelf import require_shelf_spec

_FILES = [
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
]


def _post_paths(lines: list[str]) -> set[str]:
    """``POST:<path>`` for every path item with a POST (a line scan of the large file)."""
    found: set[str] = set()
    in_paths = False
    current: str | None = None
    for line in lines:
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_paths = line.rstrip() == "paths:"
            current = None
        elif in_paths and line.startswith("  ") and not line.startswith("   "):
            key = line.strip()
            current = key[:-1] if key.startswith("/") and key.endswith(":") else None
        elif current is not None and line.strip() == "post:":
            found.add(f"POST:{current}")
    return found


def _schema_block(lines: list[str], schema: str) -> list[str]:
    """The lines of one component schema (its key sits at 4-space indent)."""
    out: list[str] = []
    inside = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if inside and line.startswith("    ") and not line.startswith("     "):
            break
        if inside:
            out.append(line)
    return out


def _properties(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema (8-space keys under ``properties``)."""
    return {
        line.strip()[:-1]
        for line in _schema_block(lines, schema)
        if line.startswith("        ") and not line.startswith("         ") and line.endswith(":")
    }


def _enum_values(lines: list[str], schema: str) -> set[str]:
    values: set[str] = set()
    in_enum = False
    for line in _schema_block(lines, schema):
        stripped = line.strip()
        if stripped == "enum:":
            in_enum = True
        elif in_enum and stripped.startswith("- "):
            values.add(stripped[2:].strip().strip("'\""))
        elif in_enum:
            in_enum = False
    return values


def test_child_op_list_is_pinned() -> None:
    assert _vm_cross_vcenter._VIM_SUB_OPS_VM_MIGRATE_CROSS_VCENTER == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/VirtualMachine/{moId}/RelocateVM_Task",
    )


@pytest.fixture(scope="module", params=_FILES, ids=[name for _dir, name in _FILES])
def vi_json(request: pytest.FixtureRequest) -> list[str]:
    product_dir, filename = request.param
    return require_shelf_spec(product_dir, filename).read_text(encoding="utf-8").splitlines()


def test_every_method_is_a_post_path(vi_json: list[str]) -> None:
    assert set(_vm_cross_vcenter._VIM_SUB_OPS_VM_MIGRATE_CROSS_VCENTER) <= _post_paths(vi_json)


@pytest.mark.parametrize(
    ("schema", "fields"),
    [
        ("RelocateVMRequestType", {"spec", "priority"}),
        (
            "VirtualMachineRelocateSpec",
            {"service", "datastore", "pool", "folder", "host", "deviceChange"},
        ),
        ("ServiceLocator", {"instanceUuid", "url", "credential", "sslThumbprint"}),
        ("ServiceLocatorNamePassword", {"username", "password"}),
        ("VirtualDeviceConfigSpec", {"operation", "device"}),
        ("VirtualEthernetCardDistributedVirtualPortBackingInfo", {"port"}),
        ("DistributedVirtualSwitchPortConnection", {"switchUuid", "portgroupKey"}),
        ("VirtualEthernetCardNetworkBackingInfo", {"network"}),
        ("VirtualDeviceDeviceBackingInfo", {"deviceName"}),
        ("ServiceContent", {"about"}),
        ("AboutInfo", {"instanceUuid"}),
        ("DVPortgroupConfigInfo", {"distributedVirtualSwitch"}),
        ("TaskInfo", {"descriptionId", "state"}),
    ],
)
def test_every_request_field_is_a_vendor_property(
    vi_json: list[str], schema: str, fields: set[str]
) -> None:
    assert fields <= _properties(vi_json, schema)


def test_every_property_read_is_a_vendor_property(vi_json: list[str]) -> None:
    """The managed-object properties the handler reads (``GET /<Type>/{moId}/<prop>``)."""
    paths = {line.strip()[:-1] for line in vi_json if line.startswith("  /") and line.endswith(":")}
    for path in (
        "/VirtualMachine/{moId}/name",
        "/VirtualMachine/{moId}/config",
        "/VirtualMachine/{moId}/recentTask",
        "/Task/{moId}/info",
        "/DistributedVirtualPortgroup/{moId}/key",
        "/DistributedVirtualPortgroup/{moId}/config",
        "/VmwareDistributedVirtualSwitch/{moId}/uuid",
        "/Network/{moId}/name",
    ):
        assert path in paths, path


def test_the_enum_values_exist(vi_json: list[str]) -> None:
    priorities = set(VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA["properties"]["priority"]["enum"])
    assert priorities <= _enum_values(vi_json, "VirtualMachineMovePriority_enum")
    assert "edit" in _enum_values(vi_json, "VirtualDeviceConfigSpecOperation_enum")
    assert {"queued", "running"} <= _enum_values(vi_json, "TaskInfoState_enum")
