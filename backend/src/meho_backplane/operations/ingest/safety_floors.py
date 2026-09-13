# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry for connector-advertised ingested-operation safety floors."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = ["apply_safety_floor", "has_safety_floor", "register_ingest_safety_floor"]

IngestSafetyFloor = Callable[[str, EndpointDescriptorProto], EndpointDescriptorProto]
_FLOORS: dict[tuple[str, str], IngestSafetyFloor] = {}
_BUILTIN_FLOOR_MODULES = {
    ("vmware", "vmware-rest"): "meho_backplane.connectors.vmware_rest.ingest_safety",
}


def register_ingest_safety_floor(*, product: str, impl_id: str, floor: IngestSafetyFloor) -> None:
    """Register a connector-owned floor during the connector module import."""
    _FLOORS[(product, impl_id)] = floor


def _floor_for(product: str, impl_id: str) -> IngestSafetyFloor | None:
    key = (product, impl_id)
    if key not in _FLOORS and (module_name := _BUILTIN_FLOOR_MODULES.get(key)) is not None:
        import_module(module_name)
    return _FLOORS.get(key)


def has_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> bool:
    """Return whether *proto* is governed by a connector-specific floor."""
    floor = _floor_for(product, impl_id)
    return floor is not None and floor(version, proto) is not proto


def apply_safety_floor(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Apply the connector advertisement or leave a generic proto unchanged."""
    floor = _floor_for(product, impl_id)
    return floor(version, proto) if floor is not None else proto
