# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI application entrypoint.

This module exposes the ``app`` callable consumed by uvicorn /
Gunicorn / k8s probes. v0.1 ships:

* The identity route at ``/``.
* The public operator surfaces - ``/healthz``, ``/version``, ``/ready``
  (Task #19) - backed by a pluggable readiness-probe registry that
  fails closed on the empty default.
* Observability primitives - structured JSON logs to stdout, the
  request-context middleware, and the Prometheus ``/metrics``
  endpoint (Task #20).
* The authenticated federation-proof endpoint at ``/api/v1/health``
  (Task #24), which exercises the entire JWT → Vault chain on every
  call and is what the CLI's ``meho status`` (G2.6-T3) hits.
* The synchronous audit-write middleware (Task #28), which writes
  one row to ``audit_log`` per authenticated request *before* the
  response yields back to the ASGI send chain. Fail-closed on
  insert error: an unaudited request is converted to HTTP 500.

v0.2 adds:

* The MCP Streamable HTTP transport entrypoint at ``/mcp`` (G0.5-T1,
  #246) — JSON-RPC 2.0 dispatch with built-in ``initialize`` / ``ping``
  / ``notifications/initialized`` handlers.
* OAuth 2.1 resource-server protection on ``/mcp`` (G0.5-T2, #247) —
  Bearer-token validation with the MCP canonical URI as the audience
  per RFC 8707 §2, plus the RFC 9728 ``/.well-known/oauth-protected-resource``
  metadata document and the ``WWW-Authenticate: Bearer
  resource_metadata=...`` header on 401. The tool + resource
  registries (T3) and reference tool (T4) land next.
* The in-process fastembed embedding pipeline (G0.4-T2, #259) —
  ``EmbeddingService`` singleton preloaded by the lifespan so the
  first ``index_document`` / ``retrieve`` call doesn't absorb the
  ~1-2 s ONNX model load cost.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import structlog
from fastapi import FastAPI, Response

from meho_backplane import __version__
from meho_backplane.api.v1.auth_config import router as api_v1_auth_config_router
from meho_backplane.api.v1.connectors import router as api_v1_connectors_router
from meho_backplane.api.v1.feed import router as api_v1_feed_router
from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.api.v1.operations import router as api_v1_operations_router
from meho_backplane.api.v1.retrieve import router as api_v1_retrieve_router
from meho_backplane.api.v1.targets import router as api_v1_targets_router
from meho_backplane.api.well_known import router as well_known_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import keycloak_readiness_probe
from meho_backplane.auth.vault import vault_readiness_probe
from meho_backplane.broadcast import (
    broadcast_readiness_probe,
    dispose_broadcast_client,
    get_broadcast_client,
)
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.db.engine import dispose_engine, get_engine
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.health import register_probe
from meho_backplane.health import router as health_router
from meho_backplane.logging import configure_logging
from meho_backplane.mcp import eager_import_mcp_modules
from meho_backplane.mcp import router as mcp_router
from meho_backplane.metrics import render_metrics
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import run_typed_op_registrars
from meho_backplane.retrieval.embedding import get_embedding_service
from meho_backplane.settings import parse_bool_env
from meho_backplane.version import router as version_router

_APP_NAME: Final[str] = "meho-backplane"


async def _preload_embedding_model() -> None:
    """Eagerly load the fastembed ONNX model.

    Failure here is loud-but-non-fatal: the model can still load
    lazily on first request, and a transient network blip on weight
    download must not turn into a CrashLoopBackOff. Operators see
    the failure class in structlog so genuine misconfigurations
    (wrong model name, unwritable cache dir) chase off the
    ``embedding_preload_failed`` event.
    """
    log = structlog.get_logger()
    try:
        await get_embedding_service().encode_one("embedding model preload")
        log.info("embedding_preload_succeeded")
    except Exception as exc:
        log.warning(
            "embedding_preload_failed",
            error_class=type(exc).__name__,
            error_message=str(exc),
        )


async def _run_lifespan_shutdown() -> None:
    """Dispose every long-lived resource the lifespan opened.

    Per-disposer ``try`` / ``except`` so an asyncpg-pool teardown
    failure in :func:`dispose_engine` cannot short-circuit
    :func:`dispose_broadcast_client` and leak the redis pool;
    structlog captures the failure class so operators can chase
    the leak from logs.
    """
    log = structlog.get_logger()
    try:
        await dispose_engine()
    except Exception:
        log.exception("dispose_engine_failed")
    try:
        await dispose_broadcast_client()
    except Exception:
        log.exception("dispose_broadcast_client_failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Configures structlog at startup so every log line emitted from
    this point onwards (including the very first request) is JSON-
    formatted, and registers the Keycloak + Vault + DB-migration-state
    readiness probes with the registry so ``/ready`` reflects whether
    each dependency is reachable. Probes are registered even though
    no request-path consumer of the dependency may have landed yet -
    readiness is a deployment-shape concern, not a request-path
    concern.

    The SQLAlchemy async engine is **eagerly** instantiated here
    (via :func:`get_engine`) so that the pool is built and the
    ``DATABASE_URL`` is validated at startup, not on the first
    request. The fastembed model (G0.4-T2 #259) and the async Valkey
    client backing G6's activity broadcast (#228) are similarly
    eagerly initialised — each owns a helper above for its
    success-or-warn shape.

    G0.6 (#388) added the typed-op registration step: after
    :func:`_eager_import_connectors` runs every connector
    subpackage's import-time ``register_connector_v2`` call,
    :func:`run_typed_op_registrars` walks the registrar list each
    subpackage appended to and runs each registrar against the DB so
    the ``endpoint_descriptor`` rows the dispatcher reads are
    populated before the first request arrives. Failure here is a
    deploy bug, not a runtime condition — the exception propagates
    and the lifespan crashes so the operator sees CrashLoopBackOff
    instead of a quietly-broken dispatch.

    On shutdown :func:`_run_lifespan_shutdown` releases the
    SQLAlchemy + Valkey pools with per-disposer try/except so a
    single failure can't leak a sibling pool.
    """
    configure_logging()
    register_probe("keycloak", keycloak_readiness_probe)
    register_probe("vault", vault_readiness_probe)
    register_probe("db", db_migration_probe)
    register_probe("broadcast", broadcast_readiness_probe)
    # Eager engine construction (G2.3-T2 #258); validates ``DATABASE_URL``
    # at startup so first-request latency doesn't absorb the pool build.
    get_engine()
    # Eager broadcast client construction (G6.1-T1 #307); URL parse
    # failures surface here, not on first /ready.
    get_broadcast_client()
    # Connector auto-discovery (G0.2-T2, #241). Walks every subpackage
    # under `connectors/` so the top-level `register_connector` /
    # `register_connector_v2` calls in each product's `__init__.py`
    # run before the first request arrives.
    _eager_import_connectors()
    # Typed-op registration (G0.6-T-Refactor-Vault #390). See the
    # docstring for the contract; runs registrars connectors appended
    # to during the import pass above so descriptor rows are populated
    # before the first dispatch.
    await run_typed_op_registrars()
    # MCP tool / resource auto-discovery (G0.5-T3, #248). Same shape
    # as connector auto-discovery: top-level register_mcp_tool /
    # register_mcp_resource calls run at module import.
    eager_import_mcp_modules()
    # Embedding model preload (G0.4-T2 #259); loud-but-non-fatal.
    await _preload_embedding_model()
    try:
        yield
    finally:
        await _run_lifespan_shutdown()


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
# G0.4-T5 (#262) -- hybrid retrieval diagnostic surface
# (`POST /api/v1/retrieve`). Mounted unconditionally; the route uses
# `require_role(TenantRole.OPERATOR)` so unauthorised callers get
# 401 / 403, and the audit middleware records every call with a
# privacy-preserving query_hash payload (the raw query is never
# persisted -- per v0.2 sensitivity defaults).
app.include_router(api_v1_retrieve_router)
# G0.3-T3 (#254) — targets CRUD surface. All 5 routes are tenant-scoped
# via the JWT's tenant_id claim; cross-tenant reads are impossible.
app.include_router(api_v1_targets_router)
# G6.1-T4 (#310) -- Server-Sent Events feed at `GET /api/v1/feed`.
# Streams events XADD'd by T3's publish-on-write hook onto
# `meho:feed:{tenant_id}`. Same RBAC gate as /api/v1/retrieve
# (operator role minimum); tenant scoping derives the stream key
# from the JWT's tenant_id claim so cross-tenant subscription is
# impossible by construction.
app.include_router(api_v1_feed_router)
# G0.2-T6 (#245) -- generic connector dispatch at POST /api/v1/connectors/{product}/{op_id}.
# Auth-required (verify_jwt_and_bind); the operator's JWT is forwarded
# to the connector's execute() as part of the pre-G0.3 target stub.
app.include_router(api_v1_connectors_router)
# G0.6-T8 (#399) -- operation meta-tool surface at /api/v1/operations/*.
# Four routes mirroring the three MCP meta-tools (list_operation_groups /
# search_operations / call_operation) plus a tenant-admin-gated descriptor
# inspection endpoint. The same handlers back the MCP transport
# (mcp/tools/operations.py).
app.include_router(api_v1_operations_router)
# MCP Streamable HTTP transport entrypoint (G0.5-T1, #246) and the
# RFC 9728 protected-resource metadata document (G0.5-T2, #247).
#
# Auth posture per route:
# * ``/.well-known/oauth-protected-resource`` — unauthenticated by
#   design. Spec-conforming MCP clients hit this *before* they have a
#   token, to discover the authorisation server. AuditMiddleware's
#   skip rule (no ``operator_sub`` bound) means the route also doesn't
#   write audit rows, which is the intended behaviour for a discovery
#   endpoint.
# * ``/mcp`` — requires a Bearer token whose ``aud`` matches the MCP
#   canonical URI (G0.5-T2). The
#   :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` dependency
#   binds ``operator_sub`` + ``tenant_id`` so AuditMiddleware writes a
#   row per request. G0.5-T5 (#250) layers MCP-specific audit
#   semantics on top.
app.include_router(well_known_router)
app.include_router(mcp_router)

# Opt-in stub routes for end-to-end verification of
# :func:`~meho_backplane.auth.rbac.require_role`. Disabled by default
# so production deploys never expose them; CI flips
# ``MEHO_ENABLE_RBAC_TEST_ROUTE=1`` for the RBAC integration job. The
# import is local to this branch so importing :mod:`meho_backplane.main`
# never pulls the stub module into a production process.
#
# The env var is read directly via :func:`_parse_bool` rather than
# through ``get_settings()`` because module import here happens before
# the rest of the chassis settings (``KEYCLOAK_ISSUER_URL``,
# ``VAULT_ADDR``, ``DATABASE_URL``) may have been pinned. The existing
# test suite assumes ``from meho_backplane.main import app`` succeeds
# without any env var set; routing instantiating ``Settings`` from this
# import path would regress that contract. The same parser keeps the
# truthy-spelling rule consistent with :class:`Settings`.
if parse_bool_env(os.environ.get("MEHO_ENABLE_RBAC_TEST_ROUTE")):
    from meho_backplane.api.v1.rbac_test import (
        router as api_v1_rbac_test_router,
    )

    app.include_router(api_v1_rbac_test_router)


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
