# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target version check for multi-step (composite) operations.

The catalog guard in :mod:`~meho_backplane.operations._catalog_guard` checks
only ingested operations. A composite operation runs its steps itself: on the
connector session of the target, or through ``dispatch_child`` with a
composite op id. Those steps never pass the catalog guard. So the composite
author owns the version fit of each step.

A connector states that fit once, for all composite operations of its
catalog, with
:attr:`~meho_backplane.connectors.base.Connector.composite_supported_version_range`.
The dispatcher checks it once, before the composite runs: the target's
version must be known and inside the range. A connector that sets no range
keeps the old behaviour (no check).
"""

from __future__ import annotations

from typing import Any

import structlog
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from meho_backplane.connectors import get_connector_v2, resolve_target_version
from meho_backplane.db.models import EndpointDescriptor

__all__ = ["composite_version_refusal"]

_log = structlog.get_logger(__name__)


def composite_version_refusal(descriptor: EndpointDescriptor, target: Any) -> str | None:
    """Return why *target* may not run the composite *descriptor*, or ``None``.

    ``None`` means the composite may run: the op is not a composite, there is
    no target, the catalog's connector states no range, or the target's
    version is inside the range. A target with no known version, or a
    version that cannot be read as a version number, is refused: without a
    version MEHO cannot say that the steps fit.
    """
    if descriptor.source_kind != "composite" or target is None:
        return None
    owner_class = get_connector_v2(descriptor.product, descriptor.version, descriptor.impl_id)
    declared = getattr(owner_class, "composite_supported_version_range", None)
    if not declared:
        return None
    try:
        spec = SpecifierSet(declared)
    except InvalidSpecifier:
        _log.warning(
            "composite_version_range_invalid",
            product=descriptor.product,
            version=descriptor.version,
            impl_id=descriptor.impl_id,
            composite_supported_version_range=declared,
        )
        return (
            f"the connector of this operation states a version range that MEHO "
            f"cannot read ({declared!r}), so MEHO cannot check the target's version"
        )
    target_version = resolve_target_version(target)
    if target_version is None:
        return (
            f"this operation works only on target versions {declared}, and the "
            "target has no known version; probe the target or set its version"
        )
    try:
        parsed = Version(target_version)
    except InvalidVersion:
        return (
            f"this operation works only on target versions {declared}, and the "
            f"target version {target_version!r} is not a version number MEHO can read"
        )
    if parsed in spec:
        return None
    return (
        f"this operation works only on target versions {declared}; "
        f"the target runs version {target_version}"
    )
