# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-catalog target guard for ingested descriptors, with sibling fallback.

A connector can opt its ingested catalog into a target check
(:attr:`~meho_backplane.connectors.base.Connector.enforces_catalog_target_compatibility`).
The dispatcher asks the catalog that owns the looked-up descriptor whether
it can serve the target. When it cannot, the call is normally rejected
with ``unqualified_target_version``.

One implementation can ship several versioned catalogs, for example
an ``<impl_id>-9.0`` catalog (9.x targets) and an ``<impl_id>-8.0``
catalog (8.0.x targets).
A caller that names one catalog for a target the other catalog serves gets
the other catalog's descriptor instead (#3600), but only when:

* the resolver picks that other catalog for the target (same product and
  implementation, different version), so the resolver's usual tie-break
  decides when more than one catalog could match;
* that catalog's own guard accepts the target; and
* the same ``op_id`` exists there as an **enabled** ingested descriptor
  the caller can see.

Otherwise the rejection stands. Its reason is the connector's own, with
one exception (#5069): when the sibling serves the target but has no
enabled op of that ``op_id``, the reason says the operation is not
available for the target and points at ``search_operations`` on the
sibling. It does not tell the caller to use the sibling, which would
answer ``unknown_op``. The preview path uses the
same choice (:func:`choose_catalog_for_target`, which writes no audit
row), so a ``preview_hash`` taken with the named catalog matches the
dispatch that runs on the sibling. Typed and composite
descriptors never reach this code: the guard only applies to
``source_kind == "ingested"``.

The fallback is for a fresh call only. An approval request stores the
catalog that was checked by policy and shown to the approver. When that
request resumes (``allow_sibling=False``), the call stays on that catalog:
if the target's detected version moved meanwhile and the catalog no longer
fits, the resume is refused with ``unqualified_target_version`` and the
caller must make a new request. Switching catalogs there would run an
operation the approver never saw, with the policy gate skipped.
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
from meho_backplane.operations._audit import audit_rejection_safe, with_audit_receipt
from meho_backplane.operations._errors import result_unqualified_target_version
from meho_backplane.operations._lookup import lookup_descriptor

__all__ = [
    "CatalogRefusal",
    "CatalogSelection",
    "catalog_refuses_target",
    "choose_catalog_for_target",
    "select_catalog_for_target",
]

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


@dataclass(frozen=True)
class CatalogRefusal:
    """Why neither the named catalog nor a sibling can serve the target."""

    reason: str
    target_product: str | None
    target_version: str | None


async def choose_catalog_for_target(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
    allow_sibling: bool = True,
) -> CatalogSelection | CatalogRefusal:
    """Pick the catalog that serves *target*, without writing an audit row.

    Returns a :class:`CatalogSelection` (the named catalog, or a qualified
    sibling) or a :class:`CatalogRefusal`. Both dispatch and preview use
    this, so a preview binds the same catalog the dispatch runs on.
    ``allow_sibling=False`` pins the named catalog: a refusal is returned
    even when a sibling would fit (the approval-resume path).

    When the sibling catalog serves the target but does not have the op
    enabled, the refusal says so and points at ``search_operations`` on
    that sibling (#5069). It does not repeat the connector's own hint to
    use the sibling, because the sibling would answer ``unknown_op``.
    """
    owner_class = _guarded_owner_class(
        product=descriptor.product,
        version=descriptor.version,
        impl_id=descriptor.impl_id,
        source_kind=descriptor.source_kind,
    )
    if owner_class is None:
        return CatalogSelection(descriptor, connector_id, None)

    check = _check_target(owner_class, descriptor.source_kind, target)
    if check.incompatibility is None:
        return CatalogSelection(descriptor, connector_id, check.resolved_class)
    refusal = CatalogRefusal(check.incompatibility, check.target_product, check.target_version)
    if not allow_sibling:
        return refusal

    sibling_class = _qualified_sibling_class(descriptor=descriptor, check=check)
    if sibling_class is None:
        return refusal
    sibling_connector_id = f"{descriptor.impl_id}-{sibling_class.version}"
    sibling = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=descriptor.product,
        version=sibling_class.version,
        impl_id=descriptor.impl_id,
        op_id=descriptor.op_id,
    )
    if sibling is None:
        return CatalogRefusal(
            _not_on_sibling_reason(sibling_connector_id, check),
            check.target_product,
            check.target_version,
        )
    if sibling.source_kind != "ingested":
        return refusal
    _log.info(
        "catalog_sibling_fallback",
        op_id=descriptor.op_id,
        requested_connector_id=connector_id,
        resolved_connector_id=sibling_connector_id,
        target_version=check.target_version,
    )
    return CatalogSelection(sibling, sibling_connector_id, check.resolved_class)


