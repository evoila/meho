# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build-time guard: the Supervisor (WCP) composite op_ids resolve in the spec.

#3281. The ``vmware.composite.supervisor.*`` composites in
:mod:`~meho_backplane.connectors.vmware_rest.composites._supervisor` dispatch
three canonical ``METHOD:/path`` op_ids against the vCenter
namespace-management surface:

* enable  -- ``POST:/vcenter/namespace-management/supervisors/{cluster}``
  ``?action=enable_on_compute_cluster``
* disable -- ``POST:/vcenter/namespace-management/clusters/{cluster}?action=disable``
* status  -- ``GET:/vcenter/namespace-management/clusters/{cluster}``

Both writes ride the governed REST sub-op seam (``_write_sub_op`` ->
``enforce_subop_policy`` -> ``_post_json``); the status read rides the read
seam (``_read_sub_op``). Each op_id must be byte-for-byte the string the
ingest parser emits from the pinned ``vcenter.yaml``, or the sub-op would
mount a path vCenter does not serve.

Two tiers mirror the ``network.portgroup.audit`` reconcile guard
(:mod:`tests.acceptance.test_portgroup_audit_op_id_reconcile`):

* an **always-on** test pinning the constant *strings* so a drift (e.g. a
  respelt ``?action=`` verb, or a slip back to the deprecated
  ``clusters/{cluster}?action=enable`` form) is caught even in the sandbox
  where the vendor spec is unavailable;
* a **spec-backed** test that parses the canonical pinned ``vcenter.yaml``
  through the real :func:`parse_openapi` and asserts each op_id is emitted --
  it skips when the spec shelf is unconfigured and runs in CI.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _supervisor
from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import (
    VCENTER_SPEC_REASON,
    resolve_vcenter_yaml,
)


def test_supervisor_op_id_constants_are_the_reconciled_rest_keys() -> None:
    """Pin the exact op_id constants (sandbox-safe, always runs).

    Freezes the ``METHOD:/path`` keys the REST Automation ingest produces so
    a regression to the deprecated 7.x ``clusters/{cluster}?action=enable``
    form, or a drift in the ``?action=`` verb / ``{cluster}`` path var, goes
    red even without the spec shelf.
    """
    assert _supervisor._OP_ENABLE_ON_COMPUTE_CLUSTER == (
        "POST:/vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster"
    )
    assert (
        _supervisor._OP_DISABLE
        == "POST:/vcenter/namespace-management/clusters/{cluster}?action=disable"
    )
    assert _supervisor._OP_GET_CLUSTER == "GET:/vcenter/namespace-management/clusters/{cluster}"
    # The governed-subop manifests reference exactly those write op_ids.
    assert _supervisor._SUB_OPS_SUPERVISOR_ENABLE == (_supervisor._OP_ENABLE_ON_COMPUTE_CLUSTER,)
    assert _supervisor._SUB_OPS_SUPERVISOR_DISABLE == (_supervisor._OP_DISABLE,)


def test_supervisor_op_ids_resolve_against_pinned_vcenter_spec() -> None:
    """Every Supervisor composite op_id is emitted by parsing the pinned vcenter.yaml.

    Parser op_ids are byte-for-byte the strings written to
    ``endpoint_descriptor.op_id`` and the strings the governed sub-op seam +
    the read seam mount, so parser coverage == dispatch resolution for these
    composites. Skips when the spec shelf is unconfigured (sandbox); CI wires
    the env vars and runs it for real.
    """
    spec_path = resolve_vcenter_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)

    spec_text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_text,
    )
    ingested_op_ids = {row.op_id for row in rows}

    required = {
        _supervisor._OP_ENABLE_ON_COMPUTE_CLUSTER,
        _supervisor._OP_DISABLE,
        _supervisor._OP_GET_CLUSTER,
    }
    missing = required - ingested_op_ids
    assert not missing, (
        "vmware.composite.supervisor.* declares op_ids the vcenter.yaml ingest "
        f"does not emit: {sorted(missing)}. Either a constant drifted from the "
        "METHOD:/path form the parser produces, or the pinned spec revision no "
        "longer exposes the namespace-management resource under that key "
        "(re-check against the vSphere Automation REST API)."
    )
