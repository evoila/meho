# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.datacenter.list``.

The operation calls four vSphere Automation REST lists: the datacenters, then
the clusters, hosts and datastores of each datacenter with the ``datacenters``
filter. Against the pinned ``vcenter.yaml`` of vCenter 8.0 U3 and 9.0 this
lane checks that:

* each ``GET`` path is served;
* each count call takes the ``datacenters`` query parameter;
* a datacenter summary has the ``datacenter`` and ``name`` fields the
  operation reads.

The shelf-backed tests skip when the spec shelf is not there; the pin test
always runs. Follows ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.typed_ops_datacenter_list import (
    DATACENTER_COUNT_PATHS,
    DATACENTER_FILTER_PARAM,
    DATACENTER_LIST_PATH,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SHELF_DIRS = ("vcenter-8.0", "vcenter-9.0")
_SHELF_FILE = "vcenter.yaml"


def _declared_op_ids() -> set[str]:
    return {f"GET:{DATACENTER_LIST_PATH}"} | {
        f"GET:{path}" for path in DATACENTER_COUNT_PATHS.values()
    }


def test_datacenter_list_paths_are_pinned() -> None:
    """Pin the REST calls of the operation (sandbox-safe, always runs)."""
    assert _declared_op_ids() == {
        "GET:/vcenter/datacenter",
        "GET:/vcenter/cluster",
        "GET:/vcenter/host",
        "GET:/vcenter/datastore",
    }
    assert DATACENTER_FILTER_PARAM == "datacenters"


@pytest.mark.parametrize("shelf_dir", _SHELF_DIRS)
def test_paths_and_filter_are_served_by_the_pinned_spec(shelf_dir: str) -> None:
    spec_path = require_shelf_spec(shelf_dir, _SHELF_FILE)
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source=f"spec:{_SHELF_FILE}",
        content=spec_path.read_text(encoding="utf-8"),
    )
    by_op_id = {row.op_id: row for row in rows}
    assert_op_ids_served(_declared_op_ids(), set(by_op_id), spec_label=f"{shelf_dir}/{_SHELF_FILE}")
    for path in DATACENTER_COUNT_PATHS.values():
        props = by_op_id[f"GET:{path}"].parameter_schema.get("properties", {})
        param = props.get(DATACENTER_FILTER_PARAM)
        assert isinstance(param, dict), f"GET {path} has no {DATACENTER_FILTER_PARAM!r} parameter"
        assert param.get("x-meho-param-loc") == "query", (path, param)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


@pytest.mark.parametrize("shelf_dir", _SHELF_DIRS)
def test_datacenter_summary_has_the_fields_the_operation_reads(shelf_dir: str) -> None:
    spec_path: Path = require_shelf_spec(shelf_dir, _SHELF_FILE)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    answer = spec["paths"][DATACENTER_LIST_PATH]["get"]["responses"]["200"]
    array = answer["content"]["application/json"]["schema"]
    summary = _resolve(spec, array["items"])
    assert {"datacenter", "name"} <= set(summary["properties"]), summary
