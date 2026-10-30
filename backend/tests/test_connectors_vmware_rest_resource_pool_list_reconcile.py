# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.resource_pool.list``.

The operation calls five vSphere Automation REST paths: the resource pool
list and details, the cluster list and details, and the host list. Against
the pinned ``vcenter.yaml`` of vCenter 8.0 U3 and 9.0 this lane checks that:

* each ``GET`` path is served (the name inside ``{...}`` differs between the
  two files, so it is not compared);
* the resource pool list takes the ``clusters``, ``hosts`` and
  ``parent_resource_pools`` query parameters, and the host list takes
  ``clusters``;
* the answers have the fields the operation reads.

The shelf-backed tests skip when the spec shelf is not there; the pin test
always runs. Follows ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.typed_ops_resource_pool_list import (
    CLUSTER_INFO_PATH,
    CLUSTER_LIST_PATH,
    HOST_LIST_PATH,
    RESOURCE_POOL_FILTER_PARAMS,
    RESOURCE_POOL_INFO_PATH,
    RESOURCE_POOL_LIST_PATH,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SHELF_DIRS = ("vcenter-8.0", "vcenter-9.0")
_SHELF_FILE = "vcenter.yaml"
_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def _plain(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


def _declared_op_ids() -> set[str]:
    paths = (
        RESOURCE_POOL_LIST_PATH,
        RESOURCE_POOL_INFO_PATH,
        CLUSTER_LIST_PATH,
        CLUSTER_INFO_PATH,
        HOST_LIST_PATH,
    )
    return {_plain(f"GET:{path}") for path in paths}


def test_resource_pool_list_paths_are_pinned() -> None:
    """Pin the REST calls of the operation (sandbox-safe, always runs)."""
    assert _declared_op_ids() == {
        "GET:/vcenter/resource-pool",
        "GET:/vcenter/resource-pool/{}",
        "GET:/vcenter/cluster",
        "GET:/vcenter/cluster/{}",
        "GET:/vcenter/host",
    }
    assert set(RESOURCE_POOL_FILTER_PARAMS.values()) == {
        "clusters",
        "hosts",
        "parent_resource_pools",
    }


def _load(shelf_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = require_shelf_spec(shelf_dir, _SHELF_FILE)
    text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(f"file://{spec_path}", spec_source=f"spec:{_SHELF_FILE}", content=text)
    return {_plain(row.op_id): row for row in rows}, yaml.safe_load(text)


@pytest.mark.parametrize("shelf_dir", _SHELF_DIRS)
def test_paths_and_filters_are_served_by_the_pinned_spec(shelf_dir: str) -> None:
    by_op_id, _ = _load(shelf_dir)
    assert_op_ids_served(_declared_op_ids(), set(by_op_id), spec_label=f"{shelf_dir}/{_SHELF_FILE}")
    expected = {
        f"GET:{RESOURCE_POOL_LIST_PATH}": set(RESOURCE_POOL_FILTER_PARAMS.values()),
        f"GET:{HOST_LIST_PATH}": {"clusters"},
    }
    for op_id, names in expected.items():
        props = by_op_id[op_id].parameter_schema.get("properties", {})
        for name in names:
            param = props.get(name)
            assert isinstance(param, dict), f"{op_id} has no {name!r} parameter"
            assert param.get("x-meho-param-loc") == "query", (op_id, param)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    # The 9.0 file wraps a field's reference in a one-item ``allOf``.
    if isinstance(schema.get("allOf"), list) and len(schema["allOf"]) == 1:
        schema = schema["allOf"][0]
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _answer(spec: dict[str, Any], plain_path: str) -> dict[str, Any]:
    (path,) = [p for p in spec["paths"] if _PLACEHOLDER.sub("{}", p) == plain_path]
    schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
    answer = _resolve(spec, schema["schema"])
    return _resolve(spec, answer["items"]) if answer.get("type") == "array" else answer


def _props(spec: dict[str, Any], schema: dict[str, Any]) -> set[str]:
    return set(_resolve(spec, schema)["properties"])


@pytest.mark.parametrize("shelf_dir", _SHELF_DIRS)
def test_answers_have_the_fields_the_operation_reads(shelf_dir: str) -> None:
    _, spec = _load(shelf_dir)
    assert {"resource_pool", "name"} <= _props(spec, _answer(spec, RESOURCE_POOL_LIST_PATH))
    info = _answer(spec, _plain(RESOURCE_POOL_INFO_PATH))
    assert {"resource_pools", "cpu_allocation", "memory_allocation"} <= set(info["properties"])
    allocation = _resolve(spec, info["properties"]["cpu_allocation"])
    assert {"reservation", "limit", "expandable_reservation", "shares"} <= set(
        allocation["properties"]
    )
    assert {"level", "shares"} <= _props(spec, allocation["properties"]["shares"])
    assert "cluster" in _props(spec, _answer(spec, CLUSTER_LIST_PATH))
    assert "resource_pool" in _props(spec, _answer(spec, _plain(CLUSTER_INFO_PATH)))
    assert "host" in _props(spec, _answer(spec, HOST_LIST_PATH))
