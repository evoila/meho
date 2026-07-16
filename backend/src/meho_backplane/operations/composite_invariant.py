# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Platform-wide registration-time invariant for the two-world op model.

Goal #2247 (Initiative #2248, Task #2252). The two-world model draws one
line: a *code-shipped* operation -- ``source_kind="typed"`` or
``source_kind="composite"``, registered from the image at connector init --
must never dispatch through an *ingested* ``endpoint_descriptor`` row
(``source_kind="ingested"``, a raw L1 primitive that only lands in the DB
when an operator runs ``meho connector ingest``). A composite whose
correctness depends on mutable per-deploy catalog state (is the sub-op
ingested here? enabled here? schema-matched here?) is the recurring defect
class the Goal retires; making the sub-call directly through the
connector's own session (Task #2251) is the compliant alternative.

This module holds the enforcement, plus the tiny registry the enforcement
reads. It is deliberately connector-agnostic: it knows nothing about github
or vmware, only about the generic fact "``endpoint_descriptor`` row X is
``source_kind='ingested'``". Three parts:

* :func:`register_composite_dispatch_surface` -- the declaration seam. A
  connector that ships a composite which routes any sub-op through the
  dispatcher (``dispatch_child``) registers, per composite op_id, the
  ``connector_id`` + declared ``sub_op_ids`` so the sweep can check them.
  A composite that dispatches every sub-op directly on the connector's own
  session has no descriptor-routed surface to declare and registers
  nothing -- that is the compliant shape the Goal migrated the shipped
  composites to, which is why the production registry is empty. The seam
  exists so a *future* code-shipped op that reintroduces ``dispatch_child``
  routing is covered without touching this module.
* :func:`assert_no_ingested_dispatch_dependency` -- the per-op primitive.
  Given a code-shipped op's declared ``sub_op_ids`` and its
  ``connector_id``, it resolves each raw sub-op against the descriptor
  table and raises :class:`IngestedDispatchDependencyError` naming the
  offending op + sub-op if any resolve to an ``ingested`` row.
