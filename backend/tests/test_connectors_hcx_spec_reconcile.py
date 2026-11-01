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
* the calls of the profile changes (``profile_writes.VENDOR_CALLS``) and of
  the site pairing change and the certificate trust
  (``site_pairing_writes.VENDOR_CALLS``);
* the calls of the service mesh changes and the task read
  (``service_mesh_writes.VENDOR_CALLS``);
* the calls of the network extension operations
  (``network_extension.VENDOR_CALLS``): list, extend, unextend, Mobility
  Optimized Networking and the policy routes;
* the calls of the migration operations (``migrations.MIGRATION_VENDOR_CALLS``):
  the migration query, the mobility groups and the migration cancel;
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

The HCX Manager setup calls the appliance management API (port 9443), a
second API with its own file. Its lane at the end of this module checks
``manager_setup.APPLIANCE_VENDOR_CALLS`` the same way. The main-API call of
the restart (``manager_restart.MIGRATION_QUERY_CALL``, the migration query)
is part of the main API set above.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.hcx import api_file as _api_file_module
from meho_backplane.connectors.hcx import connector as _connector_module
from meho_backplane.connectors.hcx import manager_restart as _manager_restart_module
from meho_backplane.connectors.hcx import manager_setup as _manager_setup_module
from meho_backplane.connectors.hcx import migrations as _migrations_module
from meho_backplane.connectors.hcx import network_extension as _network_extension_module
from meho_backplane.connectors.hcx import profile_writes as _profile_writes_module
from meho_backplane.connectors.hcx import service_mesh_writes as _service_mesh_writes_module
from meho_backplane.connectors.hcx import site_pairing_writes as _site_pairing_writes_module
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
    ``(method, path)`` pair of ``typed_reads.VENDOR_CALLS``,
    ``profile_writes.VENDOR_CALLS``, ``site_pairing_writes.VENDOR_CALLS``,
    ``service_mesh_writes.VENDOR_CALLS``, ``network_extension.VENDOR_CALLS``
    and ``migrations.MIGRATION_VENDOR_CALLS``, and the migration query of the
    restart (``manager_restart.MIGRATION_QUERY_CALL``).
    """
    connector_calls = {
        f"POST:{value}"
        for name, value in vars(_connector_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }
    read_calls = {f"{method}:{path}" for method, path in _typed_reads_module.VENDOR_CALLS}
    setup_calls = {
        f"{method}:{path}"
        for module in (_profile_writes_module, _site_pairing_writes_module)
        for method, path in module.VENDOR_CALLS
    }
    mesh_calls = {f"{method}:{path}" for method, path in _service_mesh_writes_module.VENDOR_CALLS}
    extension_calls = {
        f"{method}:{path}" for method, path in _network_extension_module.VENDOR_CALLS
    }
    migration_calls = {
        f"{method}:{path}" for method, path in _migrations_module.MIGRATION_VENDOR_CALLS
    }
    restart_method, restart_path = _manager_restart_module.MIGRATION_QUERY_CALL
    restart_calls = {f"{restart_method}:{restart_path}"}
    main_api_calls = connector_calls | read_calls | setup_calls | mesh_calls | extension_calls
    return main_api_calls | migration_calls | restart_calls


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
        "GET:/hybridity/api/interconnect/computeProfiles",
        "POST:/hybridity/api/interconnect/computeProfiles",
        "GET:/hybridity/api/interconnect/computeProfiles/{computeProfileId}",
        "PUT:/hybridity/api/interconnect/computeProfiles/{computeProfileId}",
        "GET:/hybridity/api/networks",
        "POST:/hybridity/api/networks",
        "GET:/hybridity/api/networks/{networkId}",
        "PUT:/hybridity/api/networks/{networkId}",
        "POST:/hybridity/api/cloudConfigs",
        "DELETE:/hybridity/api/endpointPairing/{endpointId}",
        "POST:/hybridity/api/admin/certificates",
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
        "POST:/hybridity/api/migrations",
        "POST:/hybridity/api/mobility/groups",
        "PUT:/hybridity/api/mobility/groups",
        "POST:/hybridity/api/mobility/groups/validate",
        "POST:/hybridity/api/mobility/groups/start",
        "POST:/hybridity/api/mobility/groups/{mobilityGroupId}/schedule",
        "POST:/hybridity/api/mobility/migrations/cancel",
        "POST:/hybridity/api/mobility/migrations/forceCancel",
        "POST:/hybridity/api/v2/mobility-groups/query",
        "GET:/hybridity/api/v2/mobility-groups/{mobilityGroupId}/summary",
    }
    assert {"GET:/hybridity/docs"} == _DOCUMENTATION_OP_IDS


@pytest.mark.parametrize("spec_dir", _SPEC_DIRS)
def test_hcx_hand_coded_api_paths_are_served_by_the_pinned_file(spec_dir: str) -> None:
    """Every hand-coded hcx API call is served by the pinned file (skips until pinned)."""
    spec_path = require_shelf_spec(spec_dir, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_api_op_ids(), served, spec_label=f"{spec_dir}/{_SPEC_FILE}")


# ---------------------------------------------------------------------------
# The appliance management API (port 9443) of the HCX Manager setup
# ---------------------------------------------------------------------------

#: Expected pinned file name of the appliance management API. Broadcom
#: publishes a reference of it for HCX 9.1, but no file.
_APPLIANCE_SPEC_FILE = "appliance_management_api.openapi3.json"


def _appliance_op_ids(spec_dir: str) -> set[str]:
    """The setup's appliance calls; the 4.x key activation only for the 4.x shelf."""
    calls = set(_manager_setup_module.APPLIANCE_VENDOR_CALLS)
    if spec_dir != "hcx-4.11":
        calls -= _manager_setup_module.LEGACY_4X_CALLS
    return {f"{method}:{path}" for method, path in calls}


def test_hcx_appliance_op_id_manifest_is_pinned() -> None:
    """Pin the setup's calls so drift cannot silently shrink the reconcile."""
    assert _appliance_op_ids("hcx-4.11") == {
        "GET:/api/admin/global/config/vcenter",
        "POST:/api/admin/global/config/vcenter",
        "GET:/api/admin/global/config/lookupservice",
        "POST:/api/admin/global/config/lookupservice",
        "GET:/api/admin/global/config/nsx",
        "POST:/api/admin/global/config/nsx",
        "GET:/api/admin/global/config/location",
        "PUT:/api/admin/global/config/location",
        "GET:/api/admin/global/config/searchCities",
        "GET:/api/admin/global/config/roleMappings",
        "PUT:/api/admin/global/config/roleMappings",
        "GET:/api/admin/global/config/hcx",
        "POST:/api/admin/global/config/hcx",
        "GET:/license/hcx/activationStatus",
        "POST:/components/appengine",
        "GET:/components/appengine/status",
    }


@pytest.mark.parametrize("spec_dir", _SPEC_DIRS)
def test_hcx_appliance_paths_are_served_by_the_pinned_file(spec_dir: str) -> None:
    """Every setup call is served by the pinned appliance API file (skips until pinned)."""
    spec_path = require_shelf_spec(spec_dir, _APPLIANCE_SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(
        _appliance_op_ids(spec_dir), served, spec_label=f"{spec_dir}/{_APPLIANCE_SPEC_FILE}"
    )
