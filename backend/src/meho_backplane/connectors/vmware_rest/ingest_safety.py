# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor advertised by the VMware REST generic connector."""

from __future__ import annotations

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _requires_vmware_safety_floor(method: str, path: str) -> bool:
    """Identify VMware VM families whose generic writes require approval."""
    if method not in _WRITE_METHODS:
        return False
    canonical_path = path.split("?", 1)[0].removeprefix("/api")
    if canonical_path == "/vcenter/vm/{vm}/hardware":
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/hardware/"):
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/power/"):
        return True
    return (
        (method == "POST" and canonical_path == "/vcenter/vm")
        or (method == "DELETE" and canonical_path == "/vcenter/vm/{vm}")
        or canonical_path == "/vcenter/vm/{vm}/power"
    )


def vmware_rest_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Promote VM hardware, lifecycle, and power writes for VMware REST 9.0."""
    if version != "9.0" or not _requires_vmware_safety_floor(proto.method.upper(), proto.path):
        return proto
    return proto.model_copy(update={"safety_level": "dangerous", "requires_approval": True})


register_ingest_safety_floor(
    product="vmware",
    impl_id="vmware-rest",
    floor=vmware_rest_safety_floor,
)
