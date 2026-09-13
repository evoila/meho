# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vmware-rest-8.0 (vSphere 8.0 U3) shipped-surface reconcile lane (#3569) — #2980 harness.

Two guards for the second versioned ``vmware-rest`` catalog:

**Always-on surface pin (manifest-pin analog).** The 8.0 U3 catalog surfaces the
same governed inventory-read core as the 9.0 catalog, so the shipped 8.0 minimal
spec (``vmware_rest_8_0_minimal.yaml``) must parse cleanly and emit exactly the
same served ``METHOD:/path`` op_id set as the shipped 9.0 minimal
(``vmware_rest_minimal.yaml``). Editing one shipped spec without the other trips
this lane loudly. Runs unconditionally in the required unit sweep (no DB /
embeddings / containers), the same shape as the ``sddc-vcf5`` manifest-pin lane
for a hand-coded surface whose full vendor OpenAPI is not committed here.

**Shelf-gated strengthening (armed, skips until provisioned).** When the pinned
8.0 U3 vendor ``vcenter.yaml`` lands on the private spec shelf
(``vcenter-8.0/vcenter.yaml`` under ``MEHO_CONSUMER_DOCS_ROOT``, the same
convention as ``vcenter-9.0/``), the shipped minimal's inventory-read op_ids must
be a subset of the vendor-served set -- proving the ops MEHO surfaces really
exist in the vendor 8.0 U3 catalog. The ``/api/about`` fingerprint-probe path is
excluded: it is the ESXi/host REST surface MEHO probes, not part of the vCenter
Automation ``vcenter.yaml`` (its only about is ``/api/vcenter/phm/about``). Skips
with the uniform reason until the shelf is wired -- a red lane there is a real
drift finding, not harness noise.

The pinned vendor 8.0 U3 catalog artifact is not yet acquired (the public
``vmware/vcf-api-specs`` repo is tagged 9.0/9.1 only, and the Broadcom Developer
Portal / appliance-served specs need authenticated retrieval); see the
``vsphere-8.x`` artifact evidence gap in
``docs/compatibility/vcf-api-contract-manifest.yaml``. The shelf-gated half arms
automatically the day that artifact is pinned to ``vcenter-8.0/``.
"""

from __future__ import annotations

from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import load_spec_resource
from tests._spec_shelf import openapi_served_op_ids, require_shelf_spec

_SHIPPED_8_0_SPEC = "vmware_rest_8_0_minimal.yaml"
_SHIPPED_9_0_SPEC = "vmware_rest_minimal.yaml"

#: The vCenter Automation ``vcenter.yaml`` has no ``/api/about`` -- that path is
#: the ESXi/host REST surface MEHO probes for fingerprinting. Excluded from the
#: vendor-served subset check (the only shipped op not sourced from the
#: Automation spec).
_FINGERPRINT_PROBE_OP_ID = "GET:/api/about"

#: Private-shelf location for the pinned 8.0 U3 vendor catalog, mirroring the
#: ``vcenter-9.0/`` convention (see tests/acceptance/_vcenter_spec.py).
_SHELF_DIR = "vcenter-8.0"
_SHELF_FILE = "vcenter.yaml"


def _shipped_served_op_ids(spec_resource: str) -> set[str]:
    """Served ``METHOD:/path`` op_ids of a shipped, package-data minimal spec."""
    content = load_spec_resource(spec_resource)
    rows = parse_openapi(
        f"spec:{spec_resource}",
        spec_source=f"spec:{spec_resource}",
        content=content,
    )
    return {row.op_id for row in rows}


def test_shipped_8_0_minimal_matches_9_0_governed_read_core() -> None:
    """The 8.0 U3 shipped minimal surfaces exactly the 9.0 governed read core."""
    served_8_0 = _shipped_served_op_ids(_SHIPPED_8_0_SPEC)
    assert served_8_0, "the 8.0 U3 shipped minimal parsed to zero served ops"
    served_9_0 = _shipped_served_op_ids(_SHIPPED_9_0_SPEC)
    assert served_8_0 == served_9_0, (
        "the 8.0 U3 and 9.0 shipped minimal specs must surface the same governed "
        "inventory-read core; update both in lock-step"
    )


def test_shipped_8_0_minimal_is_subset_of_pinned_vendor_catalog() -> None:
    """Shipped 8.0 read ops exist in the pinned vendor 8.0 U3 vcenter.yaml (shelf-gated)."""
    spec_path = require_shelf_spec(_SHELF_DIR, _SHELF_FILE)
    vendor_served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    shipped = _shipped_served_op_ids(_SHIPPED_8_0_SPEC) - {_FINGERPRINT_PROBE_OP_ID}
    missing = shipped - vendor_served
    assert not missing, (
        f"shipped 8.0 minimal declares op_ids absent from the pinned vendor 8.0 U3 "
        f"catalog: {sorted(missing)}"
    )
