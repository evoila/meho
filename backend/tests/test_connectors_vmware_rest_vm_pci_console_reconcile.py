# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the PCI passthrough and console operations against the pinned vendor files.

* An always-on test pins the vendor-call lists of the four changes, so a
  change to them is seen.
* The shelf tests read the pinned VI-JSON files of vCenter 9.0 and 8.0 U1, U2
  and U3: every VI-JSON call is a POST path there, and every data object MEHO
  sends (``_typeName``) is a schema there with the fields MEHO sets. They skip
  when the spec shelf is not configured and run in CI.

The screenshot file read uses vCenter's datastore file access
(``GET /folder/...``), which is in neither API file.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites import _vm_console, _vm_pci_device
from meho_backplane.connectors.vmware_rest.composites._vm_console_pci_schemas import (
    CONSOLE_KEYS,
)
from meho_backplane.connectors.vmware_rest.composites._vm_pci_backing import (
    add_spec,
    direct_backing,
    dynamic_backing,
)
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = (
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
)

_VIM_CALLS = (
    *_vm_pci_device._VIM_SUB_OPS_VM_PCI_DEVICE_ADD,
    *_vm_pci_device._VIM_SUB_OPS_VM_PCI_DEVICE_REMOVE,
    *_vm_console._VIM_SUB_OPS_VM_SCREENSHOT,
    *_vm_console._VIM_SUB_OPS_VM_KEYS_SEND,
)


def test_the_vendor_call_lists_are_the_expected_sets() -> None:
    assert _vm_pci_device.VM_PCI_DEVICE_GOVERNED_SUBOPS == {
        "vmware.composite.vm.pci_device.add": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/EnvironmentBrowser/{moId}/QueryConfigTarget",
            "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        ),
        "vmware.composite.vm.pci_device.remove": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        ),
    }
    assert _vm_console.VM_CONSOLE_GOVERNED_SUBOPS == {
        "vmware.composite.vm.screenshot": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/VirtualMachine/{moId}/CreateScreenshot_Task",
        ),
        "vmware.composite.vm.keys.send": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/VirtualMachine/{moId}/PutUsbScanCodes",
        ),
    }


@functools.cache
def _load(spec_path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    return loaded


def _fields(schemas: dict[str, Any], name: str) -> set[str]:
    """Every property of schema *name*, with the ones of its ``allOf`` bases."""
    out: set[str] = set()
    pending = [schemas[name]]
    while pending:
        schema = pending.pop()
        out |= set(schema.get("properties") or {})
        for part in schema.get("allOf") or []:
            ref = part.get("$ref")
            if isinstance(ref, str):
                pending.append(schemas[ref.rsplit("/", 1)[-1]])
            else:
                pending.append(part)
    return out


def _objects(value: Any) -> list[dict[str, Any]]:
    """Every dict with a ``_typeName`` inside *value*."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "_typeName" in value:
            found.append(value)
        for child in value.values():
            found += _objects(child)
    elif isinstance(value, list):
        for child in value:
            found += _objects(child)
    return found


def _sent_bodies() -> list[Any]:
    """The data objects the four changes send, built by the real helpers."""
    config_target = {
        "pciPassthrough": [
            {
                "systemId": "<system-id>",
                "pciDevice": {"id": "0000:00:00.0", "deviceId": 1, "vendorId": 1},
            }
        ],
        "dynamicPassthrough": [{"vendorId": 1, "deviceId": 2, "customLabel": "x"}],
    }
    direct = direct_backing("0000:00:00.0", config_target)
    dynamic = dynamic_backing({"vendor_id": 1, "device_id": 2, "custom_label": "x"}, config_target)
    assert isinstance(direct, dict)
    assert isinstance(dynamic, dict)
    keys = {
        "_typeName": "UsbScanCodeSpec",
        "keyEvents": _vm_console._key_events(list(CONSOLE_KEYS)),
    }
    return [add_spec(direct, True), add_spec(dynamic, False), keys]


@pytest.mark.parametrize(("product_dir", "filename"), _VI_JSON_FILES)
def test_every_vi_json_call_is_a_post_path_of_the_pinned_file(
    product_dir: str, filename: str
) -> None:
    paths = _load(require_shelf_spec(product_dir, filename))["paths"]
    missing = [
        op_id for op_id in _VIM_CALLS if "post" not in (paths.get(op_id.partition(":")[2]) or {})
    ]
    assert not missing, f"{product_dir}/{filename} has no POST path for {missing}"


@pytest.mark.parametrize(("product_dir", "filename"), _VI_JSON_FILES)
def test_every_sent_data_object_and_field_is_in_the_pinned_file(
    product_dir: str, filename: str
) -> None:
    schemas = _load(require_shelf_spec(product_dir, filename))["components"]["schemas"]
    problems: list[str] = []
    for body in _sent_bodies():
        for obj in _objects(body):
            name = obj["_typeName"]
            if name not in schemas:
                problems.append(f"no schema {name}")
                continue
            unknown = set(obj) - {"_typeName"} - _fields(schemas, name)
            if unknown:
                problems.append(f"{name} has no field {sorted(unknown)}")
    assert not problems, f"{product_dir}/{filename}: {problems}"
