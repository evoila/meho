# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floors for ingested operations.

Two sources raise the parsed safety of an ingested operation:

* a connector-owned floor, registered in code by the connector module; and
* the per-spec ``safety_overlay`` declared on the catalog row.

The catalog overlay runs after the connector floor and only raises: it never
lowers a safety level and never clears ``requires_approval``.
"""

from __future__ import annotations

from collections.abc import Callable

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


def has_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> bool:
    """Return whether *proto* is governed by a connector floor or a catalog overlay."""
    applied = apply_safety_floor(product=product, version=version, impl_id=impl_id, proto=proto)
    return applied is not proto


def apply_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Apply the connector floor, then the catalog overlay, to *proto*."""
    floor = _FLOORS.get((product, impl_id))
    floored = floor(version, proto) if floor is not None else proto
    return _apply_catalog_overlay(product=product, version=version, proto=floored)
