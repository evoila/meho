# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for the vSphere licence calls.

The licence operations (``vmware.license.list``,
``vmware.license.assigned.list``, ``vmware.composite.license.assign``) call
three VI-JSON methods and read two ``LicenseManager`` properties. Against the
pinned ``vcenter-9.0/vi-json.yaml`` this lane checks that:

* each method is served;
* the request bodies MEHO sends use only the field names the spec declares;
* the two properties MEHO reads exist.

The vCenter 8.0 U1, U2 and U3 files are checked by the source scan in
``test_connectors_vmware_rest_8_0_spec_reconcile.py``, which finds these
calls on its own. The shelf-backed tests skip when the spec shelf is not
there; the pin test always runs. Mirrors the guest-ops lane, following
``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _license, _write
from meho_backplane.connectors.vmware_rest.license_common import (
    OP_QUERY_ASSIGNED_LICENSES,
    OP_UPDATE_ASSIGNED_LICENSE,
)
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)
from tests.test_connectors_vmware_rest_license import _assign, _FakeVcenter, _GateRecorder

#: The ``LicenseManager`` properties the property read asks for, as the
#: VI-JSON property paths of the spec.
_PROPERTY_PATHS = {
    "GET:/LicenseManager/{moId}/licenses",
    "GET:/LicenseManager/{moId}/licenseAssignmentManager",
}


def test_licence_sub_op_manifest_is_pinned() -> None:
    """Pin the vendor methods of the assign write (sandbox-safe, always runs)."""
    assert _license._VIM_SUB_OPS_LICENSE_ASSIGN == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/LicenseAssignmentManager/{moId}/QueryAssignedLicenses",
        "POST:/LicenseAssignmentManager/{moId}/UpdateAssignedLicense",
    )


def test_licence_methods_and_properties_are_served_by_the_pinned_vi_json_spec() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(
        set(_license._VIM_SUB_OPS_LICENSE_ASSIGN) | _PROPERTY_PATHS,
        served,
        spec_label="vcenter-9.0/vi-json.yaml",
    )


async def test_licence_request_bodies_use_the_spec_field_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    spec_fields = openapi_request_body_props(spec_path)
    assert spec_fields[OP_UPDATE_ASSIGNED_LICENSE] == {"entity", "licenseKey", "entityDisplayName"}
    assert spec_fields[OP_QUERY_ASSIGNED_LICENSES] == {"entityId"}

    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder())
    conn = _FakeVcenter()
    out = await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    assert out["status"] == "assigned"
    for method, op_id in (
        ("UpdateAssignedLicense", OP_UPDATE_ASSIGNED_LICENSE),
        ("QueryAssignedLicenses", OP_QUERY_ASSIGNED_LICENSES),
    ):
        for body in conn.calls_to(method):
            assert set(body) <= spec_fields[op_id], (method, sorted(body))
