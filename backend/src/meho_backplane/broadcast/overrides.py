# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Publish-time broadcast-detail resolver + per-tenant override cache.

G6.3-T2 (#379) under Initiative #376. Replaces the static
:func:`~meho_backplane.broadcast.events.classify_op` +
:func:`~meho_backplane.broadcast.events.redact_payload` call at every
publish site with a precedence-aware lookup over per-tenant rules.
Decision-origin is recorded in the audit row's ``payload`` under
``broadcast_detail_origin`` so ``meho audit query`` (G8.1, #334) can
answer "who flipped this credential read to full detail?" forensically.

Precedence ladder (load-bearing for the PII policy):

1. **Per-call request override.** When :func:`read_request_override`
   returns ``"full"`` *and* the op_class is sensitive
   (``credential_read`` / ``audit_query``), the resolver upgrades the
   broadcast to full detail. Origin: ``"request_override"``. Opt-in
   only -- a request to downgrade (``"aggregate"`` on a ``read``) is
   filtered upstream by :func:`read_request_override` and never reaches
   this branch.
2. **Per-tenant override rule** from the per-tenant cache, scope-matched
   against ``raw_params``. Most-specific-wins (scoped beats op-wide),
   ties broken deterministically by the row id. Origin:
   ``f"tenant_rule:{row.id}"``.
3. **Static** :func:`~meho_backplane.broadcast.events.classify_op`
   default -- ``aggregate`` for sensitive classes, ``full`` for
   everything else. Origin: ``"default"``.

Cache layer
-----------

Per-tenant ``dict[UUID, tuple[list[BroadcastOverride], float]]``
populated lazily on the first lookup per tenant; entries expire after
:data:`_CACHE_TTL_SECONDS`. The cache is module-level state mirroring
the :mod:`~meho_backplane.broadcast.client` singleton pattern -- one
process-wide instance, asyncio-friendly because every cache miss
awaits an :class:`AsyncSession`.

T4's (#381) CRUD verbs call :func:`invalidate_tenant_cache` after each
mutation so the next publish on that tenant reloads from the DB
instead of serving a stale rule set up to 60s past the change. Tests
use :func:`reset_overrides_cache_for_testing` to reset module state
between cases.

Fail-open contract
------------------

A DB failure during cache load logs ``broadcast_override_cache_load_failed``
and drops to the default branch -- the publish path is non-load-bearing
for application correctness (the audit row is the canonical record),
and the broadcast feed is the real-time view. Valkey unreachability is
already swallowed by :func:`~meho_backplane.broadcast.publisher.publish_event`;
PG unreachability during the resolver lookup gets the same treatment
here.

References
----------

* Initiative #376 / Task #379.
* T1 schema substrate -- :class:`~meho_backplane.db.models.BroadcastOverride`.
* Cache-singleton precedent -- :mod:`meho_backplane.broadcast.client`.
* Glob matching -- :func:`fnmatch.fnmatchcase` (case-sensitive, no
  regex) per the Initiative's explicit rejection of regex patterns.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.broadcast.events import classify_op
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BroadcastOverride

__all__ = [
    "compute_effective_broadcast_detail",
    "invalidate_tenant_cache",
    "read_request_override",
    "reset_overrides_cache_for_testing",
]


_log = structlog.get_logger(__name__)


#: Per-tenant override-cache TTL. Pinned by Initiative #376: shorter
#: means tighter convergence after a T4 CRUD on a different worker
#: process (which cannot directly invalidate this process's cache);
#: longer means lower publish-hot-path DB load. 60s is the trade-off
#: the Initiative settled on.
_CACHE_TTL_SECONDS: Final[float] = 60.0


#: Module-level per-tenant cache. Value = (rules, monotonic_expires_at).
#: Reading is a thread-cheap (and asyncio-cheap) pure dict lookup; only
#: the cache-miss DB pull awaits.
_TENANT_CACHE: dict[UUID, tuple[list[BroadcastOverride], float]] = {}


#: Op classes that aggregate-only by default per decision #3 in
#: ``docs/planning/v0.2-decisions.md``. The resolver consults this set
#: rather than re-importing the classifier's internal vocabulary, so a
#: future change to :func:`classify_op`'s output taxonomy lands in one
#: place.
_SENSITIVE_OP_CLASSES: Final[frozenset[str]] = frozenset({"credential_read", "audit_query"})


#: Origin label for the default branch (no override matched).
_ORIGIN_DEFAULT: Final[str] = "default"

#: Origin label for the per-call request-override branch.
_ORIGIN_REQUEST_OVERRIDE: Final[str] = "request_override"

#: structlog contextvar key that T3 (#380) will bind from the
#: ``X-Broadcast-Detail`` HTTP header / MCP ``_meta.broadcast_detail``
#: payload. T2 only reads it; T3 binds it.
_REQUEST_OVERRIDE_CONTEXTVAR_KEY: Final[str] = "broadcast_detail_override"


def _default_detail(op_class: str) -> Literal["full", "aggregate"]:
    """Return the static classify_op default detail for *op_class*.

    Mirrors decision #3 -- sensitive classes aggregate-only, everything
    else full. Pulled out as a single source of truth so the redactor
    in :mod:`meho_backplane.broadcast.events` and the resolver agree on
    the same default mapping.
    """
    if op_class in _SENSITIVE_OP_CLASSES:
        return "aggregate"
    return "full"


def read_request_override() -> Literal["full"] | None:
    """Read the per-call broadcast-detail override from structlog contextvars.

    Opt-in-only contract: only ``"full"`` is honored; anything else
    (including ``"aggregate"``, which would be a request to weaken
    policy) maps to ``None``. The resolver therefore can never use this
    branch to downgrade -- it can only upgrade a sensitive class.

    T3 (#380) binds the contextvar from the ``X-Broadcast-Detail`` HTTP
    header and the MCP ``_meta.broadcast_detail`` field. T2 ships the
    read-side shim only; returns ``None`` until T3 lands.
    """
    raw = structlog.contextvars.get_contextvars().get(_REQUEST_OVERRIDE_CONTEXTVAR_KEY)
    if raw == "full":
        return "full"
    return None


def _match_scope(rule: BroadcastOverride, raw_params: dict[str, Any]) -> bool:
    """Return True when *rule*'s scope matches *raw_params*.

    A ``NULL`` ``scope_field`` is an op-wide rule and always matches.
    Non-null scopes key into ``raw_params`` via the allowlist documented
    in :class:`BroadcastOverride` -- ``"namespace"`` reads
    ``raw_params["namespace"]``; ``"target_name"`` reads
    ``raw_params["target"]`` (the publisher pre-merges request params
    and response summary, and the connector's target alias lands under
    the ``target`` key). An unknown ``scope_field`` is a policy error
    (T4's API layer enforces the allowlist) and treated as non-matching
    rather than crashing the publish path; the drift is logged.
    """
    if rule.scope_field is None:
        return True
    if rule.scope_field == "namespace":
        return raw_params.get("namespace") == rule.scope_value
    if rule.scope_field == "target_name":
        return raw_params.get("target") == rule.scope_value
    _log.warning(
        "broadcast_override_unknown_scope_field",
        rule_id=str(rule.id),
        scope_field=rule.scope_field,
    )
    return False


def _select_most_specific(rules: list[BroadcastOverride]) -> BroadcastOverride:
    """Pick the most-specific matching rule from a non-empty list.

    Most-specific = ``scope_field IS NOT NULL`` beats
    ``scope_field IS NULL``. Among equally-specific rules the
    tie-break is the row id rendered as a string -- UUID lexicographic
    order is stable across processes and independent of insertion order,
    so two workers see the same winner when their caches both hold the
    same rule set.
    """
    scoped = [r for r in rules if r.scope_field is not None]
    candidates = scoped if scoped else rules
    return min(candidates, key=lambda r: str(r.id))


async def _load_tenant_rules(tenant_id: UUID) -> list[BroadcastOverride]:
    """Cache-aware load of all override rules for *tenant_id*.

    First lookup per ``(tenant_id, TTL_window)`` issues exactly one
    ``SELECT * FROM broadcast_override WHERE tenant_id = :id`` against
    the ``broadcast_override_tenant_idx`` index. Lookups within
    :data:`_CACHE_TTL_SECONDS` of that hydrate return the cached list
    without touching the DB.

    Fail-open: a DB failure (PG unreachable, connection-pool drain,
    statement error) logs the exception and returns an empty list. The
    empty result is **not** cached -- caching a degraded read would
    extend a transient failure into a 60s window of silent
    "no overrides" verdicts. Subsequent calls within the window retry
    against the DB on each publish until a successful read seeds the
    cache.
    """
    now = time.monotonic()
    entry = _TENANT_CACHE.get(tenant_id)
    if entry is not None:
        rules, expires_at = entry
        if expires_at > now:
            return rules
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(BroadcastOverride).where(
                    BroadcastOverride.tenant_id == tenant_id,
                ),
            )
            fresh_rules = list(result.scalars().all())
    except Exception:
        _log.warning(
            "broadcast_override_cache_load_failed",
            tenant_id=str(tenant_id),
            exc_info=True,
        )
        return []
    _TENANT_CACHE[tenant_id] = (fresh_rules, now + _CACHE_TTL_SECONDS)
    return fresh_rules


def invalidate_tenant_cache(tenant_id: UUID) -> None:
    """Drop *tenant_id*'s cached rules so the next lookup reloads from DB.

    Hook for T4's (#381) CRUD verbs -- every BroadcastOverride mutation
    calls this so the next publish for the tenant sees the change
    immediately instead of waiting up to 60s for the TTL to expire.
    Other tenants' cache entries are untouched.

    Cross-process invalidation is **not** addressed here -- each worker
    process holds its own cache, and a CRUD on worker A does not
    invalidate worker B's cache. The 60s TTL is the convergence bound;
    a tighter coherence guarantee would require a Valkey pub/sub bus
    that is deliberately out of scope for v0.2 (Initiative #376
    "Out of scope": "Cross-tenant cache coherence (none needed -- the
    cache is per-tenant by design)" -- which excludes the harder
    per-tenant cross-worker case for the same reason: TTL is enough).
    """
    _TENANT_CACHE.pop(tenant_id, None)


def reset_overrides_cache_for_testing() -> None:
    """Clear the entire per-tenant cache. Test-only.

    Production code never calls this -- :func:`invalidate_tenant_cache`
    is the correct per-tenant invalidation path. Tests register this in
    an autouse fixture so cache state cannot leak between cases.
    """
    _TENANT_CACHE.clear()


async def compute_effective_broadcast_detail(
    *,
    op_id: str,
    tenant_id: UUID,
    raw_params: dict[str, Any],
    request_override: Literal["full"] | None,
    op_class_override: str | None = None,
) -> tuple[str, Literal["full", "aggregate"], str]:
    """Resolve the broadcast detail for one publish call.

    Returns ``(op_class, detail, origin)``. The three-element tuple is
    intentionally immutable -- downstream callers thread the values
    into :class:`~meho_backplane.broadcast.events.BroadcastEvent`
    construction and into the audit row's ``payload`` dict without ever
    mutating the resolver output.

    Precedence ladder (load-bearing -- the order is the policy):

    1. ``request_override == "full"`` AND ``op_class`` is sensitive →
       ``(op_class, "full", "request_override")``. The
       :func:`read_request_override` shim guarantees the only value
       reaching this branch is the literal ``"full"``; ``"aggregate"``
       requests are silently dropped at the shim, never here.
    2. Per-tenant override row from the cache, glob-matched on
       ``op_id_pattern`` and scope-matched on ``raw_params`` →
       ``(op_class, row.detail, f"tenant_rule:{row.id}")``. Most
       specific (scoped) beats op-wide; id-order tie-break.
    3. Default → ``(op_class, _default_detail(op_class), "default")``.

    ``op_class_override`` lets the HTTP audit middleware honour a
    route-bound ``audit_op_class`` contextvar (the retrieval-usage
    route at ``GET /api/v1/retrieve/usage`` binds it because
    ``meho.retrieval.usage`` has no recognisable suffix and would
    otherwise classify as ``other``). When ``None`` (MCP path, chassis
    HTTP routes) the resolver derives op_class from op_id via
    :func:`classify_op`.

    Glob matching uses :func:`fnmatch.fnmatchcase` -- case-sensitive,
    no regex, mirrors the Initiative's explicit rejection of regex
    patterns as a "configuration footgun".

    Fail-open: a DB failure inside :func:`_load_tenant_rules` produces
    an empty rule set, which makes the resolver drop to the default
    branch (origin=``"default"``). The publish path never blocks on a
    transient DB outage.
    """
    op_class = op_class_override if op_class_override else classify_op(op_id)

    if request_override == "full" and op_class in _SENSITIVE_OP_CLASSES:
        return op_class, "full", _ORIGIN_REQUEST_OVERRIDE

    rules = await _load_tenant_rules(tenant_id)
    matching = [
        rule
        for rule in rules
        if fnmatch.fnmatchcase(op_id, rule.op_id_pattern) and _match_scope(rule, raw_params)
    ]
    if matching:
        winner = _select_most_specific(matching)
        # ``row.detail`` is enforced as ``"full"`` | ``"aggregate"`` at
        # the API layer by T4's Pydantic Literal. The branch documents
        # the runtime invariant for mypy without trusting an unchecked
        # str.
        detail: Literal["full", "aggregate"] = "full" if winner.detail == "full" else "aggregate"
        return op_class, detail, f"tenant_rule:{winner.id}"

    return op_class, _default_detail(op_class), _ORIGIN_DEFAULT
