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
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Response

from meho_backplane import __version__
from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.auth.jwt import keycloak_readiness_probe
from meho_backplane.auth.vault import vault_readiness_probe
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
    and registers the Keycloak + Vault readiness probes with the
    registry so ``/ready`` reflects whether each dependency is
    reachable. The Vault probe is registered even though no protected
    route consuming Vault has landed yet (G2.2-T3) — readiness is a
    deployment-shape concern, not a request-path concern.

    There is nothing to tear down at shutdown yet; the ``yield`` /
    ``return`` shape is preserved so future Initiatives (G2.3
    SQLAlchemy engine ``dispose()``) can plug in without restructuring
    the function.
    """
    configure_logging()
    register_probe("keycloak", keycloak_readiness_probe)
    register_probe("vault", vault_readiness_probe)
    yield


app: FastAPI = FastAPI(
    title=_APP_NAME,
    version=__version__,
    description="MEHO governance-layer backplane (chassis-only in v0.1).",
    lifespan=lifespan,
)

# Middleware registration order matters for ASGI: ``add_middleware``
# wraps the existing app, so the *last* middleware added becomes the
# outermost layer. RequestContextMiddleware is the only middleware in
# v0.1, so the order is trivial; subsequent Initiatives (G2.2 JWT
# validation, G2.3 audit) must register *after* the request-context
# middleware so their log calls inherit ``request_id``. Middleware is
# registered before routers so every endpoint (including the Task #19
# health/version/ready surfaces and the Task #20 ``/metrics`` route)
# inherits the request-id binding and the http_requests_total counter.
app.add_middleware(RequestContextMiddleware)

app.include_router(health_router)
app.include_router(version_router)
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