def catalog_refuses_target(
    *,
    product: str,
    version: str,
    impl_id: str,
    source_kind: str,
    target: Any,
) -> bool:
    """True when the catalog of *(product, version, impl_id)* refuses *target*.

    The same check :func:`choose_catalog_for_target` makes first, for an op
    of *source_kind* in that catalog. It looks at no sibling catalog. The
    ``unknown_op`` error uses it to leave out connectors that would refuse
    the caller's target (#5069).
    """
    owner_class = _guarded_owner_class(
        product=product, version=version, impl_id=impl_id, source_kind=source_kind
    )
    if owner_class is None:
        return False
    return _check_target(owner_class, source_kind, target).incompatibility is not None


async def select_catalog_for_target(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
    params_hash: str,
    started: float,
    allow_sibling: bool = True,
) -> CatalogSelection | OperationResult:
    """Run the catalog target guard; fall back to a qualified sibling catalog.

    Returns a :class:`CatalogSelection` when the dispatch may go on, or the
    audited ``unqualified_target_version`` result when it may not.
    ``allow_sibling=False`` disables the fallback (see the module docstring).
    """
    choice = await choose_catalog_for_target(
        operator=operator,
        descriptor=descriptor,
        connector_id=connector_id,
        target=target,
        allow_sibling=allow_sibling,
    )
    if isinstance(choice, CatalogSelection):
        return choice

    duration_ms = (time.monotonic() - started) * 1000.0
    receipt = await audit_rejection_safe(
        audit_id=uuid.uuid4(),
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status="unqualified_target_version",
        duration_ms=duration_ms,
    )
    return with_audit_receipt(
        result_unqualified_target_version(
            descriptor.op_id,
            reason=choice.reason,
            target_product=choice.target_product,
            target_version=choice.target_version,
            duration_ms=duration_ms,
        ),
        receipt,
    )


@dataclass(frozen=True)
class _TargetCheck:
    """What one catalog's guard said about a target."""

    resolved_class: type[Connector] | None
    target_product: str | None
    target_version: str | None
    incompatibility: str | None


def _guarded_owner_class(
    *, product: str, version: str, impl_id: str, source_kind: str
) -> type[Connector] | None:
    """Return the catalog's class when its guard applies to an op of *source_kind*."""
    if source_kind != "ingested":
        return None
    owner_class = get_connector_v2(product, version, impl_id)
    if owner_class is None or not owner_class.enforces_catalog_target_compatibility:
        return None
    return owner_class


def _check_target(owner_class: type[Connector], source_kind: str, target: Any) -> _TargetCheck:
    """Ask *owner_class* whether its catalog can serve *target*."""
    resolved_class, _label, _message = resolve_connector_or_label(target)
    target_product = getattr(target, "product", None)
    if not isinstance(target_product, str) or not target_product:
        target_product = None
    target_version = resolve_target_version(target)
    incompatibility = owner_class.catalog_target_incompatibility(
        descriptor_source_kind=source_kind,
        target_product=target_product,
        target_version=target_version,
        selected_target_connector=resolved_class,
    )
    return _TargetCheck(resolved_class, target_product, target_version, incompatibility)


def _qualified_sibling_class(
    *, descriptor: EndpointDescriptor, check: _TargetCheck
) -> type[Connector] | None:
    """Return the sibling catalog's class when it serves the target, or ``None``.

    The sibling is the catalog the resolver picked for the target. It must
    belong to the same ``(product, impl_id)`` family, be registered under
    its own version, opt into the same guard, and accept the target.
    """
    resolved_class = check.resolved_class
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
            target_product=check.target_product,
            target_version=check.target_version,
            selected_target_connector=resolved_class,
        )
        is not None
    ):
        return None
    return resolved_class


def _not_on_sibling_reason(sibling_connector_id: str, check: _TargetCheck) -> str:
    """Reason for a refusal when the sibling serves the target but lacks the op."""
    target = " ".join(part for part in (check.target_product, check.target_version) if part)
    return (
        f"this operation is not available for {target or 'this target'}: "
        f"connector {sibling_connector_id} serves this target and does not have "
        f"this operation; use search_operations on connector {sibling_connector_id} "
        "to find a typed operation for this target"
    )
