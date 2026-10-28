# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pinned-spec reconcile lane for the ``host.evacuate`` maintenance body (#4895).

``vmware.composite.host.evacuate`` sends ``EnterMaintenanceMode_Task`` with a
``maintenanceSpec`` carrying the vSAN data option. This module grounds every
field of that body against the vendor VI/JSON files on the spec shelf, per
the spec-reconcile standard (``docs/decisions/spec-reconcile-guards-standard.md``):

* ``EnterMaintenanceModeRequestType`` declares ``timeout`` and
  ``maintenanceSpec``;
* ``HostMaintenanceSpec`` declares ``vsanMode``;
* ``VsanHostDecommissionMode`` declares (and requires) ``objectAction``;
* ``VsanHostDecommissionModeObjectAction_enum`` lists exactly the three
  values the parameter schema offers.

Checked against the vCenter 9.0 file and the 8.0 U1, U2 and U3 files, the
same files the 8.0 VI-JSON method lane uses. Skips uniformly when the shelf
is unprovisioned (#2980 harness contract); CI provisions it. The
connector-side proof -- that the handler puts exactly this body on the wire
-- is ``tests/test_connectors_vmware_rest_composites_write.py``, which runs
everywhere.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites import _write, schemas
from tests._spec_shelf import require_shelf_spec

#: ``(shelf product dir, file)`` of every pinned VI/JSON file the body must
#: match: vCenter 9.0 plus the three vCenter 8.0 updates.
_VI_JSON_FILES: dict[str, tuple[str, str]] = {
    "9.0": ("vcenter-9.0", "vi-json.yaml"),
    "8.0u1": ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    "8.0u2": ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    "8.0u3": ("vcenter-8.0", "vi-json-8.0u3.yaml"),
}


def _component_schema(spec_text: str, schema_name: str) -> dict[str, Any]:
    """Extract one ``components.schemas`` entry from a pinned spec's raw text.

    Slices the schema's block by its stable 4-space ``components.schemas``
    indentation and YAML-loads only that fragment (the line-scan approach
    of the nested-ESXi recipe lane), rather than loading the whole ~10 MB
    file.
    """
    lines = spec_text.splitlines()
    key = f"    {schema_name}:"
    try:
        start = lines.index(key)
    except ValueError as exc:
        raise AssertionError(f"{schema_name}: not found in the pinned spec") from exc
    block = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and (len(line) - len(line.lstrip(" "))) <= 4:
            break
        block.append(line)
    fragment = "\n".join(row[4:] if len(row) >= 4 else row for row in block)
    loaded = yaml.safe_load(fragment)
    assert isinstance(loaded, dict) and schema_name in loaded
    schema = loaded[schema_name]
    assert isinstance(schema, dict)
    return schema


def _ref_name(prop: dict[str, Any]) -> str | None:
    """The schema name a property points at, through ``$ref`` or ``allOf: [$ref]``."""
    ref = prop.get("$ref")
    if ref is None:
        for entry in prop.get("allOf") or []:
            if isinstance(entry, dict) and "$ref" in entry:
                ref = entry["$ref"]
                break
    return str(ref).rsplit("/", 1)[-1] if isinstance(ref, str) else None


@pytest.fixture(params=sorted(_VI_JSON_FILES), ids=sorted(_VI_JSON_FILES))
def vi_json_text(request: pytest.FixtureRequest) -> str:
    product_dir, filename = _VI_JSON_FILES[request.param]
    return require_shelf_spec(product_dir, filename).read_text(encoding="utf-8")


def test_maintenance_body_matches_the_vendor_file(vi_json_text: str) -> None:
    """Every field of the maintenance body is declared, with the right type, in the file."""
    request_type = _component_schema(vi_json_text, "EnterMaintenanceModeRequestType")
    props = request_type.get("properties", {})
    assert props.get("timeout", {}).get("type") == "integer"
    assert _ref_name(props.get("maintenanceSpec", {})) == _write._HOST_MAINTENANCE_SPEC_TYPE

    maintenance_spec = _component_schema(vi_json_text, _write._HOST_MAINTENANCE_SPEC_TYPE)
    vsan_mode = maintenance_spec.get("properties", {}).get("vsanMode")
    assert isinstance(vsan_mode, dict), "HostMaintenanceSpec lost vsanMode"
    assert _ref_name(vsan_mode) == _write._VSAN_HOST_DECOMMISSION_MODE_TYPE

    decommission_mode = _component_schema(vi_json_text, _write._VSAN_HOST_DECOMMISSION_MODE_TYPE)
    assert decommission_mode.get("properties", {}).get("objectAction", {}).get("type") == "string"
    assert decommission_mode.get("required") == ["objectAction"]


def test_vsan_mode_values_are_the_vendor_enum(vi_json_text: str) -> None:
    """The three ``vsan_mode`` values are exactly the vendor's ``objectAction`` enum."""
    enum_schema = _component_schema(vi_json_text, "VsanHostDecommissionModeObjectAction_enum")
    assert sorted(enum_schema.get("enum", [])) == sorted(schemas.VSAN_MAINTENANCE_MODES)
    assert _write._VSAN_MODE_DEFAULT in schemas.VSAN_MAINTENANCE_MODES


def test_body_emitted_by_the_handler_uses_these_names() -> None:
    """The handler's body builder emits the names the lanes above ground (always-on tie-in)."""
    body = _write._vsan_maintenance_spec("noAction")
    assert body == {
        "_typeName": "HostMaintenanceSpec",
        "vsanMode": {"_typeName": "VsanHostDecommissionMode", "objectAction": "noAction"},
    }
