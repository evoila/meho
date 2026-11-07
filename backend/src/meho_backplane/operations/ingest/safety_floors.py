# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floors for ingested operations.

Four sources raise the parsed safety of an ingested operation:

* a connector-owned floor, registered in code by the connector module;
* the per-spec ``safety_overlay`` declared on the catalog row; and
* the op class of the operation: a read whose answer holds credentials
  (``credential_read``, see :mod:`meho_backplane.op_classes`) is at least
  ``caution`` and waits for a person's approval; and
* the caller-address marker: an operation with a parameter that holds an
  address MEHO connects to (``x-meho-caller-address: true``, see
  :mod:`meho_backplane.operations.caller_address`) is at least ``caution``
  and waits for a person's approval. A connector's spec corrections set
  the marker.

Each floor runs after the one before it and only raises: it never lowers a
safety level and never clears ``requires_approval``. The op class floor and
the caller-address floor are the last and lowest bounds. They need no
catalog row and no overlay list: a marker on the operation is enough.
"""

from __future__ import annotations

from collections.abc import Callable

from meho_backplane.broadcast.events import classify_op
from meho_backplane.operations.caller_address import (
    has_caller_address,
    raise_for_caller_address,
)
from meho_backplane.operations.ingest.catalog import safety_overlay_for
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto, SafetyLevel

__all__ = ["apply_safety_floor", "has_safety_floor", "register_ingest_safety_floor"]

IngestSafetyFloor = Callable[[str, EndpointDescriptorProto], EndpointDescriptorProto]
_FLOORS: dict[tuple[str, str], IngestSafetyFloor] = {}

_SAFETY_RANK: dict[SafetyLevel, int] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}


def register_ingest_safety_floor(*, product: str, impl_id: str, floor: IngestSafetyFloor) -> None:
    """Register a connector-owned floor during the connector module import."""
    _FLOORS[(product, impl_id)] = floor


def _apply_catalog_overlay(
    *, product: str, version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Raise *proto* by every matching catalog overlay rule.

    Returns *proto* itself when no rule matches, and a copy when at least one
    rule matches, so :func:`has_safety_floor` sees the op as governed.
    """
    rules = [
        rule
        for rule in safety_overlay_for(product, version)
        if rule.matches(proto.method, proto.path)
    ]
    if not rules:
        return proto
    safety_level = proto.safety_level
    requires_approval = proto.requires_approval
    for rule in rules:
        if _SAFETY_RANK[rule.safety_level] > _SAFETY_RANK[safety_level]:
            safety_level = rule.safety_level
        requires_approval = requires_approval or rule.requires_approval
    return proto.model_copy(
        update={"safety_level": safety_level, "requires_approval": requires_approval}
    )


def _apply_credential_read_floor(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    """Raise a credential read to at least ``caution`` with approval.

    Returns *proto* itself when the op is not a credential read, and a copy
    otherwise, so :func:`has_safety_floor` sees every credential read as
    governed.
    """
    if classify_op(proto.op_id) != "credential_read":
        return proto
    safety_level = proto.safety_level
    if _SAFETY_RANK[safety_level] < _SAFETY_RANK["caution"]:
        safety_level = "caution"
    return proto.model_copy(update={"safety_level": safety_level, "requires_approval": True})


def _apply_caller_address_floor(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    """Raise an op with a caller-address parameter to at least ``caution`` with approval.

    Returns *proto* itself when no parameter carries the marker, and a copy
    otherwise, so :func:`has_safety_floor` sees every marked op as governed.
    """
    if not has_caller_address(proto.parameter_schema):
        return proto
    safety_level, requires_approval = raise_for_caller_address(
        proto.parameter_schema,
        safety_level=proto.safety_level,
        requires_approval=proto.requires_approval,
    )
    return proto.model_copy(
        update={"safety_level": safety_level, "requires_approval": requires_approval}
    )


def has_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> bool:
    """Return whether any of the four floors governs *proto*."""
    applied = apply_safety_floor(product=product, version=version, impl_id=impl_id, proto=proto)
    return applied is not proto


def apply_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Apply the connector floor, the catalog overlay, then the two marker floors, to *proto*."""
    floor = _FLOORS.get((product, impl_id))
    floored = floor(version, proto) if floor is not None else proto
    overlaid = _apply_catalog_overlay(product=product, version=version, proto=floored)
    return _apply_caller_address_floor(_apply_credential_read_floor(overlaid))
