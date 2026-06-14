# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Composite-backing registry + an ``unbacked`` op-listing marker.

G0.25-T6 (#1757). A composite op (``gh.composite.pr_status_summary``,
``vmware.composite.datastore.usage``, ...) is itself an ingested
``endpoint_descriptor`` row -- ``source_kind="composite"``,
``is_enabled=True`` -- so it shows up in the op listing
(:func:`~meho_backplane.operations.meta_tools.search_operations`) as a
normal, callable hit. But the raw-REST L2 primitives it dispatches into
ship as ``ingested`` descriptors that only land after an operator runs
``meho connector ingest --catalog <product>/<version>``. Until that
ingest happens, the composite's first dispatch trips the preflight
(:mod:`~meho_backplane.connectors.github.composites._preflight`) and
raises :class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
before any HTTP call -- a composite advertised as *enabled* dead-ends
at the first call.

This module closes the surfacing gap the
``CompositeL2DependencyMissing`` error opened at dispatch time but never
reflected on the listing: it lets each connector register, per composite
op_id, the L2 sub-ops the composite depends on plus the catalog command
that ingests them, and exposes an async check the op listing consults to
mark a composite ``unbacked`` (with the catalog-ingest ``next_step``)
while its sub-ops are absent. The marker disappears once the operator
runs the ingest -- the same DB state the preflight reads, so the listing
and the dispatch agree.

Why a registry and not the preflight's per-call constants
---------------------------------------------------------

The preflight already walks each composite's declared ``sub_op_ids``,
but it does so from inside the handler at dispatch time -- the sub-op
tuple is passed as a call argument, not stored anywhere the *listing*
can reach. The listing runs in a different code path
(``search_operations`` over ``endpoint_descriptor`` rows) with only the
op_id in hand. A small process-wide registry, populated by the same
connector composite registrars that own the ``_SUB_OPS_*`` constants,
gives the listing a lookup from op_id -> (sub_op_ids, catalog command)
without coupling the generic meta-tool to any one connector's module
layout. The connector registers the *same* constants it hands the
preflight, so the two stay a single source of truth.

Why ``lookup_descriptor`` (the enable-aware probe)
--------------------------------------------------

:func:`~meho_backplane.operations._lookup.lookup_descriptor` filters on
``is_enabled=True`` -- exactly the rows a dispatch can resolve. The
preflight uses it for the same reason, so an op that is ingested but
left disabled counts as *missing* for both the marker and the dispatch.
Mirroring the preflight's probe keeps the listing's "unbacked" verdict
faithful to what the composite would actually hit on its next call.

Registration timing
--------------------

