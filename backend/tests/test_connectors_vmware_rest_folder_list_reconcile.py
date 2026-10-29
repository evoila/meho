# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.folder.list``.

The folder list calls two REST paths and reads one VI-JSON property. Against
the pinned vCenter API files this lane checks that:

* ``GET /vcenter/folder`` and ``GET /vcenter/datacenter`` are served by the
  vCenter 9.0 and 8.0 U3 ``vcenter.yaml``, with the filters MEHO sends;
* the folder summary has the fields MEHO reads, and the folder types match
  the ``type`` enum of the operation (9.0 file);
* the ``parent`` property of ``Folder`` is served by the 9.0 ``vi-json.yaml``.

The ``RetrievePropertiesEx`` call on vCenter 8.0 is checked by the source scan
in ``test_connectors_vmware_rest_8_0_spec_reconcile.py``. The shelf-backed
tests skip when the spec shelf is not there; the pin test always runs.
Follows ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest import typed_ops_folder_list as folder_list
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, openapi_served_op_ids, require_shelf_spec

_FOLDER_OP = "GET:/vcenter/folder"
_DATACENTER_OP = "GET:/vcenter/datacenter"
_PARENT_PROPERTY = "GET:/Folder/{moId}/parent"

#: The ``/api`` names of the filters MEHO sends (``adapt_op_query`` drops the
#: ``filter.`` prefix there).
_FOLDER_FILTERS = {"type", "names", "parent_folders", "datacenters"}


def test_paths_and_filter_names_are_pinned() -> None:
    """Sandbox-safe pin of what the reconcile below checks."""
    assert folder_list.FOLDER_LIST_PATH == "/vcenter/folder"
    assert folder_list.DATACENTER_LIST_PATH == "/vcenter/datacenter"
    assert {key.removeprefix("filter.") for key in folder_list._FILTER_KEYS.values()} == (
        _FOLDER_FILTERS
    )


def _query_names(spec_path: Path, op_id: str) -> set[str]:
    """Query parameter names of one op; an exploded object adds its fields."""
    rows = parse_openapi(
        "spec:vcenter.yaml", spec_source="spec:vcenter.yaml", content=spec_path.read_text()
    )
    [row] = [r for r in rows if r.op_id == op_id]
    names: set[str] = set()
    for name, schema in row.parameter_schema.get("properties", {}).items():
        names.add(name)
        if isinstance(schema, dict) and schema.get("type") == "object":
            names |= set(schema.get("properties", {}))
    return names


@pytest.mark.parametrize("shelf_dir", ["vcenter-9.0", "vcenter-8.0"])
def test_rest_paths_and_filters_are_served(shelf_dir: str) -> None:
    spec_path = require_shelf_spec(shelf_dir, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(
        {_FOLDER_OP, _DATACENTER_OP}, served, spec_label=f"{shelf_dir}/vcenter.yaml"
    )
    assert _FOLDER_FILTERS.issubset(_query_names(spec_path, _FOLDER_OP))
    assert "datacenters" in _query_names(spec_path, _DATACENTER_OP)


def test_folder_summary_fields_and_types_match_the_9_0_spec() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    schemas: dict[str, Any] = yaml.safe_load(spec_path.read_text())["components"]["schemas"]
    assert {"folder", "name", "type"} <= set(schemas["Vcenter.Folder.Summary"]["properties"])
    assert {"datacenter", "name"} <= set(schemas["Vcenter.Datacenter.Summary"]["properties"])
    assert tuple(schemas["Vcenter.Folder.Type"]["enum"]) == folder_list.FOLDER_TYPES


def test_folder_parent_property_is_served_by_the_pinned_vi_json_spec() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served({_PARENT_PROPERTY}, served, spec_label="vcenter-9.0/vi-json.yaml")
