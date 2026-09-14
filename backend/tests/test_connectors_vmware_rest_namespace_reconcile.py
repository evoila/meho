# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the vSphere Namespace composite's REST paths (#3502).

``vmware.composite.namespace.create`` / ``.delete`` orchestrate three pure
vCenter REST paths -- ``POST /vcenter/namespaces/instances/v2`` (the v2 create
spec), ``DELETE /vcenter/namespaces/instances/{namespace}`` (delete by name),
and the read-back ``GET /vcenter/namespaces/instances/{namespace}`` (get by
name -- there is no ``/v2/`` GET-by-name variant; the v2 form is create/list
only). No SOAP / vim seam is involved, so unlike the storage-policy lane there
is no PBM partition -- every declared op_id must be REST-served by the pinned
``vcenter.yaml``. Per the spec-reconcile-guards standard
(``docs/decisions/spec-reconcile-guards-standard.md``):

* **shape (always runs)** -- every ``_namespace._OP_*`` constant is a
  well-formed ``METHOD:/path`` string and matches the frozen expected surface
  (a typo drifting a path into a fiction, or an accidental ``/v2/`` GET-by-name,
  fails here);
* **path existence (skips without the vendor-licensed shelf)** -- every path
  the composite hits is served by the pinned ``vcenter.yaml`` (no
  fictional-path drift).
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites import _namespace
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

# The vCenter REST paths the two composites hit (all served by vcenter.yaml).
_EXPECTED_OP_IDS = {
    "POST:/vcenter/namespaces/instances/v2",
    "DELETE:/vcenter/namespaces/instances/{namespace}",
    "GET:/vcenter/namespaces/instances/{namespace}",
}


def _declared_op_ids() -> set[str]:
    """Introspect every ``_OP_*`` op-id constant on the ``_namespace`` module."""
    ops: set[str] = set()
    for name in dir(_namespace):
        if not name.startswith("_OP_"):
            continue
        value = getattr(_namespace, name)
        if isinstance(value, str) and ":" in value:
            ops.add(value)
    return ops


def test_namespace_op_ids_are_well_formed_and_frozen() -> None:
    """Shape gate (always runs): the op-id constants match the frozen surface.

    Guards against a typo drifting a path into a fiction and against an
    accidental ``/v2/`` GET-by-name variant — the deliberate-change gate the
    shelf-backed lane below needs (it skips where the shelf is unprovisioned).
    """
    declared = _declared_op_ids()
    assert declared, "introspection found no _OP_* constants — wiring broke"
    for op_id in declared:
        method, _, path = op_id.partition(":")
        assert method in {"GET", "POST", "PATCH", "PUT", "DELETE"}, op_id
        assert path.startswith("/"), op_id
    assert declared == _EXPECTED_OP_IDS


def test_namespace_paths_are_served_by_the_pinned_vcenter_spec() -> None:
    """Path existence (skips without shelf): every path is served by vcenter.yaml."""
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(_declared_op_ids(), served, spec_label="vcenter.yaml")
