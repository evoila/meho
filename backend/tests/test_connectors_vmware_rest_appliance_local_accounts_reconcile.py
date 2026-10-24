# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the vCenter appliance local-account operations.

The four ``vmware.appliance.local_account.*`` operations call three vendor
REST paths: the list, the read of one account, and the ``PATCH`` of one
account. Per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``):

* **shape (always runs)** -- the declared ``METHOD:/path`` set matches the
  frozen surface;
* **paths (skips without the shelf)** -- the pinned 8.0 and 9.0
  ``vcenter.yaml`` files serve every path;
* **body (skips without the shelf)** -- in the pinned 9.0 ``vcenter.yaml``
  the ``PATCH`` body is the flat ``UpdateConfig`` (no ``config`` wrapper)
  and has every field the operations send. The 8.0 file declares the same
  ``UpdateConfig`` through ``components/requestBodies``, which the ingest
  parser does not fold into the body schema, so the body check reads the 9.0
  file only (as the write-body lane does).
"""

from __future__ import annotations

import functools

import pytest

from meho_backplane.connectors.vmware_rest.appliance_local_accounts import (
    POLICY_FIELDS,
    VENDOR_OP_IDS,
)
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_EXPECTED_OP_IDS = {
    "GET:/appliance/local-accounts",
    "GET:/appliance/local-accounts/{username}",
    "PATCH:/appliance/local-accounts/{username}",
}
_PATCH = "PATCH:/appliance/local-accounts/{username}"
_SPEC_FILE = "vcenter.yaml"


@functools.cache
def _served(product_dir: str) -> frozenset[str]:
    spec_path = require_shelf_spec(product_dir, _SPEC_FILE)
    return frozenset(openapi_served_op_ids(spec_path, spec_source=f"spec:{_SPEC_FILE}"))


def test_declared_vendor_calls_are_frozen() -> None:
    assert set(VENDOR_OP_IDS) == _EXPECTED_OP_IDS


@pytest.mark.parametrize("product_dir", ["vcenter-9.0", "vcenter-8.0"])
def test_paths_are_served_by_the_pinned_vcenter_spec(product_dir: str) -> None:
    assert_op_ids_served(
        VENDOR_OP_IDS, _served(product_dir), spec_label=f"{product_dir}/{_SPEC_FILE}"
    )


def test_patch_body_is_the_flat_update_config() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", _SPEC_FILE)
    body_props = openapi_request_body_props(spec_path, spec_source=f"spec:{_SPEC_FILE}")
    assert _PATCH in body_props, f"vcenter-9.0/{_SPEC_FILE} has no body for {_PATCH}"
    props = body_props[_PATCH]
    assert "config" not in props
    missing = {"password", *POLICY_FIELDS} - props
    assert not missing, f"vcenter-9.0/{_SPEC_FILE} UpdateConfig lacks {sorted(missing)}"
