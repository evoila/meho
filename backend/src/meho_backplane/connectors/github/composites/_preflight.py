# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""L2 sub-op pre-flight check for gh-rest composite handlers.

Mirrors :mod:`meho_backplane.connectors.vmware_rest.composites._preflight`
(G0.14-T10 / #1183). The gh-rest connector advertises L1 composites
(``gh.composite.*``) that dispatch into raw-REST L2 primitives
(``GET:/repos/{owner}/{repo}/pulls/{pull_number}`` etc.). The L2 ops
ship as ``ingested`` descriptors -- they only land in
``endpoint_descriptor`` after an operator runs
``meho connector ingest --catalog gh/v3``.

A composite handler that calls
:func:`~meho_backplane.operations.composite.DispatchChild` against an
unregistered L2 op gets back the dispatcher's generic
``OperationResult(status='error', error='unknown_op: ...')`` shape,
which the handler's ``_require_ok`` helper then re-raises. This module
turns that into a structured ``composite_l2_missing`` error per the
``docs/codebase/error-message-shape.md`` convention (G0.14-T11 #1141),
carrying the missing op-ids plus the catalog command the operator
should run to fix the gap.

Why lazy (Option B) and not eager
---------------------------------

Same trade-offs as the vmware-rest precedent (see that module's docstring
for the full Option-A/B/C analysis):

* Composites self-register during the chassis lifespan, before any
  operator has had a chance to run ``meho connector ingest``. Eager
  validation (Option A) would crash the lifespan.
* Auto-ingesting the catalog at boot (Option C) inverts the
  operator-driven posture of the v0.5.1 / v0.6.0 / v0.7.x catalog
  design and couples to T9 (#1182).

Lazy pre-resolve (this module) validates at first call, caches the
all-present result, and surfaces a remediation-bearing error when
any sub-op is missing. Same trade-offs apply here.

Cache shape
-----------

Cache is per-process and keyed on the composite op_id. Same rationale
as the vmware module: L2 descriptors are global
(``tenant_id IS NULL``), and a stale negative would defeat the
operator's expected workflow (see the error -> run the catalog
command -> retry). Negative results are NOT cached.

A separate cache per connector keeps the gh-rest pre-flight state from
colliding with the vmware-rest pre-flight state (and vice versa) so a
test that resets one does not invalidate the other.

References
----------

* G0.14-T10 #1183 -- vmware-rest precedent.
* G0.14-T11 #1141 -- error-message-shape convention.
* G3.11-T1 #1221 -- connector substrate.
* G3.11-T3 #1228 -- catalog row + ingest acceptance scaffolding.
"""

from __future__ import annotations

from meho_backplane.connectors.github._catalog_command import (
    catalog_command_for_github_rest,
)
from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
from meho_backplane.operations.composite import CompositeL2DependencyMissing

__all__ = [
    "preflight_l2_dependencies",
    "reset_preflight_cache",
]


#: Per-process cache of composite op_ids that have already passed the
#: pre-flight walk. Populated on cache miss (all sub-ops present); cleared
#: by :func:`reset_preflight_cache` (test seam). Separate from the
#: vmware-rest module's cache so cross-connector test isolation holds.
_PREFLIGHT_CACHE: set[str] = set()


async def preflight_l2_dependencies(
    *,
    composite_op_id: str,
    sub_op_ids: tuple[str, ...],
    connector_id: str,
    tenant_id: object,
) -> None:
    """Validate every sub-op_id resolves to a registered descriptor.

    Called from the top of each gh-rest composite handler with the
    handler's own op_id and the declared sub-op-id constants. On a cache
    hit (this composite already passed the walk), returns immediately
    with no DB round-trip. On a cache miss, walks each sub-op-id through
    :func:`~meho_backplane.operations._lookup.lookup_descriptor` and
    raises
    :class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
    listing every absent sub-op + the catalog command.

    Parameters
    ----------
    composite_op_id:
        The composite's own op_id (``gh.composite.pr_status_summary``
        etc.). Used as the cache key and surfaced in the exception.
    sub_op_ids:
        Tuple of L2 sub-op-ids the composite dispatches into. Order does
        not matter -- the walk reports every miss in one exception
        payload so the operator sees the full gap in one go.
    connector_id:
        The connector_id the handler dispatches against
        (``"gh-rest-3"``). Parsed into ``(product, version, impl_id)``
        for the descriptor lookup.
    tenant_id:
        The composite's operator tenant_id. Forwarded to
        ``lookup_descriptor``'s tenant-scoped-then-global fallback shape;
        L2 descriptors are global so the global fallback hit is the
        common case. Typed as :class:`object` to keep this module
        importable from handler files without pulling in the auth
        package (avoids the Operator -> settings -> DB import cycle).

    Raises
    ------
    CompositeL2DependencyMissing
        One or more sub-op-ids are not registered. The exception's
        ``missing_op_ids`` lists every absent sub-op (not just the
        first-found), and ``catalog_command`` carries the
        ``meho connector ingest --catalog gh/v3`` invocation operators
        must run.

    Notes
    -----
    * Sub-op-ids that begin with ``gh.composite.`` are skipped -- those
      are composite-to-composite recursion (a future composite that
      calls ``gh.composite.pr_status_summary`` as a sub-step). Their
      registration is guaranteed by the lifespan registrar that brought
      us here. Only raw-REST primitives (``GET:/...`` / ``POST:/...``
      etc.) get validated.
    """
    if composite_op_id in _PREFLIGHT_CACHE:
        return
    raw_sub_ops = tuple(op for op in sub_op_ids if not op.startswith("gh.composite."))
    if not raw_sub_ops:
        _PREFLIGHT_CACHE.add(composite_op_id)
        return

    product, version, impl_id = parse_connector_id(connector_id)
    missing: list[str] = []
    for sub_op_id in raw_sub_ops:
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,  # type: ignore[arg-type]
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=sub_op_id,
        )
        if descriptor is None:
            missing.append(sub_op_id)
    if missing:
        # Do NOT cache a negative result: the operator's expected next
        # action is to run the catalog command and retry, and we want
        # the retry to see fresh state from the DB.
        raise CompositeL2DependencyMissing(
            composite_op_id=composite_op_id,
            missing_op_ids=tuple(missing),
            catalog_command=catalog_command_for_github_rest(),
        )
    _PREFLIGHT_CACHE.add(composite_op_id)


def reset_preflight_cache() -> None:
    """Clear the per-process gh-rest preflight cache.

    Test seam (so a unit test can prime the cache, then invalidate it to
    exercise the cache-miss path again). Not called from production
    code; a future operator-initiated catalog-ingest signal hook would
    call this to force the next composite dispatch to re-check.
    """
    _PREFLIGHT_CACHE.clear()