* :func:`assert_registered_composites_have_no_ingested_dispatch` -- the
  platform-wide sweep. It walks every composite that declared a dispatch
  surface and applies the primitive to each. Any connector that registers a
  surface is covered without touching this module. This is the one shared
  check the Goal DoD asks for ("enforced by a registration-time invariant,
  not per-connector guards"); github's retired import-time
  ``UnbackedEnabledCompositeError`` guard and vmware's dispatch-time
  preflight are both folded into it.

Timing and fail-closed shape
----------------------------

The sweep runs at the tail of
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`,
so it fires during the FastAPI lifespan after every connector has
registered its typed/composite rows -- the same crash-loud posture the
registrar runner already takes. A violation is a *deploy* bug (a
code-shipped op wired to a catalog row), not a runtime condition, so
surfacing it as a lifespan crash with an actionable message is correct:
the operator sees the offending op + ingested sub-op immediately rather
than a mid-flight dispatch failure under request load.

Why keying on the resolved ``source_kind``
------------------------------------------

The invariant deliberately does *not* enumerate raw-REST ``METHOD:/path``
shapes or hard-code connector prefixes. It keys purely on what a declared
sub-op *resolves to* in ``endpoint_descriptor``: a row present with
``source_kind='ingested'`` is the violation; a row with
``source_kind='composite'``/``'typed'`` (a registrar-guaranteed
code-shipped op) is allowed, and a sub-op that resolves to *nothing*
(absent on this deploy) is not this invariant's concern -- absence means
the op simply is not ingested here, which a direct-session composite is
indifferent to. Composite-to-composite recursion sub-ops
(``*.composite.*``) are skipped for the same reason the connector
preflights skipped them: they are guaranteed by the lifespan registrar and
are never ingested primitives.
"""

from __future__ import annotations

from typing import Final, NamedTuple

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import parse_connector_id

__all__ = [
    "CompositeDispatchSurface",
    "IngestedDispatchDependencyError",
    "assert_no_ingested_dispatch_dependency",
    "assert_registered_composites_have_no_ingested_dispatch",
    "register_composite_dispatch_surface",
    "registered_composite_dispatch_surfaces",
    "reset_composite_dispatch_surface_registry",
]

_log = structlog.get_logger(__name__)

#: Infix marking a composite-to-composite recursion sub-op
#: (``gh.composite.*`` / ``vmware.composite.*``). Skipped by the walk for
#: the same reason the connector preflights and the backing-listing marker
#: skipped it: such sub-ops resolve to registrar-guaranteed
#: ``source_kind='composite'`` rows, never ingested primitives, so they can
#: never be the cause of an ingested-dispatch violation. Raw-REST L2
#: primitives are ``METHOD:/path`` strings and never contain it.
_COMPOSITE_OP_INFIX = ".composite."


class CompositeDispatchSurface(NamedTuple):
    """The descriptor-routed sub-op surface of one code-shipped composite.

    Registered by a connector for any composite that still routes a sub-op
    through the dispatcher (``dispatch_child``), so the platform-wide sweep
    can assert none of those sub-ops resolves to an ``ingested`` row.

    Attributes
    ----------
    connector_id:
        The connector the composite dispatches against (``"gh-rest-3"``),
        parsed into ``(product, version, impl_id)`` for the descriptor probe.
    sub_op_ids:
        The declared sub-op ids the composite dispatches into the dispatcher
        (not the ones it calls directly on the connector session).
    """

    connector_id: str
    sub_op_ids: tuple[str, ...]


#: Process-wide registry of composite op_id -> its declared descriptor-routed
#: dispatch surface. Populated at connector import/registration time. Empty in
#: production once every shipped composite dispatches its sub-ops directly on
#: the connector session (Goal #2247); the seam persists so a future
#: ``dispatch_child``-routed composite is swept without wiring here.
_REGISTRY: Final[dict[str, CompositeDispatchSurface]] = {}


class IngestedDispatchDependencyError(RuntimeError):
    """A code-shipped op's dispatch path resolves through an ingested row.

    Raised at connector-registration time (lifespan) when a
    ``source_kind='typed'``/``'composite'`` op declares a sub-op dependency
    that resolves to an ``endpoint_descriptor`` row with
    ``source_kind='ingested'``. Under the two-world model (Goal #2247) a
    code-shipped op must be self-contained -- transport via the connector's
    own session -- and never depend on mutable catalog state. This guard
    fails the boot closed with the offending op + its ingested sub-op so
    the regression cannot ship enabled-but-catalog-dependent.
    """


def register_composite_dispatch_surface(
    *,
    composite_op_id: str,
    connector_id: str,
    sub_op_ids: tuple[str, ...],
) -> None:
    """Register a composite's descriptor-routed sub-op surface for the sweep.

    Called once per descriptor-routing composite at connector import/
    registration time. Idempotent: a re-registration with the same payload
    is a no-op; a re-registration that *changes* the payload overwrites and
    logs, so a copy-paste mistake (two composites sharing an op_id constant)
    surfaces in the structured log rather than silently shadowing.

    Parameters
    ----------
    composite_op_id:
        The composite's own op_id -- the key the sweep iterates by.
    connector_id, sub_op_ids:
        See :class:`CompositeDispatchSurface`.
    """
    surface = CompositeDispatchSurface(connector_id=connector_id, sub_op_ids=sub_op_ids)
    existing = _REGISTRY.get(composite_op_id)
    if existing is not None and existing != surface:
        _log.warning(
            "composite_dispatch_surface_reregistered",
            composite_op_id=composite_op_id,
            previous_connector_id=existing.connector_id,
            new_connector_id=connector_id,
        )
    _REGISTRY[composite_op_id] = surface


def registered_composite_dispatch_surfaces() -> dict[str, CompositeDispatchSurface]:
    """Return a snapshot copy of every registered composite dispatch surface.

    A shallow copy of the process-wide registry keyed by composite op_id,
    consumed by :func:`assert_registered_composites_have_no_ingested_dispatch`.
    Returning a copy keeps the caller from mutating the live registry while
    iterating.
    """
    return dict(_REGISTRY)


def reset_composite_dispatch_surface_registry() -> None:
    """Clear the registry. Test seam only -- never called in production.

    Lets a unit test register a synthetic composite surface, exercise the
    sweep, and tear the entry down without leaking into sibling tests.
    """
    _REGISTRY.clear()


async def assert_no_ingested_dispatch_dependency(
    *,
    op_id: str,
    connector_id: str,
    sub_op_ids: tuple[str, ...],
) -> None:
    """Assert none of *op_id*'s declared sub-ops resolve to an ingested row.

    The per-op primitive of the two-world invariant. Parses *connector_id*
    into ``(product, version, impl_id)`` (the same triple the dispatcher
    resolves against), then for each raw sub-op -- composite-to-composite
    recursion (``*.composite.*``) sub-ops are skipped -- probes
    ``endpoint_descriptor`` for a built-in / global (``tenant_id IS NULL``)
    row carrying ``source_kind='ingested'``. Enablement is intentionally
    *not* filtered: a code-shipped op wired to a catalog row is a violation
    whether or not that row happens to be enabled on this deploy.

    Parameters
    ----------
    op_id:
        The code-shipped op whose dependency surface is being asserted --
        named in the raised error so the operator can locate it.
    connector_id:
        The connector the op dispatches against (``"gh-rest-3"``), parsed
        into the descriptor natural-key triple.
    sub_op_ids:
        The declared sub-op ids the op dispatches into (the same tuple a
        connector hands its dispatch-surface registration).

    Raises
    ------
    IngestedDispatchDependencyError
        One or more declared sub-ops resolve to an ``ingested`` descriptor
        row. The message lists every offending sub-op (not just the first).
    """
    raw_sub_ops = tuple(sub_op for sub_op in sub_op_ids if _COMPOSITE_OP_INFIX not in sub_op)
    if not raw_sub_ops:
        return

    product, version, impl_id = parse_connector_id(connector_id)
    offenders: list[str] = []
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for sub_op_id in raw_sub_ops:
            result = await session.execute(
                select(EndpointDescriptor.id)
                .where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == product,
                    EndpointDescriptor.version == version,
                    EndpointDescriptor.impl_id == impl_id,
                    EndpointDescriptor.op_id == sub_op_id,
                    EndpointDescriptor.source_kind == "ingested",
                )
                .limit(1)
            )
            if result.first() is not None:
                offenders.append(sub_op_id)

    if offenders:
        raise IngestedDispatchDependencyError(
            f"code-shipped op {op_id!r} (connector {connector_id!r}) dispatches "
            f"through ingested endpoint_descriptor row(s) {offenders!r}; a "
            f"typed/composite op must be self-contained (transport via the "
            f"connector's own session), never routed through a mutable "
            f"ingested catalog row (two-world invariant, Goal #2247). Migrate "
            f"the sub-call to a direct-session or code-shipped (typed/composite) "
            f"sub-op."
        )


async def assert_registered_composites_have_no_ingested_dispatch() -> None:
    """Sweep every registered composite dispatch surface for ingested-dispatch violations.

    The platform-wide entry point, invoked at the tail of
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    once every connector has registered. Walks the dispatch-surface registry
    -- each entry is a composite that declared a descriptor-routed sub-op
    surface via :func:`register_composite_dispatch_surface` -- and applies
    :func:`assert_no_ingested_dispatch_dependency` to each. Connector-agnostic:
    a connector is covered the moment it registers a surface, with no
    per-connector wiring here.

    Raises
    ------
    IngestedDispatchDependencyError
        Propagated from the first composite whose declared sub-ops resolve
        to an ingested row -- a lifespan-crashing deploy bug.
    """
    for composite_op_id, surface in registered_composite_dispatch_surfaces().items():
        await assert_no_ingested_dispatch_dependency(
            op_id=composite_op_id,
            connector_id=surface.connector_id,
            sub_op_ids=surface.sub_op_ids,
        )
