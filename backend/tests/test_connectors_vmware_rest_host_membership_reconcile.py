# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the host membership calls against the pinned vendor VI-JSON files.

* An always-on test pins the vendor-call lists of the five changes, so a
  change to them is seen.
* The shelf tests read the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 files (U1, U2, U3): every VI-JSON method is a ``POST`` path there,
  and the request bodies MEHO sends use only fields of the vendor's request
  types. They skip when the spec shelf is not configured and run in CI.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites._host_membership_ops import (
    HOST_MEMBERSHIP_GOVERNED_SUBOPS,
)
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = (
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
)

_VIM_CALLS = sorted(
    {
        op_id
        for calls in HOST_MEMBERSHIP_GOVERNED_SUBOPS.values()
        for op_id in calls
        if not op_id.startswith("GET:")
    }
)


@functools.cache
def _spec(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fields(spec: dict[str, Any], type_name: str) -> set[str]:
    """The fields of a vendor request or data type (one ``allOf`` level deep)."""
    schema = spec["components"]["schemas"][type_name]
    fields = set(schema.get("properties") or {})
    for part in schema.get("allOf") or []:
        fields |= set(part.get("properties") or {})
    return fields


def test_the_vendor_call_lists_are_the_expected_sets() -> None:
    assert set(HOST_MEMBERSHIP_GOVERNED_SUBOPS) == {
        "vmware.composite.host.add",
        "vmware.composite.host.move",
        "vmware.composite.host.connect",
        "vmware.composite.host.disconnect",
        "vmware.composite.host.remove",
    }
    assert _VIM_CALLS == [
        "POST:/ClusterComputeResource/{moId}/AddHost_Task",
        "POST:/ClusterComputeResource/{moId}/MoveHostInto_Task",
        "POST:/ComputeResource/{moId}/Destroy_Task",
        "POST:/Folder/{moId}/AddStandaloneHost_Task",
        "POST:/HostSystem/{moId}/Destroy_Task",
        "POST:/HostSystem/{moId}/DisconnectHost_Task",
        "POST:/HostSystem/{moId}/ReconnectHost_Task",
        "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    ]
    assert HOST_MEMBERSHIP_GOVERNED_SUBOPS["vmware.composite.host.add"][0] == "GET:/vcenter/host"


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_every_vi_json_call_is_a_post_path_of_the_pinned_file(
    product_dir: str, file_name: str
) -> None:
    paths = _spec(require_shelf_spec(product_dir, file_name))["paths"]
    missing = [
        op_id for op_id in _VIM_CALLS if "post" not in paths.get(op_id.partition(":")[2], {})
    ]
    assert not missing, f"{product_dir}/{file_name} has no POST path for {missing}"


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_the_request_bodies_use_vendor_fields(product_dir: str, file_name: str) -> None:
    spec = _spec(require_shelf_spec(product_dir, file_name))
    assert {"spec", "asConnected"} <= _fields(spec, "AddHostRequestType")
    assert {"spec", "addConnected"} <= _fields(spec, "AddStandaloneHostRequestType")
    assert "host" in _fields(spec, "MoveHostIntoRequestType")
    # MEHO sends an empty ReconnectHost body: both fields are optional.
    assert not spec["components"]["schemas"]["ReconnectHostRequestType"].get("required")
    assert {"hostName", "port", "sslThumbprint", "userName", "password", "force"} <= _fields(
        spec, "HostConnectSpec"
    )
