# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.network.list``.

The operation calls two vSphere Automation REST paths (the datacenter list
and the network list) and, for the distributed port groups, one vim read of
``DistributedVirtualPortgroup.config``. This lane checks:

* against the pinned ``vcenter.yaml`` of vCenter 8.0 U3 and 9.0: both
  ``GET`` paths are served, the network list takes the ``types``,
  ``folders`` and ``datacenters`` query parameters, the answers have the
  fields the operation reads, and the network type enum is the one the
  ``type`` parameter offers;
* against the pinned VI/JSON files (9.0 and, when on the shelf, 8.0 U1 to
  U3): ``RetrievePropertiesEx`` and the portgroup ``config`` property are
  served; in the 9.0 file the config has ``distributedVirtualSwitch``,
  ``defaultPortConfig`` and ``backingType``, the backing type enum is
  ``standard`` / ``nsx``, and the port setting has ``vlan``.

The shelf-backed tests skip when the spec shelf is not there; the pin test
always runs. Follows ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites._read import (
    _DVPG_DEFAULT_BACKING_TYPE,
    _PROP_DVPG_BACKING_TYPE,
)
from meho_backplane.connectors.vmware_rest.typed_ops_network_list import (
    _NSX_BACKING,
    DATACENTER_LIST_PATH,
    NETWORK_FILTER_PARAMS,
    NETWORK_LIST_PATH,
    NETWORK_TYPES,
    VMWARE_NETWORK_LIST_OP,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_REST_SHELF_DIRS = ("vcenter-8.0", "vcenter-9.0")
_VI_JSON_FILES = (
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
)
_VIM_PATHS = (
    "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    "GET:/DistributedVirtualPortgroup/{moId}/config",
)


def test_network_list_paths_are_pinned() -> None:
    """Pin the REST calls and filters of the operation (sandbox-safe, always runs)."""
    assert _PROP_DVPG_BACKING_TYPE == "config.backingType"
    assert (_DVPG_DEFAULT_BACKING_TYPE, _NSX_BACKING) == ("standard", "nsx")
    assert (DATACENTER_LIST_PATH, NETWORK_LIST_PATH) == ("/vcenter/datacenter", "/vcenter/network")
    assert set(NETWORK_FILTER_PARAMS.values()) == {"types", "folders"}
    assert NETWORK_TYPES == ("STANDARD_PORTGROUP", "DISTRIBUTED_PORTGROUP", "OPAQUE_NETWORK")
    assert VMWARE_NETWORK_LIST_OP.parameter_schema["properties"]["type"]["enum"] == list(
        NETWORK_TYPES
    )


def _load(shelf_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = require_shelf_spec(shelf_dir, "vcenter.yaml")
    text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(f"file://{spec_path}", spec_source="spec:vcenter.yaml", content=text)
    return {row.op_id: row for row in rows}, yaml.safe_load(text)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema.get("allOf"), list) and len(schema["allOf"]) == 1:
        schema = schema["allOf"][0]
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _item(spec: dict[str, Any], path: str) -> dict[str, Any]:
    schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
    answer = _resolve(spec, schema["schema"])
    return _resolve(spec, answer["items"])


@pytest.mark.parametrize("shelf_dir", _REST_SHELF_DIRS)
def test_paths_and_filters_are_served_by_the_pinned_spec(shelf_dir: str) -> None:
    by_op_id, _ = _load(shelf_dir)
    declared = {f"GET:{DATACENTER_LIST_PATH}", f"GET:{NETWORK_LIST_PATH}"}
    assert_op_ids_served(declared, set(by_op_id), spec_label=f"{shelf_dir}/vcenter.yaml")
    props = by_op_id[f"GET:{NETWORK_LIST_PATH}"].parameter_schema.get("properties", {})
    for name in {*NETWORK_FILTER_PARAMS.values(), "datacenters"}:
        param = props.get(name)
        assert isinstance(param, dict), f"GET {NETWORK_LIST_PATH} has no {name!r} parameter"
        assert param.get("x-meho-param-loc") == "query", (name, param)


@pytest.mark.parametrize("shelf_dir", _REST_SHELF_DIRS)
def test_answers_have_the_fields_and_types_the_operation_reads(shelf_dir: str) -> None:
    _, spec = _load(shelf_dir)
    network = _item(spec, NETWORK_LIST_PATH)
    assert {"network", "name", "type"} <= set(network["properties"])
    # The 8.0 file names the enum schema VcenterNetworkType, the 9.0 file
    # Vcenter.Network.Type (and its summary repeats the values only in text).
    schemas = spec["components"]["schemas"]
    (type_schema,) = [
        schemas[k] for k in ("VcenterNetworkType", "Vcenter.Network.Type") if k in schemas
    ]
    assert tuple(type_schema["enum"]) == NETWORK_TYPES
    assert {"datacenter", "name"} <= set(_item(spec, DATACENTER_LIST_PATH)["properties"])


def _vi_json_lines(shelf_dir: str, file_name: str) -> list[str]:
    return require_shelf_spec(shelf_dir, file_name).read_text(encoding="utf-8").splitlines()


def _served(lines: list[str]) -> set[str]:
    """``<METHOD>:<path>`` of every path item (a line scan; the file is 8 MB)."""
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
        elif current is not None and line.startswith("    ") and not line.startswith("     "):
            method = line.strip().rstrip(":")
            if method in {"get", "post"}:
                found.add(f"{method.upper()}:{current}")
    return found


def _enum_values(lines: list[str], schema: str) -> set[str]:
    """The values of one enum component schema (the ``- 'x'`` lines under ``enum:``)."""
    values: set[str] = set()
    inside = in_enum = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     "):
            break
        if line.startswith("      ") and not line.startswith("       "):
            in_enum = line.strip() == "enum:"
        elif in_enum and line.startswith("        - "):
            values.add(line.removeprefix("        - ").strip().strip("'\""))
    return values


def _schema_properties(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema (4-space key, 8-space properties)."""
    names: set[str] = set()
    inside = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     "):
            break
        if line.startswith("        ") and not line.startswith("         "):
            key = line.strip()
            if key.endswith(":"):
                names.add(key[:-1])
    return names


@pytest.mark.parametrize(("shelf_dir", "file_name"), _VI_JSON_FILES)
def test_vim_read_is_served_by_the_pinned_vi_json(shelf_dir: str, file_name: str) -> None:
    served = _served(_vi_json_lines(shelf_dir, file_name))
    missing = sorted(set(_VIM_PATHS) - served)
    assert not missing, f"{shelf_dir}/{file_name} has no path for {missing}"


def test_portgroup_config_has_the_switch_and_vlan_fields() -> None:
    lines = _vi_json_lines("vcenter-9.0", "vi-json.yaml")
    assert {"distributedVirtualSwitch", "defaultPortConfig", "backingType"} <= _schema_properties(
        lines, "DVPortgroupConfigInfo"
    )
    assert _enum_values(lines, "DistributedVirtualPortgroupBackingType_enum") == {
        _DVPG_DEFAULT_BACKING_TYPE,
        _NSX_BACKING,
    }
    assert "vlan" in _schema_properties(lines, "VMwareDVSPortSetting")
