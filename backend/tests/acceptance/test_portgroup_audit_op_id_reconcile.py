# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build-time guard: ``network.portgroup.audit`` sub-ops resolve in the spec.

#1602. The ``vmware.composite.network.portgroup.audit`` read composite
declares the L2 sub-ops it dispatches via ``_SUB_OPS_NETWORK_PORTGROUP_AUDIT``
in :mod:`~meho_backplane.connectors.vmware_rest.composites._read`. At
dispatch time
:func:`~...composites._preflight.preflight_l2_dependencies` looks each
sub-op_id up in ``endpoint_descriptor`` (keyed ``op_id = f"{method}:{path}"``
by the ingest pipeline) and raises
:class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
on a miss.

The original constants (``GET:/vcenter/network/distributed-switch`` +
``GET:/vcenter/network/distributed-portgroup``, both singular) never
resolved against a real ``vmware/9.0`` ingest:

* The vSphere Automation REST distributed-switch resource is **plural** --
  ``GET:/vcenter/network/distributed-switches`` (a preview feature). The
  singular spelling exists in no spec revision.
* There is **no** dedicated distributed-portgroup list resource at all;
  distributed portgroups are enumerated via the generic
  ``GET:/vcenter/network`` resource filtered to the
  ``DISTRIBUTED_PORTGROUP`` type. The singular ``distributed-portgroup``
  op_id was absent from every ingest.

This module is the durable guard the divergence slipped past: it parses
the **canonical pinned** ``vcenter.yaml`` (resolved via the same
spec-shelf env vars the G0.7 canary uses) into the real
:class:`EndpointDescriptorProto` set and asserts every
``_SUB_OPS_NETWORK_PORTGROUP_AUDIT`` op_id is present. Had it existed
before #508 shipped, the wrong constants would have gone red in CI.

Spec resolution mirrors the G0.7 canary
(:mod:`tests.acceptance._vcenter_spec`): the vendor-licensed vSphere
specs live in the operator's separate spec-shelf repo, not this chassis
repo, so the spec-backed assertion **skips** when no spec source is
configured and runs in CI where ``MEHO_VCENTER_OPENAPI_VCENTER`` (or the
local-dev ``MEHO_CONSUMER_DOCS_ROOT``) is wired. CI green is the
operator-visible signal.

A second, **always-on** test pins the corrected constant *strings*
themselves so a future edit that re-introduces a singular path (or
otherwise drifts the keys) is caught even in the sandbox where the spec
file is unavailable.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _read
from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import (
    VCENTER_SPEC_REASON,
    resolve_vcenter_yaml,
)


def test_portgroup_audit_sub_op_constants_are_the_reconciled_rest_keys() -> None:
    """Pin the corrected op_id constants (sandbox-safe, always runs).

    The spec-backed guard below skips when the vendor specs are not
    configured; this test runs everywhere and freezes the exact
    ``METHOD:/path`` keys the REST Automation ingest produces, so a
    regression to the singular ``distributed-switch`` /
    ``distributed-portgroup`` spelling (the #1602 defect) is caught
    even without the spec shelf.
    """
    assert _read._OP_LIST_DVS == "GET:/vcenter/network/distributed-switches"
    assert _read._OP_LIST_NETWORK == "GET:/vcenter/network"
    assert _read._SUB_OPS_NETWORK_PORTGROUP_AUDIT == (
        "GET:/vcenter/network/distributed-switches",
        "GET:/vcenter/network",
        "GET:/vcenter/vm",
    )


def test_portgroup_audit_sub_ops_resolve_against_pinned_vcenter_spec() -> None:
    """Every audit sub-op_id is emitted by parsing the canonical vcenter.yaml.

    Parses the pinned ``vcenter.yaml`` through the real
    :func:`parse_openapi` and asserts each
    ``_SUB_OPS_NETWORK_PORTGROUP_AUDIT`` op_id appears in the emitted
    descriptor set. Parser op_ids are byte-for-byte the strings written
    to ``endpoint_descriptor.op_id`` and the strings
    :func:`preflight_l2_dependencies` queries on, so parser coverage ==
    pre-flight resolution for this composite.

    Skips when the spec shelf is unconfigured (sandbox); CI wires the
    env vars and runs it for real.
    """
    spec_path = resolve_vcenter_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)

    spec_text = spec_path.read_text(encoding="utf-8")
    # ``content=`` feeds the bytes verbatim (the https-only SSRF guard
    # applies only to the URL-fetch path); the URI arg is just the audit
    # label. Mirrors the CLI's docs:/file:// upload path.
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_text,
    )
    ingested_op_ids = {row.op_id for row in rows}

    required = set(_read._SUB_OPS_NETWORK_PORTGROUP_AUDIT)
    missing = required - ingested_op_ids
    assert not missing, (
        "vmware.composite.network.portgroup.audit declares sub-op_ids the "
        "vcenter.yaml ingest does not emit: "
        f"{sorted(missing)}. Either a _SUB_OPS_NETWORK_PORTGROUP_AUDIT "
        "constant drifted from the METHOD:/path form the parser produces, "
        "or the pinned spec revision no longer exposes the resource under "
        "that key (re-check against the vSphere Automation REST API)."
    )
