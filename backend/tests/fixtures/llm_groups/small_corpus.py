# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Small 5-op test corpus for the T3 LLM grouping pipeline.

Each op mirrors the shape :class:`EndpointDescriptorProto` carries
after T1's parser runs (op_id = METHOD:path; tags from OpenAPI). The
stub LLM responses below are tightened to the schemas T3 validates --
real model calls produce more varied prose but the shape contract is
identical.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = [
    "EXPECTED_ASSIGNMENT_BY_OP_ID",
    "PROTOS",
    "STUB_ASSIGNMENT_RESPONSE",
    "STUB_PROPOSE_RESPONSE",
    "make_protos",
]


def make_protos() -> list[EndpointDescriptorProto]:
    """Return a fresh list of 5 EndpointDescriptorProto rows."""
    return [
        EndpointDescriptorProto(
            op_id="GET:/api/vcenter/cluster",
            method="GET",
            path="/api/vcenter/cluster",
            summary="List clusters",
            tags=["Cluster"],
            parameter_schema={"type": "object", "properties": {}},
            safety_level="safe",
        ),
        EndpointDescriptorProto(
            op_id="GET:/api/vcenter/vm",
            method="GET",
            path="/api/vcenter/vm",
            summary="List VMs",
            tags=["VM"],
            parameter_schema={"type": "object", "properties": {}},
            safety_level="safe",
        ),
        EndpointDescriptorProto(
            op_id="POST:/api/vcenter/vm",
            method="POST",
            path="/api/vcenter/vm",
            summary="Create a VM",
            tags=["VM"],
            parameter_schema={"type": "object", "properties": {}},
            safety_level="caution",
        ),
        EndpointDescriptorProto(
            op_id="DELETE:/api/vcenter/vm/{vm}",
            method="DELETE",
            path="/api/vcenter/vm/{vm}",
            summary="Delete a VM",
            tags=["VM"],
            parameter_schema={"type": "object", "properties": {}},
            safety_level="dangerous",
        ),
        EndpointDescriptorProto(
            op_id="POST:/api/vcenter/vm/{vm}/snapshot",
            method="POST",
            path="/api/vcenter/vm/{vm}/snapshot",
            summary="Take a VM snapshot",
            tags=["VM", "Snapshot"],
            parameter_schema={"type": "object", "properties": {}},
            safety_level="caution",
        ),
    ]


PROTOS: Sequence[EndpointDescriptorProto] = make_protos()


#: Pass-1 stub response. The LLM is asked for 8-15 groups but a
#: small corpus reasonably proposes fewer; the test suite pins the
#: actual value but the production prompt's bounds are stricter. For
#: schema-validation tests we only need 2 groups to demonstrate.
STUB_PROPOSE_RESPONSE: str = json.dumps(
    [
        {
            "group_key": "inventory",
            "name": "Inventory",
            "when_to_use": (
                "Use when the operator needs to list, browse, or count "
                "infrastructure objects such as clusters and VMs without "
                "mutating them. Read-only enumeration of compute and "
                "container resources falls here."
            ),
        },
        {
            "group_key": "vm_lifecycle",
            "name": "VM Lifecycle",
            "when_to_use": (
                "Use when the operator is creating, deleting, or snapshotting "
                "virtual machines. Covers state mutations on the VM resource "
                "and any direct child resources such as snapshots."
            ),
        },
    ],
)

#: Pass-2 stub response keyed by op_id. The fifth op (snapshot) is
#: deliberately ambiguous between the two groups; the stub assigns
#: it to ``vm_lifecycle`` because the verb is POST (a mutation).
STUB_ASSIGNMENT_RESPONSE: str = json.dumps(
    {
        "GET:/api/vcenter/cluster": "inventory",
        "GET:/api/vcenter/vm": "inventory",
        "POST:/api/vcenter/vm": "vm_lifecycle",
        "DELETE:/api/vcenter/vm/{vm}": "vm_lifecycle",
        "POST:/api/vcenter/vm/{vm}/snapshot": "vm_lifecycle",
    },
)

#: Inverse of :data:`STUB_ASSIGNMENT_RESPONSE` -- the expected
#: ``op_id -> group_key`` mapping after parsing. Tests assert
#: against this; updating the stub above without updating this map
#: surfaces as a clear test failure rather than a silent drift.
EXPECTED_ASSIGNMENT_BY_OP_ID: dict[str, str] = {
    "GET:/api/vcenter/cluster": "inventory",
    "GET:/api/vcenter/vm": "inventory",
    "POST:/api/vcenter/vm": "vm_lifecycle",
    "DELETE:/api/vcenter/vm/{vm}": "vm_lifecycle",
    "POST:/api/vcenter/vm/{vm}/snapshot": "vm_lifecycle",
}
