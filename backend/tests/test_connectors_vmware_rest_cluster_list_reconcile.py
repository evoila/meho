# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.cluster.list``.

The operation calls two vSphere Automation REST lists (the datacenters, then
the clusters of each datacenter) and reads two ``ClusterComputeResource``
properties through VI-JSON. This lane checks that:

* against the pinned ``vcenter.yaml`` of vCenter 8.0 U3 and 9.0: each ``GET``
  path is served, takes the filters the operation sends, and a cluster summary
  has the fields the operation reads;
* against the pinned VI-JSON files of vCenter 9.0 and 8.0 U1, U2 and U3: the
  data types behind the two properties have the fields the operation reads
  (``ComputeResourceSummary.numHosts``, ``ClusterConfigInfoEx.vsanConfigInfo``,
  ``VsanClusterConfigInfo.enabled``);
* against the pinned 9.0 VI-JSON file: both properties are served.

The ``RetrievePropertiesEx`` method itself is checked against the 8.0 files by
the source scan in ``test_connectors_vmware_rest_8_0_spec_reconcile.py``. The
shelf-backed tests skip when the spec shelf is not there; the pin test always
runs. Follows ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.typed_ops_cluster_list import (
    CLUSTER_DATACENTER_FILTER_PARAM,
    CLUSTER_FOLDER_FILTER_PARAM,
    CLUSTER_LIST_PATH,
    CLUSTER_MO_TYPE,
    CLUSTER_PROPERTY_PATHS,
    CLUSTER_SUMMARY_FIELDS,
    DATACENTER_LIST_PATH,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, openapi_served_op_ids, require_shelf_spec

_REST_SHELF_DIRS = ("vcenter-8.0", "vcenter-9.0")
_REST_FILE = "vcenter.yaml"

#: ``(shelf product dir, file)`` of every pinned VI-JSON file.
_VI_JSON_FILES: dict[str, tuple[str, str]] = {
    "9.0": ("vcenter-9.0", "vi-json.yaml"),
    "8.0u1": ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    "8.0u2": ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    "8.0u3": ("vcenter-8.0", "vi-json-8.0u3.yaml"),
}

#: The REST filters each list call sends.
_FILTERS_BY_PATH = {
    DATACENTER_LIST_PATH: (CLUSTER_DATACENTER_FILTER_PARAM,),
    CLUSTER_LIST_PATH: (CLUSTER_DATACENTER_FILTER_PARAM, CLUSTER_FOLDER_FILTER_PARAM),
}


def test_cluster_list_calls_are_pinned() -> None:
    """Pin the vendor calls of the operation (sandbox-safe, always runs)."""
    assert DATACENTER_LIST_PATH == "/vcenter/datacenter"
    assert CLUSTER_LIST_PATH == "/vcenter/cluster"
    assert (CLUSTER_DATACENTER_FILTER_PARAM, CLUSTER_FOLDER_FILTER_PARAM) == (
        "datacenters",
        "folders",
    )
    assert CLUSTER_SUMMARY_FIELDS == ("cluster", "name", "ha_enabled", "drs_enabled")
    assert CLUSTER_MO_TYPE == "ClusterComputeResource"
    assert CLUSTER_PROPERTY_PATHS == ("summary.numHosts", "configurationEx")


@pytest.mark.parametrize("shelf_dir", _REST_SHELF_DIRS)
def test_rest_paths_and_filters_are_served_by_the_pinned_spec(shelf_dir: str) -> None:
    spec_path = require_shelf_spec(shelf_dir, _REST_FILE)
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source=f"spec:{_REST_FILE}",
        content=spec_path.read_text(encoding="utf-8"),
    )
    by_op_id = {row.op_id: row for row in rows}
    declared = {f"GET:{path}" for path in _FILTERS_BY_PATH}
    assert_op_ids_served(declared, set(by_op_id), spec_label=f"{shelf_dir}/{_REST_FILE}")
    for path, filters in _FILTERS_BY_PATH.items():
        props = by_op_id[f"GET:{path}"].parameter_schema.get("properties", {})
        for name in filters:
            param = props.get(name)
            assert isinstance(param, dict), f"GET {path} has no {name!r} parameter"
            assert param.get("x-meho-param-loc") == "query", (path, param)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


@pytest.mark.parametrize("shelf_dir", _REST_SHELF_DIRS)
def test_cluster_summary_has_the_fields_the_operation_reads(shelf_dir: str) -> None:
    spec_path = require_shelf_spec(shelf_dir, _REST_FILE)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    answer = spec["paths"][CLUSTER_LIST_PATH]["get"]["responses"]["200"]
    array = answer["content"]["application/json"]["schema"]
    summary = _resolve(spec, array["items"])
    assert set(CLUSTER_SUMMARY_FIELDS) <= set(summary["properties"]), summary


def _component_schema(spec_text: str, schema_name: str) -> dict[str, Any]:
    """Load one ``components.schemas`` entry from the raw text of a VI-JSON file.

    Slices the block by its 4-space indent and loads only that part, instead
    of the whole ~10 MB file (the same approach as the host evacuate lane).
    """
    lines = spec_text.splitlines()
    key = f"    {schema_name}:"
    try:
        start = lines.index(key)
    except ValueError as exc:
        raise AssertionError(f"{schema_name}: not found in the pinned spec") from exc
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip(" "))) <= 4:
            break
        block.append(line)
    fragment = "\n".join(row[4:] if len(row) >= 4 else row for row in block)
    loaded = yaml.safe_load(fragment)
    assert isinstance(loaded, dict) and isinstance(loaded.get(schema_name), dict)
    return loaded[schema_name]


def _own_properties(schema: dict[str, Any]) -> set[str]:
    """The property names a schema declares itself, also inside ``allOf``."""
    names = set(schema.get("properties") or {})
    for part in schema.get("allOf") or []:
        if isinstance(part, dict):
            names |= set(part.get("properties") or {})
    return names


@pytest.fixture(params=sorted(_VI_JSON_FILES), ids=sorted(_VI_JSON_FILES))
def vi_json_text(request: pytest.FixtureRequest) -> str:
    product_dir, filename = _VI_JSON_FILES[request.param]
    return require_shelf_spec(product_dir, filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("schema_name", "field"),
    [
        ("ComputeResourceSummary", "numHosts"),
        ("ClusterConfigInfoEx", "vsanConfigInfo"),
        ("VsanClusterConfigInfo", "enabled"),
    ],
)
def test_vi_json_types_have_the_fields_the_operation_reads(
    vi_json_text: str, schema_name: str, field: str
) -> None:
    schema = _component_schema(vi_json_text, schema_name)
    assert field in _own_properties(schema), f"{schema_name} has no {field!r}"


def test_cluster_properties_are_served_by_the_pinned_9_0_vi_json_spec() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    declared = {
        f"GET:/{CLUSTER_MO_TYPE}/{{moId}}/{path.split('.', 1)[0]}"
        for path in CLUSTER_PROPERTY_PATHS
    }
    assert_op_ids_served(
        declared, openapi_served_op_ids(spec_path), spec_label="vcenter-9.0/vi-json.yaml"
    )
