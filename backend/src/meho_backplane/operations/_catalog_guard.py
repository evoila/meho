# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-catalog target guard for ingested descriptors, with sibling fallback.

A connector can opt its ingested catalog into a target check
(:attr:`~meho_backplane.connectors.base.Connector.enforces_catalog_target_compatibility`).
The dispatcher asks the catalog that owns the looked-up descriptor whether
it can serve the target. When it cannot, the call is normally rejected
with ``unqualified_target_version``.

One implementation can ship several versioned catalogs, for example
``vmware-rest-9.0`` (9.x targets) and ``vmware-rest-8.0`` (8.0.x targets).
A caller that names one catalog for a target the other catalog serves gets
the other catalog's descriptor instead (#3600), but only when:

* the resolver picks that other catalog for the target (same product and
  implementation, different version), so the resolver's usual tie-break
  decides when more than one catalog could match;
* that catalog's own guard accepts the target; and
* the same ``op_id`` exists there as an **enabled** ingested descriptor
  the caller can see.

Otherwise the rejection stands, unchanged. Typed and composite
descriptors never reach this code: the guard only applies to
``source_kind == "ingested"``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import (
    OperationResult,
    get_connector_v2,
    resolve_connector_or_label,
    resolve_target_version,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import audit_rejection_safe
from meho_backplane.operations._errors import result_unqualified_target_version
from meho_backplane.operations._lookup import lookup_descriptor

__all__ = ["CatalogSelection", "select_catalog_for_target"]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CatalogSelection:
    """The descriptor the dispatch runs, and the connector it runs on.

    ``connector_id`` names the catalog of ``descriptor``. It differs from
    the caller's id only when the sibling fallback was taken.
    ``connector_class`` is the class the resolver picked for the target,
    or ``None`` when the guard did not apply (the dispatcher resolves the
    connector later, as before).
    """

    descriptor: EndpointDescriptor
    connector_id: str
    connector_class: type[Connector] | None


async def select_catalog_for_target(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
    params_hash: str,
    started: float,
) -> CatalogSelection | OperationResult:
    """Run the catalog target guard; fall back to a qualified sibling catalog.

    Returns a :class:`CatalogSelection` when the dispatch may go on, or the
    audited ``unqualified_target_version`` result when it may not.
    """
    owner_class = get_connector_v2(descriptor.product, descriptor.version, descriptor.impl_id)
    if (
        descriptor.source_kind != "ingested"
        or owner_class is None
        or not owner_class.enforces_catalog_target_compatibility
    ):
        return CatalogSelection(descriptor, connector_id, None)

    resolved_class, _label, _message = resolve_connector_or_label(target)
    target_product = getattr(target, "product", None)
    if not isinstance(target_product, str) or not target_product:
        target_product = None
    target_version = resolve_target_version(target)
    incompatibility = owner_class.catalog_target_incompatibility(
        descriptor_source_kind=descriptor.source_kind,
        target_product=target_product,
        target_version=target_version,
        selected_target_connector=resolved_class,
    )
    if incompatibility is None:
        return CatalogSelection(descriptor, connector_id, resolved_class)

    sibling = await _qualified_sibling_descriptor(
        operator=operator,
        descriptor=descriptor,
        resolved_class=resolved_class,
        target_product=target_product,
        target_version=target_version,
    )
    if sibling is not None:
        sibling_connector_id = f"{sibling.impl_id}-{sibling.version}"
        _log.info(
            "catalog_sibling_fallback",
            op_id=descriptor.op_id,
            requested_connector_id=connector_id,
            resolved_connector_id=sibling_connector_id,
            target_version=target_version,
        )
        return CatalogSelection(sibling, sibling_connector_id, resolved_class)

    duration_ms = (time.monotonic() - started) * 1000.0
    await audit_rejection_safe(
        audit_id=uuid.uuid4(),
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status="unqualified_target_version",
        duration_ms=duration_ms,
    )
    return result_unqualified_target_version(
        descriptor.op_id,
        reason=incompatibility,
        target_product=target_product,
        target_version=target_version,
        duration_ms=duration_ms,
    )


async def _qualified_sibling_descriptor(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    resolved_class: type[Connector] | None,
    target_product: str | None,
    target_version: str | None,
) -> EndpointDescriptor | None:
    """Return the sibling catalog's enabled descriptor for the same op, or ``None``.

    The sibling is the catalog the resolver picked for the target. It must
    belong to the same ``(product, impl_id)`` family, be registered under
    its own version, opt into the same guard, and accept the target.
    """
    if resolved_class is None:
        return None
    sibling_version = resolved_class.version
    if (
        resolved_class.product != descriptor.product
        or resolved_class.impl_id != descriptor.impl_id
        or not sibling_version
        or sibling_version == descriptor.version
        or not resolved_class.enforces_catalog_target_compatibility
        or get_connector_v2(descriptor.product, sibling_version, descriptor.impl_id)
        is not resolved_class
    ):
        return None
    if (
        resolved_class.catalog_target_incompatibility(
            descriptor_source_kind=descriptor.source_kind,
            target_product=target_product,
            target_version=target_version,
            selected_target_connector=resolved_class,
        )
        is not None
    ):
        return None
    sibling = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=descriptor.product,
        version=sibling_version,
        impl_id=descriptor.impl_id,
        op_id=descriptor.op_id,
    )
    if sibling is None or sibling.source_kind != "ingested":
        return None
    return sibling
