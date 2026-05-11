# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI application entrypoint.

This module exposes the ``app`` callable consumed by uvicorn /
Gunicorn / k8s probes. v0.1 ships:

* The identity route at ``/``.
* The public operator surfaces — ``/healthz``, ``/version``, ``/ready``
  (Task #19) — backed by a pluggable readiness-probe registry that
  fails closed on the empty default.
* Observability primitives — structured JSON logs to stdout, the
  request-context middleware, and the Prometheus ``/metrics``
  endpoint (Task #20).
* The authenticated federation-proof endpoint at ``/api/v1/health``
  (Task #24), which exercises the entire JWT → Vault chain on every
  call and is what the CLI's ``meho status`` (G2.6-T3) hits.
* The synchronous audit-write middleware (Task #28), which writes
  one row to ``audit_log`` per authenticated request *before* the
  response yields back to the ASGI send chain. Fail-closed on
  insert error: an unaudited request is converted to HTTP 500.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Response

from meho_backplane import __version__
from meho_backplane.api.v1.auth_config import router as api_v1_auth_config_router
from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import keycloak_readiness_probe
from meho_backplane.auth.vault import vault_readiness_probe
from meho_backplane.db.engine import dispose_engine, get_engine
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.health import register_probe
from meho_backplane.health import router as health_router
from meho_backplane.logging import configure_logging
from meho_backplane.metrics import render_metrics
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.version import router as version_router

_APP_NAME: Final[str] = "meho-backplane"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Configures structlog at startup so every log line emitted from this
    point onwards (including the very first request) is JSON-formatted,
    and registers the Keycloak + Vault + DB-migration-state readiness
    probes with the registry so ``/ready`` reflects whether each
    dependency is reachable. Probes are registered even though no
    request-path consumer of the dependency may have landed yet —
    readiness is a deployment-shape concern, not a request-path
    concern.

    The SQLAlchemy async engine is **eagerly** instantiated here (via
    :func:`get_engine`) so that the pool is built and the
    ``DATABASE_URL`` is validated at startup, not on the first
    request. Without this pre-warm the very first ``/ready`` poll the
    kubelet sends absorbs the engine-construction cost, which both
    inflates first-request latency and risks a race where the readiness
    probe is asked to query a database whose engine hasn't been
    constructed yet. The engine factory itself stays lazy
    (process-level cache); the lifespan call is what flips it from
    "lazy on first request" to "eager at process boot".

    On shutdown the SQLAlchemy async engine is disposed so the asyncpg
    connection pool releases its connections cleanly; without the
    explicit ``await engine.dispose()`` the SQLAlchemy 2.x async docs
    warn that the underlying connections may stay reachable only from
    a different event loop, which the GC cannot reliably close.
    """
    configure_logging()
    register_probe("keycloak", keycloak_readiness_probe)
    register_probe("vault", vault_readiness_probe)
    register_probe("db", db_migration_probe)
    # Eager engine construction — see lifespan docstring for why.
    get_engine()
    try:
        yield
    finally:
        await dispose_engine()


app: FastAPI = FastAPI(
    title=_APP_NAME,
    version=__version__,
    description="MEHO governance-layer backplane (chassis-only in v0.1).",
    lifespan=lifespan,
)

# Middleware registration order matters for ASGI: ``add_middleware``
# wraps the existing app, so the *last* middleware added becomes the
# outermost layer (its ``__call__`` runs first on the request side and
# last on the response side). The required runtime order for v0.1 is:
#
#   client → RequestContextMiddleware → AuditMiddleware → router → handler
#
# - ``RequestContextMiddleware`` outermost so ``request_id`` is bound
#   before any inner middleware reads it; the
#   :func:`~meho_backplane.middleware.verify_jwt_and_bind` dependency
#   binds ``operator_sub`` deeper still, inside the handler invocation.
# - ``AuditMiddleware`` directly inside it so the audit row sees both
#   contextvars on the response side, and so its fail-closed 500
#   replacement still passes through ``RequestContextMiddleware``'s
#   header injection (the operator gets ``X-Request-Id`` even on the
#   audit-failure path).
#
# To achieve that with ``add_middleware``'s last-added-is-outermost
# rule, ``AuditMiddleware`` is registered *first* (becomes innermost),
# then ``RequestContextMiddleware`` (becomes outermost). Middleware is
# registered before routers so every endpoint (including the Task #19
# health/version/ready surfaces and the Task #20 ``/metrics`` route)
# inherits the request-id binding and the http_requests_total counter.
app.add_middleware(AuditMiddleware)
app.add_middleware(RequestContextMiddleware)

app.include_router(health_router)
app.include_router(version_router)
app.include_router(api_v1_auth_config_router)
app.include_router(api_v1_health_router)


@app.get("/")
async def root() -> dict[str, str]:
    """Identity route.

    Returns the running app's name and version. Kept alongside
    ``/healthz`` because some legacy probes hit ``/`` instead of
    ``/healthz`` and we want both paths to behave.
    """
    return {"name": _APP_NAME, "version": __version__}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition endpoint.

    Returns the default registry contents (process / GC collectors +
    the ``http_requests_total`` counter the middleware increments) in
    the legacy ``text/plain; version=0.0.4`` format that every
    Prometheus scraper understands.
    """
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
