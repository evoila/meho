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

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from meho_backplane.features import build_features_block
from meho_backplane.settings import get_settings

__all__ = [
    "ProbeFn",
    "ProbeResult",
    "clear_probes",
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
