# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""L2 sub-op pre-flight check for vmware-rest composite handlers.

G0.14-T10 (#1151). The vmware-rest connector advertises 13 ops, all
``vmware.composite.*`` aggregations that dispatch into ~3,470 ingested
raw-REST primitives (``GET:/vcenter/datastore``,
``POST:/vcenter/vm`` etc.). The L2 primitives ship as ``ingested``
descriptors -- they only land in ``endpoint_descriptor`` after an
operator runs ``meho connector ingest --catalog vmware/9.0``.

A composite handler that calls
:func:`~meho_backplane.operations.composite.DispatchChild` against an
unregistered L2 op gets back the dispatcher's generic
``OperationResult(status='error', error='unknown_op: ...')`` shape,
which the handler's ``_require_ok`` helper then re-raises as a
``RuntimeError``. The dispatcher's outer exception branch then wraps
that as a ``connector_error`` whose text reads roughly:

    composite sub-op 'GET:/vcenter/datastore' returned status='error':
    unknown_op: GET:/vcenter/datastore

Correct, but missing the remediation the operator needs (which catalog
command to run, why those sub-ops are missing, where to read more).
Consumer signal 20 in ``claude-rdc-hetzner-dc#697`` is exactly this
shape: the vmware-rest-9.0 connector is registered, fingerprinted,
ingested at the L1 (composite) layer, but operationally inert in a
default v0.6.0 deploy because nothing brings the L2 catalog along.

This module is the lazy pre-resolve helper (Option B in the task body):
each composite handler calls :func:`preflight_l2_dependencies` at the
top of its body, passing its own ``op_id`` plus its declared sub-op-id
constants. The helper walks each sub-op against
:func:`~meho_backplane.operations._lookup.lookup_descriptor`; if any
are missing, it raises
:class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
listing the missing ops + the catalog command. The dispatcher catches
that exception specifically (ahead of the generic ``except Exception``)
and surfaces it as a structured ``composite_l2_missing`` error per the
``docs/codebase/error-message-shape.md`` convention (G0.14-T11 #1141).

Disabled vs absent classification (#1601)
-----------------------------------------

``lookup_descriptor`` hard-filters ``is_enabled = TRUE``, so a sub-op
whose descriptor row exists but is **disabled** resolves to ``None``
exactly like one that was never ingested. Collapsing both into
``composite_l2_missing`` mis-remediates the disabled case: it tells the
operator to re-run ``meho connector ingest`` when the catalog is already
ingested. The helper therefore classifies each non-dispatchable sub-op
with the ``is_enabled``-agnostic
:func:`~meho_backplane.operations._lookup.descriptor_exists_any_state`
probe (used **only** to classify; never to dispatch a disabled op):

* **present but disabled** -> raise
  :class:`~meho_backplane.operations.composite.CompositeL2DependencyDisabled`
  -> structured ``composite_l2_disabled`` whose remediation names a real
  verb, per-op ``meho connector edit-op <connector_id> <op_id> --enable``
  (connector-level ``enable`` does not cascade to spec-ingested
  ``group_id = NULL`` ops, so per-op is the deterministic path).
* **truly absent** -> unchanged ``composite_l2_missing`` + the
  catalog-ingest remediation.

Disabled takes precedence over missing when a single walk turns up both
(only one exception can surface, and the re-enable remediation is the
one a default ingested-but-disabled deploy needs).

Why lazy (Option B) and not eager (Options A / C)
-------------------------------------------------

Three options were considered (per the task body's *Desired state*):

* **A — validate at registration time.** Composite registrar walks each
  composite's declared sub-op-ids; refuse registration if any are
  missing. *Cons*: composites self-register during the chassis
  lifespan, before the operator has had a chance to run ``meho
  connector ingest``. A default deploy would crash on boot until the
  catalog is ingested -- making the connector even harder to bring up
  than today, where ``unknown_op`` is at least a runtime error the
  operator can react to. Inverts the "registered = useful" contract
  the chassis assumes about connectors.
* **B — pre-resolve on first call** (this module). Composite handler
  validates its sub-ops at dispatch time; first call against an
  un-ingested catalog returns a structured error with the catalog
  command; subsequent calls reuse the cached "all-present" result.
  *Pros*: minimal blast radius (one helper, called from each handler),
  no boot-order dependency, doesn't depend on T9 (#1150) shipping
  server-side catalog-driven ingest. Matches the consumer's actual
  feedback ("the dispatch error needed a remediation hint") without
  reshaping the connector lifecycle. *Cons*: error surfaces at first
  call rather than at registration -- operators who run ``connector
  list`` see the 13 composites and may dispatch before realising L2
  needs ingestion. Acceptable because the error itself names the
  remediation.
* **C — ship L1+L2 as a unit** (auto-ingest at boot from the catalog).
  *Cons*: bigger default footprint (~3,470 ops auto-ingested per
  connector), couples to T9 (#1150) catalog-driven REST ingest landing
  first to do server-side multi-spec resolution, inverts the "ingest
  is an operator action" posture of the v0.5.1 / v0.6.0 catalog
  design (T9's own *Out of scope* explicitly excludes auto-ingest at
  boot).

Option B was picked because it (a) closes signal 20's actual gap (a
remediation-bearing error), (b) does not block on T9, (c) does not
disrupt the boot order, and (d) leaves the catalog ingest as the
explicit operator action v0.5.1 / v0.6.0 are designed around.

Cache shape
-----------

Caching is per-composite-op_id and process-wide. ``preflight_l2_dependencies``
keeps a set of composite op_ids that have already passed the walk; a
subsequent call for the same composite skips the DB round-trip. Cache
misses (a sub-op-id that doesn't exist) are *not* cached -- the
operator's expected workflow is "see the error, run the catalog
command, retry" and we want the retry to land on a fresh check rather
than a stale negative.

The cache is keyed only on the composite op_id (not on
``(operator.tenant_id, op_id)``) because L2 descriptors are global
(``tenant_id IS NULL``); see
:func:`~meho_backplane.operations._lookup.lookup_descriptor` for the
tenant-then-global fallback shape. A future tenant-scoped composite
override would need to invalidate this cache; not relevant for v0.6.x.

Reset hooks
-----------

:func:`reset_preflight_cache` clears the per-process cache so test
fixtures (and operator-initiated catalog ingest, eventually) can force
a re-check on the next dispatch. Production code does not call this
function; the cache is only ever populated on cache miss (correct
state was determined), and a stale positive ("L2 was ingested but
later removed") is acceptably handled by the underlying
``dispatch_child`` call returning ``unknown_op`` like today.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest._catalog_command import (
    catalog_command_for_vmware_rest,
)
from meho_backplane.operations._lookup import (
    descriptor_exists_any_state,
    lookup_descriptor,
    parse_connector_id,
)
from meho_backplane.operations.composite import (
    CompositeL2DependencyDisabled,
    CompositeL2DependencyMissing,
)

__all__ = [
    "preflight_l2_dependencies",
    "reset_preflight_cache",
]


#: Per-process cache of composite op_ids that have already passed the
#: pre-flight walk. Populated on cache miss (all sub-ops present);
#: cleared by :func:`reset_preflight_cache` (test seam, and a future
#: operator-initiated invalidate hook on catalog ingest).
_PREFLIGHT_CACHE: set[str] = set()


async def preflight_l2_dependencies(
    *,
    composite_op_id: str,
    sub_op_ids: tuple[str, ...],
    connector_id: str,
    tenant_id: object,
) -> None:
    """Validate every sub-op_id resolves to a registered descriptor.

    Called from the top of each vmware-rest composite handler with the
    handler's own op_id (``composite_op_id``, the cache key) and its
    declared L2 ``sub_op_ids``. On a cache hit (this composite already
    passed the walk), returns immediately with no DB round-trip. On a
    cache miss, classifies each non-dispatchable sub-op into disabled vs
    absent via :func:`_classify_sub_ops` and raises the matching
    structured exception. ``connector_id`` is parsed into
    ``(product, version, impl_id)`` for the lookups and forwarded into
    the disabled-state remediation; ``tenant_id`` is typed
    :class:`object` to keep this module importable from handler files
    without the auth-package import cycle (it is always an
    ``operator.tenant_id`` UUID at runtime).

    Raises
    ------
    CompositeL2DependencyDisabled
        One or more sub-op-ids resolve to a descriptor row that exists
        but is ``is_enabled = false`` (ingested-but-disabled). Takes
        precedence over :class:`CompositeL2DependencyMissing` when both
        states are present in the same walk (see the *Disabled vs absent*
        note). The exception's ``disabled_op_ids`` lists every disabled
        sub-op and ``connector_id`` is forwarded so the remediation can
        name the per-op ``meho connector edit-op <connector_id> <op_id>
        --enable`` invocation. #1601.
    CompositeL2DependencyMissing
        One or more sub-op-ids have **no** descriptor row at all (and
        none are merely disabled). The exception's ``missing_op_ids``
        lists every absent sub-op (not just the first-found), and
        ``catalog_command`` carries the ``meho connector ingest --catalog
        <product>/<version>`` invocation operators must run.

    Notes
    -----
    * Sub-op-ids that begin with ``vmware.composite.`` are skipped --
      those are composite-to-composite recursion (host.evacuate ->
      vm.migrate), guaranteed to be registered by the lifespan
      registrar that ships this module's containing package. Only
      raw-REST primitives (``GET:/...`` / ``POST:/...`` etc.) get
      validated.
    """
    if composite_op_id in _PREFLIGHT_CACHE:
        return
    # Filter composite-to-composite sub-op-ids -- those are registered by
    # the same lifespan registrar that brought us here, so their
    # registration is a given by the time any composite runs. Validating
    # them would create a startup-order false-positive when the registrar
    # has only run partially.
    raw_sub_ops = tuple(op for op in sub_op_ids if not op.startswith("vmware.composite."))
    if not raw_sub_ops:
        _PREFLIGHT_CACHE.add(composite_op_id)
        return

    product, version, impl_id = parse_connector_id(connector_id)
    disabled, missing = await _classify_sub_ops(
        raw_sub_ops=raw_sub_ops,
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    # Do NOT cache a negative result (disabled or missing): the operator's
    # expected next action is to remediate and retry, and we want the retry
    # to see fresh state from the DB. Disabled takes precedence over missing
    # when both are present in the same walk -- only one exception can
    # surface, and on a default ingested-but-disabled deploy the disabled
    # remediation (re-enable) is the one the operator needs (#1601).
    if disabled:
        raise CompositeL2DependencyDisabled(
            composite_op_id=composite_op_id,
            disabled_op_ids=tuple(disabled),
            connector_id=connector_id,
        )
    if missing:
        raise CompositeL2DependencyMissing(
            composite_op_id=composite_op_id,
            missing_op_ids=tuple(missing),
            catalog_command=catalog_command_for_vmware_rest(version),
        )
    _PREFLIGHT_CACHE.add(composite_op_id)


async def _classify_sub_ops(
    *,
    raw_sub_ops: tuple[str, ...],
    tenant_id: object,
    product: str,
    version: str,
    impl_id: str,
) -> tuple[list[str], list[str]]:
    """Split non-dispatchable sub-ops into ``(disabled, missing)``.

    Walks each raw-REST sub-op through
    :func:`~meho_backplane.operations._lookup.lookup_descriptor` (which
    hard-filters ``is_enabled = TRUE``). A sub-op that resolves is
    dispatchable and is ignored. A sub-op that does **not** resolve is
    then classified with the ``is_enabled``-agnostic
    :func:`~meho_backplane.operations._lookup.descriptor_exists_any_state`
    probe: a row present in any state means *disabled*; no row means
    *missing*. The probe is used only to classify -- never to dispatch a
    disabled op (#1601). Every non-dispatchable sub-op is reported (not
    just the first found) so the operator sees the full gap in one go.

    ``tenant_id`` is typed :class:`object` for the same import-cycle
    reason as the public helper; the composite handler always passes
    ``operator.tenant_id`` (a UUID) and the runtime check happens in the
    lookup helpers' ORM call.
    """
    disabled: list[str] = []
    missing: list[str] = []
    for sub_op_id in raw_sub_ops:
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,  # type: ignore[arg-type]
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=sub_op_id,
        )
        if descriptor is not None:
            continue
        present_any_state = await descriptor_exists_any_state(
            tenant_id=tenant_id,  # type: ignore[arg-type]
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=sub_op_id,
        )
        if present_any_state:
            disabled.append(sub_op_id)
        else:
            missing.append(sub_op_id)
    return disabled, missing


def reset_preflight_cache() -> None:
    """Clear the per-process preflight cache.

    Test seam (so a unit test can prime the cache, then invalidate it
    to exercise the cache-miss path again). Not called from production
    code today; a future operator-initiated catalog-ingest signal hook
    would call this to force the next composite dispatch to re-check.
    """
    _PREFLIGHT_CACHE.clear()
