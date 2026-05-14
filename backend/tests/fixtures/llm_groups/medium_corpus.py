# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Medium 50-op test corpus for the T3 LLM grouping pipeline.

The corpus is synthetic but follows the shape vCenter REST produces:
distinct top-level path families (cluster / vm / storage / network /
event / session), HTTP verb spread across the family. Drives the
documented LLM-call-count contract:

    1 (Pass-1) + ceil(50 / 50) = 2 LLM calls

The stub responses produce a clean 8-group taxonomy and a complete
op-to-group mapping; one synthetic op deliberately gets ``"none"`` so
the test suite covers the unassigned-row path without inflating the
fixture data by inventing an outlier op.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = [
    "EXPECTED_GROUP_KEYS",
    "EXPECTED_UNASSIGNED_OP_ID",
    "PROTOS",
    "STUB_ASSIGNMENT_RESPONSE",
    "STUB_PROPOSE_RESPONSE",
    "make_protos",
]

#: The eight group keys the Pass-1 stub returns. Tests assert that
#: each one ends up in the persisted ``operation_group`` rows.
EXPECTED_GROUP_KEYS: tuple[str, ...] = (
    "cluster",
    "events",
    "network",
    "performance",
    "session",
    "storage",
    "vm_inventory",
    "vm_lifecycle",
)

#: The single op the Pass-2 stub assigns to ``"none"``. The test
#: asserts this op ends up ``group_id=NULL`` and counted under
#: :attr:`GroupingResult.operations_unassigned`.
EXPECTED_UNASSIGNED_OP_ID: str = "GET:/api/vcenter/health/probe"


def _make_op(
    op_id: str,
    *,
    method: str,
    path: str,
    summary: str,
    tags: list[str],
    safety_level: str,
) -> EndpointDescriptorProto:
    return EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        summary=summary,
        tags=tags,
        parameter_schema={"type": "object", "properties": {}},
        safety_level=safety_level,  # type: ignore[arg-type]
    )


def make_protos() -> list[EndpointDescriptorProto]:
    """Return a fresh list of 50 EndpointDescriptorProto rows.

    Buckets:

    * 8 cluster ops, 12 vm-inventory ops, 8 vm-lifecycle ops
    * 6 storage ops, 6 network ops, 3 events ops, 3 performance ops
    * 3 session ops, 1 outlier (health probe -> unassigned)
    """
    ops: list[EndpointDescriptorProto] = []

    # Cluster: 8 ops
    for i in range(8):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/cluster-{i:02d}",
                method="GET",
                path=f"/api/vcenter/cluster-{i:02d}",
                summary=f"Read cluster object {i:02d}",
                tags=["Cluster"],
                safety_level="safe",
            ),
        )

    # VM inventory: 12 ops (mostly reads)
    for i in range(12):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/vm/inventory-{i:02d}",
                method="GET",
                path=f"/api/vcenter/vm/inventory-{i:02d}",
                summary=f"List VM inventory slice {i:02d}",
                tags=["VM"],
                safety_level="safe",
            ),
        )

    # VM lifecycle: 8 ops (mutations)
    lifecycle_verbs = (
        "POST",
        "DELETE",
        "POST",
        "DELETE",
        "POST",
        "DELETE",
        "POST",
        "DELETE",
    )
    for i, verb in enumerate(lifecycle_verbs):
        ops.append(
            _make_op(
                op_id=f"{verb}:/api/vcenter/vm/lifecycle-{i:02d}",
                method=verb,
                path=f"/api/vcenter/vm/lifecycle-{i:02d}",
                summary=f"Mutate VM lifecycle slice {i:02d}",
                tags=["VM"],
                safety_level="caution" if verb == "POST" else "dangerous",
            ),
        )

    # Storage: 6 ops
    for i in range(6):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/storage-{i:02d}",
                method="GET",
                path=f"/api/vcenter/storage-{i:02d}",
                summary=f"Read storage object {i:02d}",
                tags=["Datastore"],
                safety_level="safe",
            ),
        )

    # Network: 6 ops
    for i in range(6):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/network-{i:02d}",
                method="GET",
                path=f"/api/vcenter/network-{i:02d}",
                summary=f"Read network object {i:02d}",
                tags=["Network"],
                safety_level="safe",
            ),
        )

    # Events: 3 ops
    for i in range(3):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/event-{i:02d}",
                method="GET",
                path=f"/api/vcenter/event-{i:02d}",
                summary=f"Read event {i:02d}",
                tags=["Event"],
                safety_level="safe",
            ),
        )

    # Performance: 3 ops
    for i in range(3):
        ops.append(
            _make_op(
                op_id=f"GET:/api/vcenter/perf-{i:02d}",
                method="GET",
                path=f"/api/vcenter/perf-{i:02d}",
                summary=f"Read performance counter {i:02d}",
                tags=["PerfManager"],
                safety_level="safe",
            ),
        )

    # Session: 3 ops
    for i, verb in enumerate(("POST", "DELETE", "GET")):
        ops.append(
            _make_op(
                op_id=f"{verb}:/api/vcenter/session-{i:02d}",
                method=verb,
                path=f"/api/vcenter/session-{i:02d}",
                summary=f"Manage session {i:02d}",
                tags=["Session"],
                safety_level="caution",
            ),
        )

    # Outlier: 1 op the stub assigns to "none"
    ops.append(
        _make_op(
            op_id=EXPECTED_UNASSIGNED_OP_ID,
            method="GET",
            path="/api/vcenter/health/probe",
            summary="Liveness probe -- not part of any operator-facing taxonomy",
            tags=[],
            safety_level="safe",
        ),
    )

    assert len(ops) == 50, f"medium corpus should have 50 ops, got {len(ops)}"
    return ops