Registration is a pure in-process dict write with no DB / embedding
work, so connector composite modules register at import time (alongside
the lifespan-registrar queueing in their package ``__init__``). The
registry is therefore populated before any request reaches the listing.
``gh.composite.*`` sub-ops (composite-to-composite recursion) are
skipped by the presence walk for the same reason the preflight skips
them: their registration is guaranteed by the lifespan registrar, and
they are not catalog-ingested raw primitives.
"""

from __future__ import annotations

from typing import Final, NamedTuple
from uuid import UUID

import structlog

from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
from meho_backplane.operations.ingest.api_schemas import NextStep

__all__ = [
    "CompositeBacking",
    "register_composite_backing",
    "registered_composite_backing",
    "reset_composite_backing_registry",
    "unbacked_composite_next_step",
]

_log = structlog.get_logger(__name__)


class CompositeBacking(NamedTuple):
    """The L2 dependency surface of one composite op, for the listing check.

    Field-table form (the same shape the connector registrars already use
    for their per-composite metadata) so a maintainer reading a
    registration sees the whole row at a glance.

    Attributes
    ----------
    connector_id:
        The connector_id the composite dispatches against
        (``"gh-rest-3"``). Parsed into ``(product, version, impl_id)`` for
        the descriptor probe -- the same triple the preflight derives.
    sub_op_ids:
        The L2 sub-op-ids the composite dispatches into. The same tuple
        the connector hands the preflight, so the listing's verdict and
        the dispatch-time preflight read one source of truth.
    catalog_command:
        The operator-facing CLI verb that ingests the missing L2 ops
        (``"meho connector ingest --catalog gh/3"``). Becomes the
        ``next_step.verb`` on the unbacked marker -- the same command the
        ``CompositeL2DependencyMissing`` dispatch error carries.
    """

    connector_id: str
    sub_op_ids: tuple[str, ...]
    catalog_command: str


#: Process-wide registry of composite op_id -> its backing dependency
#: surface. Populated at import time by connector composite registrars
#: (see the module docstring on timing). A plain dict keeps the lookup
#: O(1) for the listing's per-hit check.
_REGISTRY: Final[dict[str, CompositeBacking]] = {}

#: Infix that marks a composite-to-composite recursion sub-op
#: (``gh.composite.*`` / ``vmware.composite.*``). Skipped by the presence
#: walk for the same reason the connector preflights skip it -- such
#: sub-ops are registered by the lifespan registrar, not catalog-ingested,
#: so they can never be the cause of an unbacked composite. The infix
#: form (rather than a per-connector ``<product>.composite.`` prefix)
#: keeps this generic across connectors; raw-REST L2 primitives are
#: ``METHOD:/path`` strings and never contain it.
_COMPOSITE_OP_INFIX: Final[str] = ".composite."


def register_composite_backing(
    *,
    composite_op_id: str,
    connector_id: str,
    sub_op_ids: tuple[str, ...],
    catalog_command: str,
) -> None:
    """Register a composite's L2 dependency surface for the listing check.

    Called once per composite op at connector import time. Idempotent: a
    re-registration with the same payload is a no-op; a re-registration
    that *changes* the payload overwrites and logs, so a copy-paste
    mistake (two composites sharing an op_id constant) surfaces in the
    structured log rather than silently shadowing.

    Parameters
    ----------
    composite_op_id:
        The composite's own op_id -- the key the listing looks up by.
    connector_id, sub_op_ids, catalog_command:
        See :class:`CompositeBacking`.
    """
    backing = CompositeBacking(
        connector_id=connector_id,
        sub_op_ids=sub_op_ids,
        catalog_command=catalog_command,
    )
    existing = _REGISTRY.get(composite_op_id)
    if existing is not None and existing != backing:
        _log.warning(
            "composite_backing_reregistered",
            composite_op_id=composite_op_id,
            previous_connector_id=existing.connector_id,
            new_connector_id=connector_id,
        )
    _REGISTRY[composite_op_id] = backing


def registered_composite_backing(composite_op_id: str) -> CompositeBacking | None:
    """Return the registered backing for *composite_op_id*, or ``None``.

    ``None`` means the op_id is not a registered composite -- the listing
    treats it as an ordinary op and never attaches an unbacked marker.
    """
    return _REGISTRY.get(composite_op_id)


def reset_composite_backing_registry() -> None:
    """Clear the registry. Test seam only -- never called in production.

    Lets a unit test register a synthetic composite, exercise the marker,
    and tear the entry down without leaking into sibling tests.
    """
    _REGISTRY.clear()


async def unbacked_composite_next_step(
    *,
    op_id: str,
    tenant_id: UUID,
) -> NextStep | None:
    """Return the catalog-ingest ``next_step`` when *op_id* is an unbacked composite.

    The op listing calls this per composite hit. Three outcomes:

    * *op_id is not a registered composite* -> ``None`` (ordinary op; no
      marker).
    * *op_id is a registered composite, every raw L2 sub-op resolves to an
      enabled descriptor* -> ``None`` (the composite dispatches cleanly;
      no marker).
    * *op_id is a registered composite, at least one raw L2 sub-op is
      absent (or ingested-but-disabled)* -> a :class:`NextStep` whose
      ``verb`` is the catalog-ingest command, so the operator sees
      "enabled -- run the catalog ingest first" instead of a silent
      dead-end at dispatch.

    The presence probe is :func:`lookup_descriptor` -- the enable-aware
    lookup the dispatch-time preflight uses -- under *tenant_id*'s
    tenant-scoped-then-global fallback, so the marker reflects what the
    composite would actually resolve on its next call for this operator.
    ``composite.*`` sub-ops (composite-to-composite recursion) are skipped
    for the same reason the preflight skips them.
    """
    backing = _REGISTRY.get(op_id)
    if backing is None:
        return None
    raw_sub_ops = tuple(
        sub_op for sub_op in backing.sub_op_ids if _COMPOSITE_OP_INFIX not in sub_op
    )
    if not raw_sub_ops:
        return None
    product, version, impl_id = parse_connector_id(backing.connector_id)
    for sub_op_id in raw_sub_ops:
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=sub_op_id,
        )
        if descriptor is None:
            return NextStep(
                verb=backing.catalog_command,
                rationale=(
                    "This composite is enabled but its L2 sub-operations are not "
                    "ingested yet, so the first dispatch fails with "
                    "composite_l2_missing before any call. Run the catalog ingest "
                    "to populate them, then retry."
                ),
            )
    return None
