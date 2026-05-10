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

Usage::

    from meho_backplane.health import register_probe, ProbeResult

    def vault_probe() -> ProbeResult:
        return ProbeResult(name="vault", ok=client.is_authenticated())

    register_probe("vault", vault_probe)
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

__all__ = [
    "ProbeResult",
    "clear_probes",
    "register_probe",
    "router",
    "run_probes",
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


_probes: list[tuple[str, Callable[[], ProbeResult]]] = []


def register_probe(name: str, fn: Callable[[], ProbeResult]) -> None:
    """Register *fn* under *name* in the readiness-probe registry.

    Probes are evaluated in registration order on every ``/ready`` hit.
    The same name may be registered more than once (callers are
    responsible for uniqueness); duplicates simply run twice. This
    permissive contract keeps the registry trivially testable — see
    :func:`clear_probes`.
    """
    _probes.append((name, fn))


def run_probes() -> list[ProbeResult]:
    """Evaluate every registered probe and return the result list.

    Pure pass-through: probe exceptions are *not* caught here. Probes
    are expected to convert their own failures into a ``ProbeResult``
    with ``ok=False``; an uncaught exception is a probe-implementation
    bug and surfacing it as a 500 from ``/ready`` is the correct
    behaviour.
    """
    return [fn() for _, fn in _probes]


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
    """Readiness probe.

    Returns 200 with ``{"status": "ready", "checks": [...]}`` when at
    least one probe is registered and every probe reports ``ok``.
    Returns 503 with ``{"status": "not_ready", "checks": [...]}``
    otherwise — including the fail-closed empty-registry case at the
    chassis stage. The empty case is handled explicitly because
    ``all([])`` is vacuously ``True`` in Python, which would otherwise
    flip the chassis to "ready" with zero evidence.
    """
    results = run_probes()
    ready_ok = bool(results) and all(r.ok for r in results)
    payload = {
        "status": "ready" if ready_ok else "not_ready",
        "checks": [asdict(r) for r in results],
    }
    return JSONResponse(content=payload, status_code=200 if ready_ok else 503)
