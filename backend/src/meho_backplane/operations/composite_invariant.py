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

This module holds the enforcement. It is deliberately connector-agnostic:
it knows nothing about github or vmware, only about the generic fact
"``endpoint_descriptor`` row X is ``source_kind='ingested'``". Two entry
points:

* :func:`assert_no_ingested_dispatch_dependency` -- the per-op primitive.
  Given a code-shipped op's declared ``sub_op_ids`` and its
  ``connector_id``, it resolves each raw sub-op against the descriptor
  table and raises :class:`IngestedDispatchDependencyError` naming the
  offending op + sub-op if any resolve to an ``ingested`` row.
* :func:`assert_registered_composites_have_no_ingested_dispatch` -- the
  platform-wide sweep. It walks every composite that declared its L2
  dependency surface via
  :func:`~meho_backplane.operations.composite_backing.register_composite_backing`
  and applies the primitive to each. This is where github's
  ``gh.composite.pr_status_summary`` is folded into the one shared check
  (its bespoke import-time ``UnbackedEnabledCompositeError`` guard is
  retired separately in #2259); any future connector that registers a
  backing is covered without touching this module.

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
(absent on this deploy) is not this invariant's concern -- absence is the
``composite_l2_missing`` failure class the retired apparatus handles.
Composite-to-composite recursion sub-ops (``*.composite.*``) are skipped
for the same reason the connector preflights skip them: they are
guaranteed by the lifespan registrar and are never ingested primitives.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations.composite_backing import registered_composite_backings

__all__ = [
    "IngestedDispatchDependencyError",
    "assert_no_ingested_dispatch_dependency",
    "assert_registered_composites_have_no_ingested_dispatch",
]

_log = structlog.get_logger(__name__)

#: Infix marking a composite-to-composite recursion sub-op
#: (``gh.composite.*`` / ``vmware.composite.*``). Skipped by the walk for
#: the same reason the connector preflights and the backing-listing marker
#: skip it: such sub-ops resolve to registrar-guaranteed
#: ``source_kind='composite'`` rows, never ingested primitives, so they can
#: never be the cause of an ingested-dispatch violation. Raw-REST L2
#: primitives are ``METHOD:/path`` strings and never contain it.
_COMPOSITE_OP_INFIX = ".composite."


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
        The declared L2 sub-op ids the op dispatches into (the same tuple a
        connector hands its preflight / backing registration).

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
    """Sweep every registered composite backing for ingested-dispatch violations.

    The platform-wide entry point, invoked at the tail of
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    once every connector has registered. Walks the composite-backing
    registry -- each entry is a composite that declared its L2 dependency
    surface via
    :func:`~meho_backplane.operations.composite_backing.register_composite_backing`
    -- and applies :func:`assert_no_ingested_dispatch_dependency` to each.
    Connector-agnostic: a connector is covered the moment it registers a
    backing, with no per-connector wiring here.

    Raises
    ------
    IngestedDispatchDependencyError
        Propagated from the first composite whose declared sub-ops resolve
        to an ingested row -- a lifespan-crashing deploy bug.
    """
    for composite_op_id, backing in registered_composite_backings().items():
        await assert_no_ingested_dispatch_dependency(
            op_id=composite_op_id,
            connector_id=backing.connector_id,
            sub_op_ids=backing.sub_op_ids,
        )
