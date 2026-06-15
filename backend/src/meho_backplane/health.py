# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Liveness and readiness surfaces plus a pluggable readiness-probe registry.

Two endpoints live here:

* ``GET /healthz`` — process-up signal. Returns 200 unconditionally; never
  consults the probe registry. This is the kubernetes *liveness* contract:
  pod restart on failure.
* ``GET /ready`` — readiness signal. Iterates every probe registered via
  :func:`register_probe`, returning 200 only if every probe passes. With
  an empty registry (the default at the chassis stage), ``/ready``
  returns 503 by design — the backplane fails closed until downstream
  initiatives wire concrete probes (Vault/Keycloak in G2.2, Alembic
  migrations in G2.3).

Probes are plain callables that return a :class:`ProbeResult`. They are
expected to be cheap and synchronous; long-running checks should cache
state out-of-band and have the probe return the cached verdict. v0.1
deliberately ships no timeout / retry / circuit-breaker around probes —
if a probe hangs, ``/ready`` hangs, and the kubelet's own readiness
timeout takes the pod out of rotation.

``/ready`` also exposes a ``features`` block built by
:func:`~meho_backplane.features.build_features_block` (G0.14-T7
#1148). The block enumerates the four gated features
(``agent_runtime``, ``ui_surface``, ``audit_replay``,
``approval_queue``) with their configured / missing-env state so an
operator's single GET answers "which features will work out of the
box on my deploy?". The block is **always present** — emitted on both
the 200 and 503 branches — and is independent of the probe-registry
verdict: a probe failure surfaces under ``checks``, a feature gate
surfaces under ``features``, and the two never mask each other.

Usage::

    from meho_backplane.health import register_probe, ProbeResult

    def vault_probe() -> ProbeResult:
        return ProbeResult(name="vault", ok=client.is_authenticated())

    register_probe("vault", vault_probe)
"""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from meho_backplane.features import build_features_block
from meho_backplane.settings import get_settings

__all__ = [
    "DEFAULT_READINESS_TTL_S",
    "ProbeFn",
    "ProbeResult",
    "clear_probes",
    "clear_readiness_cache",
    "readiness_snapshot",
    "register_probe",
    "router",
    "run_probes",
    "run_probes_async",
]


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single readiness probe.

    Attributes
    ----------
    name:
        Stable identifier surfaced in the ``/ready`` response. Must match
        the ``name`` passed to :func:`register_probe`.
    ok:
        ``True`` if the underlying dependency is healthy from this
        process's perspective.
    detail:
        Optional human-readable context (error message, version banner,
        etc.). Operators read this when ``ok`` is ``False``.
    """

    name: str
    ok: bool
    detail: str | None = None


#: A probe is either a plain callable returning :class:`ProbeResult`
#: synchronously (Keycloak / Vault probes — both use the ``hvac``
#: + ``httpx`` sync clients wrapped where needed) or an ``async def``
#: coroutine returning the same (the DB-migration-state probe — the
#: SQLAlchemy 2.x async engine forces the I/O onto the event loop).
#: The registry stores both shapes; the ``/ready`` handler awaits
#: coroutine-returning probes and calls sync probes inline.
ProbeFn = Callable[[], ProbeResult] | Callable[[], Awaitable[ProbeResult]]


_probes: list[tuple[str, ProbeFn]] = []


def register_probe(name: str, fn: ProbeFn) -> None:
    """Register *fn* under *name* in the readiness-probe registry.

    Probes are evaluated in registration order on every ``/ready`` hit.
    Both synchronous (``def``) and asynchronous (``async def``) probe
    callables are accepted — the registry keeps them in a single list
    and the ``/ready`` handler dispatches via
    :func:`inspect.iscoroutinefunction`. The same name may be
    registered more than once (callers are responsible for
    uniqueness); duplicates simply run twice. This permissive contract
    keeps the registry trivially testable — see :func:`clear_probes`.
    """
    _probes.append((name, fn))


def run_probes() -> list[ProbeResult]:
    """Evaluate every registered **synchronous** probe.

    Async probes are skipped — calling them from a synchronous
    context would either return an un-awaited coroutine (silently
    discarding the I/O) or require spinning a new event loop (which
    would deadlock when called from inside a running loop). The
    ``/ready`` endpoint uses :func:`run_probes_async` instead;
    this function is preserved for the Task #19 contract that
    ``run_probes`` is part of the public registry API and for
    callers that only register sync probes.

    Pure pass-through: probe exceptions are *not* caught here. Probes
    are expected to convert their own failures into a ``ProbeResult``
    with ``ok=False``; an uncaught exception is a probe-implementation
    bug and surfacing it as a 500 from ``/ready`` is the correct
    behaviour.
    """
    results: list[ProbeResult] = []
    for _name, fn in _probes:
        if inspect.iscoroutinefunction(fn):
            continue
        result = fn()
        # An ``async def`` without the ``__wrapped__`` marker still
        # returns a coroutine when called; defensively skip those.
        if inspect.iscoroutine(result):  # pragma: no cover — defensive
            continue
        # Mypy can't see that ``iscoroutinefunction`` already excluded
        # the awaitable branch of the ``ProbeFn`` union at this point,
        # so we narrow explicitly via :func:`isinstance`.
        if isinstance(result, ProbeResult):
            results.append(result)
    return results


async def run_probes_async() -> list[ProbeResult]:
    """Evaluate every registered probe — sync and async alike.

    Async probes are awaited; sync probes are called inline. Probes
    run sequentially in registration order (parallelising readiness
    checks across dependencies is a v0.2 optimisation; v0.1 favours
    deterministic ordering for readable ``/ready`` payloads and
    audit logs).
    """
    results: list[ProbeResult] = []
    for _name, fn in _probes:
        if inspect.iscoroutinefunction(fn):
            results.append(await fn())
        else:
            value = fn()
            # Defensive: a sync-typed callable that returned an
            # awaitable (mis-annotated probe) gets awaited rather
            # than silently dropped on the floor.
            if inspect.isawaitable(value):  # pragma: no cover — defensive
                results.append(await value)
            else:
                results.append(value)
    return results


def clear_probes() -> None:
    """Empty the registry. Test-only — never call from production code."""
    _probes.clear()


#: Default freshness window for :func:`readiness_snapshot`. The UI
#: chassis injects the readiness verdict into every ``/ui/*`` page
#: render via a synchronous Jinja context processor that cannot itself
#: await the probe sweep; the snapshot is computed in the async session
#: middleware and read from ``request.state`` by the processor. Caching
#: the verdict for a short window keeps that hot path at "negligible
#: cost" (issue #1776) — at most one probe sweep per window across all
#: concurrent page loads, rather than a fresh sweep per render — while
#: still surfacing a 503→200 (or 200→503) transition within ~2s. The
#: dashboard, which owns the detailed readiness card, passes
#: ``max_age_s=0`` to force a fresh sweep so its behaviour is unchanged.
DEFAULT_READINESS_TTL_S: float = 2.0


#: Monotonic-clock-stamped cache for :func:`readiness_snapshot`:
#: ``(captured_at_monotonic, snapshot)`` or ``None`` before the first
#: sweep. Guarded by :data:`_readiness_lock` so a burst of concurrent
#: requests triggers a single refresh (single-flight) rather than a
#: thundering herd of probe sweeps.
_readiness_cache: tuple[float, dict[str, object]] | None = None
_readiness_lock = asyncio.Lock()


def _build_readiness_snapshot(results: list[ProbeResult]) -> dict[str, object]:
    """Project probe *results* into the ``{ready, checks}`` snapshot shape.

    The shape mirrors the ``/ready`` payload (minus the ``features``
    block, which is a deploy-config concern orthogonal to the live
    readiness verdict) so the UI footer pill, the dashboard readiness
    card, and ``/ready`` all read one contract. ``ready`` is ``False``
    for an empty registry because ``all([])`` is vacuously ``True`` —
    the chassis must fail closed until concrete probes are wired (see
    the module docstring and :func:`ready`).
    """
    ready_ok = bool(results) and all(r.ok for r in results)
    return {
        "ready": ready_ok,
        "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail or ""} for r in results],
    }


async def readiness_snapshot(
    *,
    max_age_s: float = DEFAULT_READINESS_TTL_S,
    timeout_s: float | None = None,
) -> dict[str, object]:
    """Return a short-TTL-cached readiness snapshot ``{ready, checks}``.

    Runs the full probe sweep (:func:`run_probes_async`, sync + async
    probes alike) at most once per *max_age_s* window and caches the
    result. A cached entry younger than *max_age_s* is returned without
    re-running probes; otherwise the cache is refreshed under a
    single-flight lock so concurrent callers share one sweep.

    Pass ``max_age_s=0`` to force a fresh sweep (and refresh the cache)
    — the dashboard does this so its readiness card stays live while
    every other surface reads the cheap cached verdict.

    The ``checks`` detail mirrors the ``/ready`` payload so a caller can
    surface the same per-probe breakdown without a second sweep.

    *timeout_s* bounds the probe sweep. The registry's probes do live
    network I/O (Keycloak/Vault/DB), run **sequentially**, and carry no
    internal timeout of their own (see :func:`run_probes_async`), so a
    slow or black-holed dependency would otherwise hang the caller
    indefinitely. When *timeout_s* is set and a refresh is required, the
    sweep is wrapped in :func:`asyncio.wait_for`; if it does not finish
    in time the call degrades to a not-ready verdict carrying a single
    synthetic ``timeout`` check and **does not** cache that verdict — a
    transient stall must not pin a misleading result for the rest of the
    TTL window, and the next caller retries a fresh sweep. ``None`` (the
    default) leaves the sweep unbounded: ``GET /ready`` and the dashboard
    (``max_age_s=0``) keep their existing fidelity, while the per-request
    UI hot path passes a short bound (see
    :func:`~meho_backplane.ui.auth.middleware._stash_ui_readiness`).
    """
    global _readiness_cache
    now = time.monotonic()
    cached = _readiness_cache
    if cached is not None and max_age_s > 0 and (now - cached[0]) < max_age_s:
        return cached[1]

    async with _readiness_lock:
        # Re-check under the lock: a peer may have refreshed while we
        # waited to acquire it, so we avoid a redundant second sweep.
        cached = _readiness_cache
        refreshed_now = time.monotonic()
        if cached is not None and max_age_s > 0 and (refreshed_now - cached[0]) < max_age_s:
            return cached[1]
        if timeout_s is None:
            results = await run_probes_async()
        else:
            try:
                results = await asyncio.wait_for(run_probes_async(), timeout_s)
            except TimeoutError:
                # The sweep blew its budget (a probe is hung on network
                # I/O). ``asyncio.wait_for`` has already cancelled the
                # underlying coroutine. ``asyncio.CancelledError`` is a
                # ``BaseException``, not an ``Exception``, so it is *not*
                # caught here and continues to propagate on a genuine
                # task cancellation. Return — but deliberately do NOT
                # cache — a not-ready verdict so the next request retries.
                return {
                    "ready": False,
                    "checks": [
                        {
                            "name": "timeout",
                            "ok": False,
                            "detail": f"readiness probe sweep exceeded {timeout_s}s",
                        }
                    ],
                }
        snapshot = _build_readiness_snapshot(results)
        _readiness_cache = (time.monotonic(), snapshot)
        return snapshot


def clear_readiness_cache() -> None:
    """Drop the cached readiness snapshot. Test-only.

    Production never invalidates the cache out-of-band; the TTL window
    is the only refresh trigger. Tests that register a fresh probe set
    call this so a snapshot cached by an earlier test (potentially on
    the same xdist worker) cannot leak a stale verdict.
    """
    global _readiness_cache
    _readiness_cache = None


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Always returns 200; never inspects the registry."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe with deploy-time feature-gate visibility.

    Returns 200 with
    ``{"status": "ready", "checks": [...], "features": {...}}`` when
    at least one probe is registered and every probe reports ``ok``.
    Returns 503 with
    ``{"status": "not_ready", "checks": [...], "features": {...}}``
    otherwise — including the fail-closed empty-registry case at the
    chassis stage. The empty case is handled explicitly because
    ``all([])`` is vacuously ``True`` in Python, which would otherwise
    flip the chassis to "ready" with zero evidence.

    The ``features`` block (G0.14-T7 #1148) enumerates the four gated
    features and their configured-vs-missing-env state. It is emitted
    on **both** branches — the operator's "is this deploy correctly
    wired?" question is independent of the probe-registry verdict.
    See :func:`meho_backplane.features.build_features_block` for the
    block's shape and the audit table in
    ``docs/codebase/error-message-shape.md`` for why this surface
    exists (signals 16, 17).
    """
    results = await run_probes_async()
    ready_ok = bool(results) and all(r.ok for r in results)
    payload = {
        "status": "ready" if ready_ok else "not_ready",
        "checks": [asdict(r) for r in results],
        "features": build_features_block(get_settings()),
    }
    return JSONResponse(content=payload, status_code=200 if ready_ok else 503)
