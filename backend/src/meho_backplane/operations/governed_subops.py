# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed-subop discovery registry + classification (#3349).

A composite fans out to child ops, and child gating consults the grant
plane on the **child** op id with no parent→child inheritance (by design —
``docs/architecture/operations-substrate.md``), so a service principal that
runs a composite unattended needs a standing grant for **each** governed
child op. Before #3349 an operator had to read the connector's
``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*`` manifests in source to learn that child
set. This module is the supported alternative: a connector registers, per
composite op id, the child op ids it may dispatch (the manifest tuples,
referenced — never re-typed), and the operator reads them off a discovery
surface (``GET /api/v1/operations/governed-subops``).

It is deliberately connector-agnostic — it knows nothing about vmware; it
only holds op-id → child-op-id tuples a connector declared. Two parts:

* :func:`register_governed_subops` — the declaration seam a connector calls
  once per composite at registration time, passing its manifest tuple.
* :func:`classify_subop_grantability` — the per-child grantability verdict.
  It single-sources the delete-shaped classification through
  :mod:`meho_backplane.operations.service_grants` (the same rule
  ``ServicePrincipalGrantService.create`` refuses on), so a rollback / delete
  leg (e.g. ``vm.create``'s ``DELETE:/vcenter/vm/{vm}``) is flagged
  **un-grantable** on the discovery surface exactly as it would be refused at
  grant-create time — telling the operator up front that a partial failure of
  that composite will still require a human.
"""

from __future__ import annotations

from typing import Final, NamedTuple
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

__all__ = [
    "GovernedSubop",
    "GovernedSubopSurface",
    "GovernedSubopsResponse",
    "SubopGrantability",
    "build_governed_subops_response",
    "classify_subop_grantability",
    "governed_subops_for",
    "register_governed_subops",
    "registered_governed_subop_surfaces",
    "reset_governed_subop_registry",
]

_log = structlog.get_logger(__name__)


class GovernedSubopSurface(NamedTuple):
    """The governed child-op surface of one composite.

    Attributes
    ----------
    connector_id:
        The connector the composite dispatches against (``"vmware-rest-9.0"``)
        — used to resolve each child's descriptor for a best-effort
        ``safety_level`` on the discovery surface.
    sub_op_ids:
        The child op ids the composite may dispatch, in declaration order,
        de-duplicated. Sourced verbatim from the connector's
        ``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*`` manifests (rollback / delete legs
        included — they are governed dispatches too).
    """

    connector_id: str
    sub_op_ids: tuple[str, ...]


class SubopGrantability(NamedTuple):
    """One child op's grantability verdict for the discovery surface."""

    op_id: str
    grantable: bool
    ungrantable_reason: str | None


#: Process-wide registry of composite op_id -> its governed child-op surface.
#: Populated at connector import/registration time.
_REGISTRY: Final[dict[str, GovernedSubopSurface]] = {}


def register_governed_subops(
    *,
    composite_op_id: str,
    connector_id: str,
    sub_op_ids: tuple[str, ...],
) -> None:
    """Register a composite's governed child-op surface for discovery.

    Idempotent per payload; a re-registration that changes the payload
    overwrites and logs (a copy-paste of a shared op_id constant surfaces in
    the structured log rather than silently shadowing). Empty ``sub_op_ids``
    registers nothing — a composite with no governed children has no grant
    set to discover.
    """
    if not sub_op_ids:
        return
    # De-duplicate while preserving first-seen order (a child op can appear in
    # both the REST and vim manifest halves of one composite).
    deduped = tuple(dict.fromkeys(sub_op_ids))
    surface = GovernedSubopSurface(connector_id=connector_id, sub_op_ids=deduped)
    existing = _REGISTRY.get(composite_op_id)
    if existing is not None and existing != surface:
        _log.warning(
            "governed_subop_surface_reregistered",
            composite_op_id=composite_op_id,
            previous_connector_id=existing.connector_id,
            new_connector_id=connector_id,
        )
    _REGISTRY[composite_op_id] = surface


def governed_subops_for(composite_op_id: str) -> GovernedSubopSurface | None:
    """Return the registered governed child-op surface for *composite_op_id*."""
    return _REGISTRY.get(composite_op_id)


def registered_governed_subop_surfaces() -> dict[str, GovernedSubopSurface]:
    """Return a snapshot copy of the whole registry (keyed by composite op_id)."""
    return dict(_REGISTRY)


def reset_governed_subop_registry() -> None:
    """Clear the registry. Test seam only — never called in production."""
    _REGISTRY.clear()


def classify_subop_grantability(op_id: str) -> SubopGrantability:
    """Return whether *op_id* is grantable, and why not when it is not.

    Single-sources the delete-shaped classification through
    :func:`meho_backplane.operations.service_grants.delete_shaped_refusal_reason`
    — the exact rule ``ServicePrincipalGrantService.create`` refuses on — so
    an un-grantable rollback / delete leg is flagged on the discovery surface
    identically to how it would be refused at grant-create time. Pattern-only
    here (no descriptor lookup): the surface classifies by op-id shape, and
    the caller layers a best-effort descriptor ``safety_level`` on top.
    """
    from meho_backplane.operations.service_grants import delete_shaped_refusal_reason
    from meho_backplane.settings import get_settings

    reason = delete_shaped_refusal_reason(
        op_id, get_settings().service_grant_delete_shaped_patterns
    )
    return SubopGrantability(op_id=op_id, grantable=reason is None, ungrantable_reason=reason)


# ---------------------------------------------------------------------------
# Discovery-surface view (GET /api/v1/operations/governed-subops, #3349)
# ---------------------------------------------------------------------------


class GovernedSubop(BaseModel):
    """One governed child op of a composite (#3349).

    ``safety_level`` is best-effort — resolved from the child's descriptor
    when one is ingested for the connector, ``None`` for a code-shipped vim
    control-plane sub-op that carries no descriptor. ``grantable`` is
    ``False`` for a delete-shaped child (a rollback / delete leg): such a
    child can never carry a standing grant, so a partial failure of the
    composite still requires a human.
    """

    model_config = ConfigDict(frozen=True)

    op_id: str
    safety_level: str | None
    grantable: bool
    ungrantable_reason: str | None


class GovernedSubopsResponse(BaseModel):
    """Response for ``GET /api/v1/operations/governed-subops`` (#3349)."""

    model_config = ConfigDict(frozen=True)

    op_id: str
    connector_id: str
    governed_subops: list[GovernedSubop]


async def build_governed_subops_response(
    *,
    tenant_id: UUID,
    op_id: str,
    connector_id_override: str | None = None,
) -> GovernedSubopsResponse | None:
    """Build the discovery view for *op_id*, or ``None`` when none is registered.

    Enumerates the composite's governed child ops (from the registry, derived
    from the connector's manifests), classifying each: a best-effort
    ``safety_level`` from the child's descriptor (``None`` for a code-shipped
    vim sub-op with no descriptor) and a ``grantable`` flag from the
    delete-shaped classifier (an un-grantable rollback / delete leg carries
    its refusal reason). ``None`` when *op_id* has no registered child
    surface — the caller maps that to a 404.
    """
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

    surface = governed_subops_for(op_id)
    if surface is None:
        return None

    effective_connector_id = connector_id_override or surface.connector_id
    product, version, impl_id = parse_connector_id(effective_connector_id)

    subops: list[GovernedSubop] = []
    for child_op_id in surface.sub_op_ids:
        grantability = classify_subop_grantability(child_op_id)
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=child_op_id,
        )
        subops.append(
            GovernedSubop(
                op_id=child_op_id,
                safety_level=descriptor.safety_level if descriptor is not None else None,
                grantable=grantability.grantable,
                ungrantable_reason=grantability.ungrantable_reason,
            )
        )
    return GovernedSubopsResponse(
        op_id=op_id,
        connector_id=effective_connector_id,
        governed_subops=subops,
    )
