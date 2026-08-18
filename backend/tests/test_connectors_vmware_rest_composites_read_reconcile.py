# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for every read-composite sub-op path (#2986).

#2970's real-spec sweep covered the write composites' hand-coded paths
but left ``composites/_read.py`` guarded only piecemeal: the
portgroup-audit lane (``tests/acceptance/test_portgroup_audit_op_id_reconcile.py``)
asserted its own two op_ids and nothing asserted the rest -- which is
exactly how ``_OP_GET_CLUSTER_DRS = "GET:/vcenter/cluster/{cluster}/drs"``
(a path the pinned ``vcenter.yaml`` does not serve) survived the 11-op
fix as a recorded adjacent finding (PR #2974). This lane closes the gap
per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``): **every**
``_OP_*`` constant in ``_read.py`` is introspected live (never a
hardcoded mirror that can drift) and asserted against the pinned spec
that actually serves its dispatch leg.

The partition mirrors :func:`~...composites._read._read_sub_op`'s
dispatch branch byte-for-byte: ``GET`` legs are vSphere Automation
``/vcenter/*`` paths (mounted on ``/api`` / ``/rest``) and must be
served by ``vcenter-9.0/vcenter.yaml``; ``POST`` legs are vmomi
(VI-JSON) methods (routed through ``_post_vmomi_json`` onto the
``/sdk/vim25`` mount, #2466) and must be served by
``vcenter-9.0/vi-json.yaml``.

Both shelf-backed tests skip uniformly when the spec shelf is
unconfigured (the #2980 harness contract) and run for real in CI, where
the ``vcenter-9.0`` checkout is armed. The always-on pin test freezes
the corrected constant strings so a regression to the unserved DRS path
is caught even without the shelf (the portgroup lane's convention).
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites import _read
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)


def _swept_op_ids() -> dict[str, str]:
    """Every ``_OP_*`` op_id constant in ``_read``, introspected live."""
    ops: dict[str, str] = {}
    for name in dir(_read):
        if not name.startswith("_OP_"):
            continue
        value = getattr(_read, name)
        if isinstance(value, str):
            ops[name] = value
    return ops


def _manifest_union() -> set[str]:
    """Union of every ``_SUB_OPS_*`` tuple in ``_read``, introspected live."""
    union: set[str] = set()
    for name in dir(_read):
        if name.startswith("_SUB_OPS_"):
            union.update(getattr(_read, name))
    return union


def _partition_by_dispatch_leg() -> tuple[set[str], set[str]]:
    """Split the swept op_ids by ``_read_sub_op``'s dispatch branch.

    Returns ``(rest, vim)``: ``GET`` legs dispatch on the Automation
    mount and reconcile against ``vcenter.yaml``; every other method is
    a vmomi POST on the VI-JSON mount and reconciles against
    ``vi-json.yaml``.
    """
    rest: set[str] = set()
    vim: set[str] = set()
    for op_id in _swept_op_ids().values():
        method, _, _ = op_id.partition(":")
        (rest if method == "GET" else vim).add(op_id)
    return rest, vim


def test_every_op_constant_is_manifested_and_every_manifest_entry_is_a_constant() -> None:
    """The ``_OP_*`` sweep and the ``_SUB_OPS_*`` manifests agree exactly.

    The exhaustiveness contract behind #2986's "every op_id constant is
    asserted by a lane": a new ``_OP_*`` constant that never lands in a
    composite manifest, or a raw path literal smuggled into a
    ``_SUB_OPS_*`` tuple without a swept constant, both fail here -- so
    the shelf-backed lanes below always cover the full live surface.
    """
    swept = set(_swept_op_ids().values())
    manifested = _manifest_union()
    assert swept, "introspection found no _OP_* constants -- wiring broke"
    assert swept == manifested, (
        f"_OP_* constants not in any _SUB_OPS_* manifest: {sorted(swept - manifested)}; "
        f"manifest entries with no _OP_* constant: {sorted(manifested - swept)}"
    )


def test_read_sub_op_constants_are_the_reconciled_keys() -> None:
    """Pin the corrected op_id strings (sandbox-safe, always runs).

    The shelf-backed lanes below skip without the spec shelf; this pin
    runs everywhere and freezes the reconciled keys, so a regression to
    the unserved ``GET:/vcenter/cluster/{cluster}/drs`` path (the #2970
    adjacent finding this task fixed) is caught even in the sandbox.
    """
    rest, vim = _partition_by_dispatch_leg()
    assert rest == {
        "GET:/vcenter/cluster/{cluster}",
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
        "GET:/vcenter/network",
        "GET:/vcenter/vm",
    }
    assert vim == {
        "POST:/EventManager/{moId}/QueryEvents",
        "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric",
        "POST:/PerformanceManager/{moId}/QueryPerf",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    }
    # #2986: the DRS config read rides vim now -- no REST DRS constant
    # may reappear (the pinned vcenter.yaml serves no cluster DRS
    # resource at all).
    assert not hasattr(_read, "_OP_GET_CLUSTER_DRS")


def test_rest_read_sub_ops_are_served_by_the_pinned_vcenter_spec() -> None:
    """Every GET (Automation) sub-op is served by the pinned vcenter.yaml."""
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    served = openapi_served_op_ids(spec_path)
    rest, _ = _partition_by_dispatch_leg()
    assert_op_ids_served(rest, served, spec_label="vcenter-9.0/vcenter.yaml")


def test_vim_read_sub_ops_are_served_by_the_pinned_vi_json_spec() -> None:
    """Every POST (vmomi VI-JSON) sub-op is served by the pinned vi-json.yaml."""
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    served = openapi_served_op_ids(spec_path)
    _, vim = _partition_by_dispatch_leg()
    assert_op_ids_served(vim, served, spec_label="vcenter-9.0/vi-json.yaml")