PROTOS: Sequence[EndpointDescriptorProto] = make_protos()


def _build_propose_response() -> str:
    """Return the deterministic Pass-1 response for the medium corpus."""
    proposals = [
        {
            "group_key": "cluster",
            "name": "Cluster",
            "when_to_use": (
                "Use when the operator is reading cluster topology or "
                "membership. Covers cluster enumeration and per-cluster "
                "configuration reads."
            ),
        },
        {
            "group_key": "events",
            "name": "Events",
            "when_to_use": (
                "Use when the operator needs the platform event stream -- "
                "audit-style logs of state changes, alarms, and notifications "
                "emitted by the vendor."
            ),
        },
        {
            "group_key": "network",
            "name": "Network",
            "when_to_use": (
                "Use when the operator is reading or troubleshooting "
                "virtual networking objects -- port groups, switches, and "
                "their attached state."
            ),
        },
        {
            "group_key": "performance",
            "name": "Performance",
            "when_to_use": (
                "Use when the operator is investigating performance "
                "counters or capacity metrics for compute, storage, or "
                "network resources."
            ),
        },
        {
            "group_key": "session",
            "name": "Session",
            "when_to_use": (
                "Use when the operator is managing API sessions -- "
                "authentication tokens, logout, and session metadata."
            ),
        },
        {
            "group_key": "storage",
            "name": "Storage",
            "when_to_use": (
                "Use when the operator is reading datastore or volume "
                "objects, capacity, or mount metadata. Covers read-only "
                "storage inspection."
            ),
        },
        {
            "group_key": "vm_inventory",
            "name": "VM Inventory",
            "when_to_use": (
                "Use when the operator is listing, browsing, or counting "
                "virtual machines without mutating them. Read-only "
                "enumeration only."
            ),
        },
        {
            "group_key": "vm_lifecycle",
            "name": "VM Lifecycle",
            "when_to_use": (
                "Use when the operator is creating, deleting, or otherwise "
                "mutating virtual machines. State-changing operations on "
                "the VM resource."
            ),
        },
    ]
    return json.dumps(proposals)


def _build_assignment_response() -> str:
    """Return the deterministic Pass-2 response keyed by op_id.

    The mapping covers every op in :func:`make_protos` except the
    outlier (``EXPECTED_UNASSIGNED_OP_ID``), which is assigned to
    ``"none"`` so the test suite exercises the unassigned path.
    """
    mapping: dict[str, str] = {}
    for proto in make_protos():
        op_id = proto.op_id
        if op_id == EXPECTED_UNASSIGNED_OP_ID:
            mapping[op_id] = "none"
            continue
        if op_id.startswith("GET:/api/vcenter/cluster"):
            mapping[op_id] = "cluster"
        elif op_id.startswith("GET:/api/vcenter/vm/inventory"):
            mapping[op_id] = "vm_inventory"
        elif "/api/vcenter/vm/lifecycle" in op_id:
            mapping[op_id] = "vm_lifecycle"
        elif op_id.startswith("GET:/api/vcenter/storage"):
            mapping[op_id] = "storage"
        elif op_id.startswith("GET:/api/vcenter/network"):
            mapping[op_id] = "network"
        elif op_id.startswith("GET:/api/vcenter/event"):
            mapping[op_id] = "events"
        elif op_id.startswith("GET:/api/vcenter/perf"):
            mapping[op_id] = "performance"
        elif "/api/vcenter/session" in op_id:
            mapping[op_id] = "session"
        else:
            mapping[op_id] = "none"
    return json.dumps(mapping)


STUB_PROPOSE_RESPONSE: str = _build_propose_response()
STUB_ASSIGNMENT_RESPONSE: str = _build_assignment_response()
