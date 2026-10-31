# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hcx spec-reconcile lane -- hand-coded paths vs the pinned HCX API files.

Every hand-coded ``METHOD:/path`` literal in the hcx connector is listed
here with its method, introspected from the live module constants:

* the login, ``POST /hybridity/api/sessions``;
* the version call, ``POST /hybridity/api/hcspAppliances``;
* the calls of the reads (``typed_reads.VENDOR_CALLS``): the local
  site list, site pairings, service meshes, appliances, the service mesh
  diagnostics, one job, alerts and the inventory;
* the calls of the service mesh changes and the task read
  (``service_mesh_writes.VENDOR_CALLS``);
* the calls of the network extension operations
  (``network_extension.VENDOR_CALLS``): list, extend, unextend, Mobility
  Optimized Networking and the policy routes;
* the documentation path, ``GET /hybridity/docs`` -- marked
  "documentation path, not in the API file" and left out of the served
  check, because the Manager serves it as a web page, not as an API call.

No HCX API file is pinned yet: Broadcom publishes none, and the lab pins
the files that a running Manager serves through ``hcx.api_file.get``
(the read this connector ships). Until then the shelf lane skips with the
harness's uniform reason (the waiting lane of
``docs/decisions/spec-reconcile-guards-standard.md``); the manifest-pin
test below runs always, so the enumeration stays proven. When the lab
pins the files, rename :data:`_SPEC_FILE` to the pinned file name and the
lane arms itself.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.hcx import api_file as _api_file_module
from meho_backplane.connectors.hcx import connector as _connector_module
from meho_backplane.connectors.hcx import network_extension as _network_extension_module
from meho_backplane.connectors.hcx import service_mesh_writes as _service_mesh_writes_module
from meho_backplane.connectors.hcx import typed_reads as _typed_reads_module
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

#: One shelf directory per HCX version the connector covers.
_SPEC_DIRS = ("hcx-4.11", "hcx-9.0", "hcx-9.1")
#: Expected pinned file name (OpenAPI 3.x conversion of the served
#: description). Renamed when the lab pins the files.
_SPEC_FILE = "hybridity_api.openapi3.json"

#: Documentation path, not in the API file: served as a web page.
_DOCUMENTATION_OP_IDS = frozenset({f"GET:{_api_file_module._DOCS_PATH}"})


def _declared_api_op_ids() -> set[str]:
    """The hand-coded API calls.

    Every ``*_PATH`` constant of the connector module (``POST``), plus every
    ``(method, path)`` pair of ``typed_reads.VENDOR_CALLS`` and
    ``service_mesh_writes.VENDOR_CALLS`` and ``network_extension.VENDOR_CALLS``.
    """
    connector_calls = {
        f"POST:{value}"
        for name, value in vars(_connector_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }
    read_calls = {f"{method}:{path}" for method, path in _typed_reads_module.VENDOR_CALLS}
    mesh_calls = {f"{method}:{path}" for method, path in _service_mesh_writes_module.VENDOR_CALLS}
    extension_calls = {
        f"{method}:{path}" for method, path in _network_extension_module.VENDOR_CALLS
    }
    return connector_calls | read_calls | mesh_calls | extension_calls


def test_hcx_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift cannot silently shrink the reconcile."""
    assert _declared_api_op_ids() == {
        "POST:/hybridity/api/sessions",
        "POST:/hybridity/api/hcspAppliances",
        "POST:/hybridity/api/service/inventory/cloud/list",
        "GET:/hybridity/api/cloudConfigs",
        "GET:/hybridity/api/interconnect/serviceMesh",
        "GET:/hybridity/api/interconnect/appliances",
        "POST:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnose",
        "POST:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnostics/query",
        "GET:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnostics/{serviceMeshDiagId}",
        "GET:/hybridity/api/jobs/{jobid}",
        "POST:/hybridity/api/alerts",
        "POST:/hybridity/api/service/inventory/resourcecontainer/list",
        "POST:/hybridity/api/service/inventory/vc/list",
        "POST:/hybridity/api/service/inventory/vc/datastores/query",
        "POST:/hybridity/api/service/inventory/networks",
        "POST:/hybridity/api/interconnect/serviceMesh",
        "GET:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}",
        "PUT:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}",
        "DELETE:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}",
        "POST:/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/resync",
        "GET:/hybridity/api/interconnect/tasks/{interconnectTaskId}",
        "POST:/hybridity/api/interconnect/tasks/query",
        "GET:/hybridity/api/l2Extensions",
        "POST:/hybridity/api/l2Extensions",
        "DELETE:/hybridity/api/l2Extensions/{extensionId}",
        "PUT:/hybridity/api/networkExtensions/{extensionId}",
        "PUT:/hybridity/api/l2Extensions/policyRoutes",
    }
    assert {"GET:/hybridity/docs"} == _DOCUMENTATION_OP_IDS


@pytest.mark.parametrize("spec_dir", _SPEC_DIRS)
def test_hcx_hand_coded_api_paths_are_served_by_the_pinned_file(spec_dir: str) -> None:
    """Every hand-coded hcx API call is served by the pinned file (skips until pinned)."""
    spec_path = require_shelf_spec(spec_dir, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_api_op_ids(), served, spec_label=f"{spec_dir}/{_SPEC_FILE}")
