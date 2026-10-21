# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for ``vcenter.sso_admin.api_file.get``.

The read makes two vendor calls:

* ``GET`` on a path under the SSO Admin service folder ``/sso-adminserver/``.
  This is a documentation path, not in the API file: the SSO Admin service
  is SOAP, and an API file never lists the path that serves it. So the
  pinned ``vcenter.yaml`` files must NOT serve it.
* ``GET /api/appliance/system/version`` for the vCenter version and build.
  Both pinned ``vcenter.yaml`` files (8.0 and 9.0) must serve it.

Built like ``test_connectors_vmware_rest_storage_policy_reconcile.py``:

* **shape (always runs)** -- the two calls the module declares match the
  frozen list below;
* **path existence (skips without the vendor-licensed shelf)** -- checks
  both calls against the pinned ``vcenter.yaml`` of 8.0 and 9.0.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest import typed_ops_sso_admin_api_file as api_file
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

#: The mount the module's version path carries; the API file's paths
#: leave it out.
_API_MOUNT = "/api"

#: Served by vcenter.yaml.
_EXPECTED_SERVED = {"GET:/appliance/system/version"}

#: Documentation path, not in the API file (the SSO Admin service folder).
_EXPECTED_DOCUMENTATION_ONLY = {"GET:/sso-adminserver/"}

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def _served_call() -> str:
    path = api_file._APPLIANCE_VERSION_PATH
    assert path.startswith(_API_MOUNT + "/"), path
    return f"GET:{path[len(_API_MOUNT) :]}"


def _documentation_call() -> str:
    return f"GET:{api_file.SSO_ADMIN_FOLDER}"


def test_declared_calls_match_the_frozen_list() -> None:
    """Shape gate (always runs)."""
    assert {_served_call()} == _EXPECTED_SERVED
    assert {_documentation_call()} == _EXPECTED_DOCUMENTATION_ONLY
    # Every path the read accepts lies in the documentation-only folder.
    assert api_file.PATH_PATTERN.count(api_file.SSO_ADMIN_FOLDER) == 1


@pytest.mark.parametrize("shelf", _SHELVES)
def test_version_read_is_served_and_sso_folder_is_not(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(_EXPECTED_SERVED, served, spec_label=f"{shelf}/vcenter.yaml")
    sso_served = sorted(
        op_id for op_id in served if op_id.partition(":")[2].startswith("/sso-adminserver")
    )
    assert not sso_served, (
        f"pinned {shelf}/vcenter.yaml unexpectedly serves {sso_served}; the SSO Admin "
        "folder is a documentation path, not in the API file"
    )
