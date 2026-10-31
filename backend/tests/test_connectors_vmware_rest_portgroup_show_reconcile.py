# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for ``vmware.network.portgroup.show``.

The operation reads the VIM property ``DistributedVirtualPortgroup.config`` and
the switch's ``name`` and ``config.defaultPortConfig`` with
``PropertyCollector.RetrievePropertiesEx`` and, for a lookup by name, calls the
REST list ``GET /vcenter/network`` with the ``names`` and ``types`` filters.

* An always-on test pins the VIM types, the properties and the REST path.
* The shelf tests check, in the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 update files, that ``DistributedVirtualPortgroup`` has the
  ``config`` property, that ``DistributedVirtualSwitch`` has ``name`` and
  ``config``, that ``RetrievePropertiesEx`` is served, and that every VIM
  field the operation reads exists on its type. They also check the REST path
  and its two filters in the pinned ``vcenter.yaml`` of 8.0 U3 and 9.0.

The shelf tests skip when the spec shelf is not there and run in CI. Follows
``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.typed_ops_portgroup_show import (
    DVPG_MO_TYPE,
    DVS_MO_TYPE,
    PORT_SETTING_FIELDS,
    PORTGROUP_CONFIG_PROPERTY,
    PORTGROUP_LIST_PATH,
    SWITCH_DEFAULTS_PROPERTY,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = [
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
]
_VCENTER_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def test_read_targets_are_pinned() -> None:
    """Shape gate (always runs)."""
    assert DVPG_MO_TYPE == "DistributedVirtualPortgroup"
    assert PORTGROUP_CONFIG_PROPERTY == "config"
    assert DVS_MO_TYPE == "DistributedVirtualSwitch"
    assert SWITCH_DEFAULTS_PROPERTY == "config.defaultPortConfig"
    assert "defaultPortConfig" in PORT_SETTING_FIELDS["DVSConfigInfo"]
    assert PORTGROUP_LIST_PATH == "/vcenter/network"


def _schema_fields(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema.

    A line scan instead of a YAML parse of the large file: a schema key sits at
    4-space indent and its property names at 8-space indent under
    ``properties:`` (the same scan idea as the alarm and 8.0 lanes).
    """
    fields: set[str] = set()
    inside = False
    in_props = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     ") and line.strip():
            break
        if line.startswith("      ") and not line.startswith("       "):
            in_props = line.strip() == "properties:"
            continue
        if in_props and line.startswith("        ") and not line.startswith("         "):
            key = line.strip()
            if key.endswith(":"):
                fields.add(key[:-1])
    return fields


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_vim_fields_exist_in_the_pinned_vi_json(shelf: str, filename: str) -> None:
    spec_path = require_shelf_spec(shelf, filename)
    lines = spec_path.read_text(encoding="utf-8").splitlines()
    assert f"  /{DVPG_MO_TYPE}/{{moId}}/{PORTGROUP_CONFIG_PROPERTY}:" in lines
    assert f"  /{DVS_MO_TYPE}/{{moId}}/name:" in lines
    assert f"  /{DVS_MO_TYPE}/{{moId}}/config:" in lines
    assert "  /PropertyCollector/{moId}/RetrievePropertiesEx:" in lines
    missing = {
        vim_type: sorted(set(fields) - _schema_fields(lines, vim_type))
        for vim_type, fields in PORT_SETTING_FIELDS.items()
    }
    missing = {vim_type: names for vim_type, names in missing.items() if names}
    assert not missing, f"{shelf}/{filename} lacks these VIM fields: {missing}"


@pytest.mark.parametrize("shelf", _VCENTER_SHELVES)
def test_network_list_takes_the_names_and_types_filters(shelf: str) -> None:
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_path.read_text(encoding="utf-8"),
    )
    by_op_id = {row.op_id: row for row in rows}
    row = by_op_id.get(f"GET:{PORTGROUP_LIST_PATH}")
    assert row is not None, f"{shelf}/vcenter.yaml does not serve GET {PORTGROUP_LIST_PATH}"
    props = row.parameter_schema.get("properties", {})
    for param in ("names", "types"):
        assert isinstance(props.get(param), dict), f"GET {PORTGROUP_LIST_PATH} has no {param!r}"
        assert props[param].get("x-meho-param-loc") == "query", (param, props[param])
