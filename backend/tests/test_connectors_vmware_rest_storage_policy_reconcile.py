# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the storage-policy composite's REST paths (#3494).

``vmware.composite.storage_policy.create`` orchestrates the tag substrate over
the vCenter REST paths ``POST /cis/tagging/category`` / ``/tag`` /
``/tag-association/{tagId}?action=attach`` and reads
``GET /vcenter/datastore`` / ``GET /vcenter/storage/policies``; the policy
create/delete itself rides the PBM **SOAP** API (``POST:/pbm/...`` governance
keys), which the pinned ``vcenter.yaml`` deliberately does *not* serve (there
is no REST path for PBM — the whole reason this op is typed). This lane, per
the spec-reconcile-guards standard (``docs/decisions/spec-reconcile-guards-standard.md``):

* **shape (always runs)** — every ``_storage_policy._OP_*`` constant is a
  well-formed ``METHOD:/path`` string, the REST paths match their expected
  literals (a typo drifting a tag path into a fiction fails here), and the PBM
  keys are correctly partitioned out of the REST-served set;
* **path existence (skips without the vendor-licensed shelf)** — every REST
  path the composite hits is served by the pinned ``vcenter.yaml`` (no
  fictional-path drift), while the PBM SOAP keys are asserted **absent** from
  the served set (proving they genuinely have no REST equivalent).
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites import _storage_policy
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

# The REST-Automation paths the composite hits (served by vcenter.yaml).
_EXPECTED_REST_OP_IDS = {
    "POST:/cis/tagging/category",
    "POST:/cis/tagging/tag",
    "POST:/cis/tagging/tag-association/{tagId}?action=attach",
    "GET:/vcenter/datastore",
    "GET:/vcenter/storage/policies",
}
# The PBM SOAP governance keys — no REST path exists for these (the gap #3494
# closes over SOAP), so vcenter.yaml must NOT serve them.
_EXPECTED_PBM_OP_IDS = {
    "POST:/pbm/ProfileManager/PbmCreate",
    "POST:/pbm/ProfileManager/PbmDelete",
}


def _declared_op_ids() -> set[str]:
    """Introspect every ``_OP_*`` op-id constant on the ``_storage_policy`` module."""
    ops: set[str] = set()
    for name in dir(_storage_policy):
        if not name.startswith("_OP_"):
            continue
        value = getattr(_storage_policy, name)
        if isinstance(value, str) and ":" in value:
            ops.add(value)
    return ops


def _partition(op_ids: set[str]) -> tuple[set[str], set[str]]:
    """Split declared op_ids into (REST-Automation, PBM SOAP) by path root."""
    rest, pbm = set(), set()
    for op_id in op_ids:
        _, _, path = op_id.partition(":")
        (pbm if path.lstrip("/").startswith("pbm/") else rest).add(op_id)
    return rest, pbm


def test_storage_policy_op_ids_are_well_formed_and_partitioned() -> None:
    """Shape gate (always runs): the op-id constants match the frozen surface.

    Guards against a typo drifting a tag path into a fiction and against a PBM
    key leaking into the REST-served set — the deliberate-change gate the
    shelf-backed lane below needs (it skips where the shelf is unprovisioned).
    """
    declared = _declared_op_ids()
    assert declared, "introspection found no _OP_* constants — wiring broke"
    for op_id in declared:
        method, _, path = op_id.partition(":")
        assert method in {"GET", "POST", "PATCH", "PUT", "DELETE"}, op_id
        assert path.startswith("/"), op_id
    rest, pbm = _partition(declared)
    assert rest == _EXPECTED_REST_OP_IDS
    assert pbm == _EXPECTED_PBM_OP_IDS


def test_rest_paths_are_served_by_the_pinned_vcenter_spec() -> None:
    """Path existence (skips without shelf): every REST path is real; PBM is not."""
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    rest, pbm = _partition(_declared_op_ids())
    assert_op_ids_served(rest, served, spec_label="vcenter.yaml")
    # The PBM SOAP keys have no REST equivalent — prove the spec serves none.
    assert not (pbm & served), (
        f"pinned vcenter.yaml unexpectedly serves PBM keys {sorted(pbm & served)}; "
        "PBM policy create/delete is SOAP-only (the gap #3494 closes)"
    )
