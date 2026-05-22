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

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from meho_backplane import __version__
from meho_backplane.api.v1.audit import router as api_v1_audit_router
from meho_backplane.api.v1.auth_config import router as api_v1_auth_config_router
from meho_backplane.api.v1.broadcast_overrides import (
    router as api_v1_broadcast_overrides_router,
)
from meho_backplane.api.v1.connectors_ingest import (
    router as api_v1_connectors_ingest_router,
)
from meho_backplane.api.v1.feed import router as api_v1_feed_router
from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.api.v1.kb import router as api_v1_kb_router
from meho_backplane.api.v1.memory import router as api_v1_memory_router
from meho_backplane.api.v1.operations import router as api_v1_operations_router
from meho_backplane.api.v1.retrieve import router as api_v1_retrieve_router
from meho_backplane.api.v1.retrieve_eval import router as api_v1_retrieve_eval_router
from meho_backplane.api.v1.retrieve_retire import router as api_v1_retrieve_retire_router
from meho_backplane.api.v1.retrieve_usage import router as api_v1_retrieve_usage_router
from meho_backplane.api.v1.targets import router as api_v1_targets_router
from meho_backplane.api.v1.topology import router as api_v1_topology_router
from meho_backplane.api.well_known import router as well_known_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import (
    AUDIENCE_NOT_CONFIGURED_REMEDIATION,
    keycloak_readiness_probe,
)
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
from meho_backplane.mcp.auth import mcp_resource_uri
from meho_backplane.memory import (
    start_memory_expiry_sweeper,
    stop_memory_expiry_sweeper,
)
from meho_backplane.metrics import render_metrics
from meho_backplane.middleware import BroadcastDetailMiddleware, RequestContextMiddleware
from meho_backplane.operations import run_typed_op_registrars
from meho_backplane.operations.ingest import load_catalog
from meho_backplane.retrieval.embedding import get_embedding_service
from meho_backplane.settings import get_settings, parse_bool_env
from meho_backplane.topology import (
    start_topology_history_retention_sweeper,
    start_topology_refresh_scheduler,
    stop_topology_history_retention_sweeper,
    stop_topology_refresh_scheduler,
)
from meho_backplane.ui.auth import UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import ensure_static_dist_dir, static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
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


def _assert_mcp_resource_uri_configured() -> None:
    """Fail loudly at startup when the MCP audience can't be resolved.

    The ``/mcp`` router is mounted unconditionally (no enable flag), so
    a deploy that sets neither ``MCP_RESOURCE_URI`` nor ``BACKPLANE_URL``
    leaves the resolved audience empty and **every** ``/mcp`` request
    fails closed with a 401 — the MCP surface is dark with no signal
    pointing at the cause (consumer dogfood signal #633). Per-request
    fail-closed is correct security posture but a context-free 401 is a
    terrible operability posture: an operator following the published
    runbooks cannot tell the surface is misconfigured rather than down.

    Resolving the same way :func:`meho_backplane.mcp.auth.mcp_resource_uri`
    does and aborting startup converts that silent dark surface into a
    CrashLoopBackOff carrying the actionable remediation — the same
    fail-fast-on-missing-security-config posture the
    :class:`~meho_backplane.settings.Settings` field validators already
    take for ``DATABASE_URL`` / ``BROADCAST_REDIS_URL``, and the same
    crash-loud posture :func:`run_typed_op_registrars` takes for an
    unpopulated dispatch table. The chart now derives a default from
    ``ingress.host`` for the common ingress-fronted deploy, so this
    guard only fires on a genuinely unresolvable config (no ingress,
    nothing set).

    Raises :class:`RuntimeError` with
    :data:`~meho_backplane.auth.jwt.AUDIENCE_NOT_CONFIGURED_REMEDIATION`
    so the lifespan crashes and the operator sees the fix in the pod
    logs, not buried in a per-request 401 body.
    """
    if not mcp_resource_uri():
        raise RuntimeError(AUDIENCE_NOT_CONFIGURED_REMEDIATION)


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


async def _run_lifespan_startup() -> None:
    """Eager init phase of the lifespan: probes, pools, registrars, model.

    Extracted from :func:`lifespan` so the lifespan body stays under
    the chassis code-quality limit on function size. Each helper /
    constructor here owns its own success-or-warn shape; this function
    only sequences them. The order is load-bearing:

    1. Logging configured first so every subsequent ``structlog`` call
       lands in the JSON output the rest of the chassis assumes.
    2. Readiness probes registered before any eager resource so
       ``/ready`` accurately reports state even if a later step crashes.
    3. Eager engine + broadcast-client construction so URL-parse / pool-
       build failures surface at startup rather than first request.
    4. Connector + MCP module auto-discovery so import-time
       ``register_*`` calls run before the first request arrives.
    5. Typed-op registrars run **after** connector discovery (the
       registrars are appended during the import pass).
    6. MCP audience guard last — the eager-init failures above raise
       before this, so the guard's CrashLoopBackOff carries the
       MCP-audience message, not a stale earlier failure.
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
    # Connector-spec catalog parse + schema validation (#743). Loads the
    # packaged catalog.yaml once; a malformed catalog (bad YAML, unknown
    # field, non-PEP-440 spec_info_version, duplicate (product, version))
    # raises here and crashes the lifespan, so CI's app-boot smoke fails
    # instead of the bad catalog surfacing as a 500 on first
    # GET /api/v1/connectors/catalog. Registry-coverage of each entry's
    # requires_connector_class is a CI regression test, not a startup
    # guard (the registry's populated-ness is import-order-dependent
    # under pytest-xdist; see catalog.py).
    load_catalog()
    # Typed-op registration (G0.6-T-Refactor-Vault #390). See the
    # docstring for the contract; runs registrars connectors appended
    # to during the import pass above so descriptor rows are populated
    # before the first dispatch.
    await run_typed_op_registrars()
    # MCP tool / resource auto-discovery (G0.5-T3, #248). Same shape
    # as connector auto-discovery: top-level register_mcp_tool /
    # register_mcp_resource calls run at module import.
    eager_import_mcp_modules()
    # MCP audience guard (G0.8-T4 #633). The /mcp router is mounted
    # unconditionally; a deploy with neither MCP_RESOURCE_URI nor
    # BACKPLANE_URL leaves the audience empty and every /mcp request
    # 401s with no signal. Crash loudly here with the remediation
    # instead of serving a dark, silent surface.
    _assert_mcp_resource_uri_configured()
    # G10.0-T5 (#866) — ensure ``ui/static/dist/`` exists so the
    # StaticFiles mount does not crash on a fresh clone where
    # ``tailwindcss --watch`` has not yet materialised the compiled
    # stylesheet. Idempotent; mkdir(exist_ok=True). The mount serves
    # a 404 on ``/ui/static/dist/tailwind.css`` until the operator
    # runs the Tailwind build -- the operator-facing remediation is
    # documented in ``docs/codebase/ui.md``.
    ensure_static_dist_dir()
    # Embedding model preload (G0.4-T2 #259); loud-but-non-fatal.
    await _preload_embedding_model()


@dataclass
class _BackgroundTasks:
    """Lifespan-owned background ``asyncio`` task handles.

    Returned from :func:`_start_background_tasks`; consumed by
    :func:`_stop_background_tasks` on shutdown. The shape (one optional
    per gated task, one required per always-on task) is what lets the
    ``finally`` branch tolerate the disabled-by-settings shape without
    a per-task ``if``-ladder inside the lifespan body itself.
    """

    topology_scheduler: asyncio.Task[None]
    memory_expiry: asyncio.Task[None] | None
    topology_history: asyncio.Task[None] | None


def _start_background_tasks() -> _BackgroundTasks:
    """Start every lifespan-owned background loop, return their handles.

    Started after :func:`_run_lifespan_startup` returns -- they depend
    on the engine pool, the typed-op registrars, and the connector
    table the eager-init phase populates. Each handle is kept on the
    returned dataclass so :func:`_stop_background_tasks` can cancel +
    await unwind before the DB/redis pools are disposed (an in-flight
    sweep must not race pool teardown).
    """
    settings = get_settings()
    # G9.1-T3 #450 — always on; the cadence + advisory-lock guard live
    # in the scheduler module itself.
    topology_scheduler = start_topology_refresh_scheduler()
    # G5.2-T1 #623 — gated on MEMORY_EXPIRY_ENABLED so operators using
    # an external cleanup mechanism don't double-sweep.
    memory_expiry: asyncio.Task[None] | None = None
    if settings.memory_expiry_enabled:
        memory_expiry = start_memory_expiry_sweeper()
    # G9.3-T6 #858 — gated on TOPOLOGY_HISTORY_PRUNE_ENABLED.
    # ``RETENTION_DAYS=0`` keeps the loop running but every tick is a
    # no-op (heartbeat-only); ``PRUNE_ENABLED=false`` skips starting
    # the loop entirely.
    topology_history: asyncio.Task[None] | None = None
    if settings.topology_history_prune_enabled:
        topology_history = start_topology_history_retention_sweeper()
    return _BackgroundTasks(
        topology_scheduler=topology_scheduler,
        memory_expiry=memory_expiry,
        topology_history=topology_history,
    )


async def _stop_background_tasks(tasks: _BackgroundTasks) -> None:
    """Cancel + await every background task, then dispose pooled resources.

    Stop order is the reverse of start order so each task's session
    borrow can never outlive the engine pool teardown. The opted-out
    branches (``None`` task handles) are tolerated cleanly so a
    disable-and-shutdown sequence does not raise.
    """
    if tasks.topology_history is not None:
        await stop_topology_history_retention_sweeper(tasks.topology_history)
    if tasks.memory_expiry is not None:
        await stop_memory_expiry_sweeper(tasks.memory_expiry)
    await stop_topology_refresh_scheduler(tasks.topology_scheduler)
    await _run_lifespan_shutdown()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Sequences three phases: :func:`_run_lifespan_startup` (probes,
    eager pools, registrars, audience guard, embedding preload),
    :func:`_start_background_tasks` (lifespan-owned ``asyncio`` loops),
    yield (request-handling window), and :func:`_stop_background_tasks`
    (reverse-order shutdown so background tasks don't outlive the
    pools they borrow from).

    Each phase helper carries the load-bearing detail in its own
    docstring; the lifespan body stays thin so the chassis function-
    size budget is not the first thing a future contributor has to
    refactor when adding a fourth background loop.
    """
    await _run_lifespan_startup()
    tasks = _start_background_tasks()
    try:
        yield
    finally:
        await _stop_background_tasks(tasks)


app: FastAPI = FastAPI(
    title=_APP_NAME,
    version=__version__,
    description="MEHO governance-layer backplane (chassis-only in v0.1).",
    lifespan=lifespan,
)

# Middleware registration order matters for ASGI: ``add_middleware``
# wraps the existing app, so the *last* middleware added becomes the
# outermost layer (its ``__call__`` runs first on the request side and
# last on the response side). The required runtime order for v0.2 is:
#
#   client → UISessionMiddleware → CSRFMiddleware
#          → RequestContextMiddleware → BroadcastDetailMiddleware
#          → AuditMiddleware → router → handler
#
# - ``UISessionMiddleware`` (G10.0-T4 #865, mounted by T5 #866)
#   outermost so unauthenticated ``/ui/*`` requests 302 to
#   ``/ui/auth/login`` BEFORE any inner middleware does
#   request-context / audit work. Out-of-prefix paths
#   (``/api/*`` / ``/mcp/*`` / ``/healthz`` / etc.) pass straight
#   through to the inner chain -- the middleware is ``/ui/``-scoped
#   by construction. Registering it *before* (i.e. outside) the
#   JWT dependency chain is what the Initiative #337 acceptance
#   criterion 4 means by "session middleware before JWT
#   middleware": JWT verification is enforced as a route
#   dependency on ``/api/*`` routes, and the session middleware
#   short-circuits ``/ui/*`` requests so the JWT dependency never
#   runs on them. The /api/* surface continues to flow through the
#   inner chain unchanged.
# - ``CSRFMiddleware`` (G10.0-T5 #866) runs second. It guards
#   state-changing ``/ui/*`` requests (POST/PATCH/PUT/DELETE) with
#   the OWASP double-submit cookie pattern. Out-of-prefix paths
#   and read-only methods pass through. The middleware is
#   intentionally registered *outside* the audit chain so a CSRF
#   rejection produces a 403 without writing an unauthenticated
#   audit row (the audit middleware skips when ``operator_sub`` is
#   not bound, which is true on a CSRF rejection).
# - ``RequestContextMiddleware`` next so ``request_id`` is bound
#   before any inner middleware reads it; the
#   :func:`~meho_backplane.middleware.verify_jwt_and_bind` dependency
#   binds ``operator_sub`` deeper still, inside the handler invocation.
# - ``BroadcastDetailMiddleware`` (G6.3-T3 #380) sits between
#   ``RequestContextMiddleware`` and ``AuditMiddleware`` so the
#   ``broadcast_detail_override`` contextvar is bound BEFORE
#   ``AuditMiddleware``'s broadcast resolver consults it on the
#   response side.
# - ``AuditMiddleware`` directly inside ``BroadcastDetailMiddleware``
#   so the audit row sees both contextvars on the response side, and
#   so its fail-closed 500 replacement still passes through the outer
#   middlewares' header injection / cleanup.
#
# To achieve that with ``add_middleware``'s last-added-is-outermost
# rule, ``AuditMiddleware`` is registered *first* (becomes innermost),
# then ``BroadcastDetailMiddleware``, then ``RequestContextMiddleware``,
# then ``CSRFMiddleware``, then ``UISessionMiddleware`` (becomes
# outermost). Middleware is registered before routers so every
# endpoint (including the Task #19 health/version/ready surfaces
# and the Task #20 ``/metrics`` route) inherits the request-id
# binding and the http_requests_total counter.
app.add_middleware(AuditMiddleware)
app.add_middleware(BroadcastDetailMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(UISessionMiddleware)

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
# G4.3-T5 (#444) -- audit-backed retrieval usage telemetry
# (`GET /api/v1/retrieve/usage`). Operator role minimum; the route uses
# `require_role(TenantRole.OPERATOR)` and gates the `tenant_filter`
# cross-tenant parameter behind tenant_admin. Broadcast publishes
# under the canonical op_id `meho.retrieval.usage` + aggregate-only
# `audit_query` class via the `audit_op_id` / `audit_op_class`
# contextvar overrides honoured by the chassis broadcast publisher.
app.include_router(api_v1_retrieve_usage_router)
# G4.3-T2 (#441) -- corpus-driven retrieval-quality eval at
# `POST /api/v1/retrieve/eval`. Operator role minimum; tenant-scoped
# to the operator's JWT claim. Broadcast publishes under the canonical
# op_id `meho.retrieval.eval` + aggregate-only `audit_query` class
# via the same `audit_op_id` / `audit_op_class` contextvar overrides
# T5's retrieve_usage adopted -- the eval queries themselves can be
# operator-sensitive so the broadcast event ships in aggregate-only
# mode.
app.include_router(api_v1_retrieve_eval_router)
# G4.3-T6 (#445) -- retire-decision checklist verb at
# `POST /api/v1/retrieve/retire-checklist`. Combines T2 eval results +
# T5 usage telemetry + a CLI-supplied open-blocker count into the
# five-criterion per-surface green/yellow/red checklist Goal #215
# decision #2 locked. Operator role minimum; tenant_admin gates the
# cross-tenant `tenant_filter` field. Broadcast publishes under the
# canonical op_id `meho.retrieval.retire_checklist` + aggregate-only
# `audit_query` class via the same `audit_op_id` / `audit_op_class`
# contextvar overrides T2 + T5 adopted -- surface filters + blocker
# counts can leak retire-decision intent so the broadcast event ships
# in aggregate-only mode.
app.include_router(api_v1_retrieve_retire_router)
# G0.3-T3 (#254) — targets CRUD surface. All 5 routes are tenant-scoped
# via the JWT's tenant_id claim; cross-tenant reads are impossible.
# G9.1-T5 (#453) extends this router with GET /api/v1/targets/discover
# (registered before GET /{name} so the literal path wins).
app.include_router(api_v1_targets_router)
# G9.1-T5 (#453) — topology REST surface at /api/v1/topology*. Three
# query routes (dependents / dependencies / path) wrapping the T4
# recursive-CTE verbs + POST /refresh/{target_name} wrapping the T3
# refresh service. Operator role minimum; tenant-scoped via the JWT's
# tenant_id claim (the query verbs filter graph_node/graph_edge
# tenant_id; refresh resolves the target tenant-scoped). The fifth
# route (GET /api/v1/targets/discover) lives on the targets router.
app.include_router(api_v1_topology_router)
# G6.1-T4 (#310) -- Server-Sent Events feed at `GET /api/v1/feed`.
# Streams events XADD'd by T3's publish-on-write hook onto
# `meho:feed:{tenant_id}`. Same RBAC gate as /api/v1/retrieve
# (operator role minimum); tenant scoping derives the stream key
# from the JWT's tenant_id claim so cross-tenant subscription is
# impossible by construction.
app.include_router(api_v1_feed_router)
# G0.6-T8 (#399) -- operation meta-tool surface at /api/v1/operations/*.
# Four routes mirroring the three MCP meta-tools (list_operation_groups /
# search_operations / call_operation) plus a tenant-admin-gated descriptor
# inspection endpoint. The same handlers back the MCP transport
# (mcp/tools/operations.py).
app.include_router(api_v1_operations_router)
# G0.7-T6 (#406) -- spec-ingestion + review-queue REST surface at
# /api/v1/connectors*. Seven routes (ingest / list / review / PATCH
# group / PATCH op / enable / disable) that drive the T1+T2+T3 pipeline
# and the T4 review state machine. Tenant-scoped to the JWT's tenant_id
# claim; mutating routes are tenant_admin-gated, read routes are
# operator-gated. The same service layer (IngestionPipelineService +
# ReviewService) backs T5 (CLI verbs) and T7 (admin MCP tools).
app.include_router(api_v1_connectors_ingest_router)
# G4.1-T2 (#416) -- knowledge-base REST surface at /api/v1/kb*.
# Five routes (GET / GET /{slug} / POST / DELETE /{slug} / POST /ingest)
# that expose the T1 :class:`KbService` to operators + agents. Tenant-
# scoped via the JWT's tenant_id claim; cross-tenant reads return 404
# (not 403) to prevent enumerating other tenants by status-code
# differential. Read routes (list / show) require ``operator`` minimum;
# write routes (create / delete / ingest) require ``tenant_admin``.
# Audit + broadcast op_ids: ``kb.list`` / ``kb.show`` / ``kb.create`` /
# ``kb.delete`` / ``kb.ingest`` -- bound via the ``audit_op_id`` /
# ``audit_op_class`` contextvar overrides the chassis publisher honours.
app.include_router(api_v1_kb_router)
# G5.1-T2 (#422) -- memory REST surface at /api/v1/memory*.
# Four routes (POST / GET / GET /{scope}/{slug} / DELETE /{scope}/{slug})
# that expose the T1 :class:`MemoryService` to operators + agents. Tenant-
# scoped via the JWT's tenant_id claim; cross-tenant + cross-user reads
# collapse to 404 (not 403) to prevent enumerating other operators via
# status-code differential. Per-scope RBAC is delegated to the service's
# :class:`MemoryRbacResolver` (e.g. only ``tenant_admin`` writes
# ``tenant``-scoped). Route-layer dependency is split: reads gated by
# ``require_role(READ_ONLY)`` (``GET /api/v1/memory`` +
# ``GET /api/v1/memory/{scope}/{slug}``), writes by ``require_role(OPERATOR)``
# (``POST /api/v1/memory`` + ``DELETE /api/v1/memory/{scope}/{slug}``) --
# the split is load-bearing because :class:`MemoryRbacResolver`
# explicitly allows ``read_only`` operators to read ``tenant`` /
# ``target`` scopes per consumer-needs §G5 L131.
# Audit + broadcast op_ids: ``memory.remember`` / ``memory.list`` /
# ``memory.recall`` / ``memory.forget`` -- bound via the ``audit_op_id`` /
# ``audit_op_class`` contextvar overrides the chassis publisher honours.
app.include_router(api_v1_memory_router)
# G8.1-T2 (#466) -- audit-query REST surface. Four routes (POST /query,
# GET who-touched / my-recent / show) all dispatching through the T1
# substrate (`meho_backplane.audit_query.query_audit`). Operator role
# minimum; tenant-scoped via the JWT's tenant_id claim. Binds the
# `audit_op_id` / `audit_op_class` contextvar overrides so every audit
# row this surface writes carries the canonical `meho.audit.query`
# identity and broadcasts as aggregate-only per decision #3.
app.include_router(api_v1_audit_router)
# G6.3-T4 (#381) -- tenant-admin CRUD verbs for BroadcastOverride
# rules (list / create / delete). Wraps the substrate ORM model T1
# (#378) ships and the resolver-cache invalidation hook T2 (#379)
# ships. RBAC: tenant_admin-only (operator + read_only get 403).
# Tenant-scoped via the JWT's tenant_id claim; cross-tenant probes
# return 404 (never 403) so existence is not leaked across tenant
# boundaries. Every mutation writes an audit row and broadcasts
# under op_class=write.
app.include_router(api_v1_broadcast_overrides_router)
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

# G10.0-T5 (#866) -- Operator console surface. Three pieces ship
# together: the static-asset mount for vendored JS + compiled
# Tailwind, the BFF auth router (login/callback/logout), and the
# umbrella UI router (dashboard + 5 surface stubs).
#
# Mount order:
# * ``/ui/static`` -- StaticFiles wrapping the ``ui/static/``
#   subtree. Covers both ``static/src/vendor/*.js`` (vendored HTMX /
#   Alpine / Cytoscape) and ``static/dist/tailwind.css`` (compiled
#   stylesheet, materialised by ``ensure_static_dist_dir`` at
#   startup). The ``UISessionMiddleware`` short-circuits on the
#   ``/ui/static/`` prefix so unauthenticated browsers can load
#   the styled login page assets.
# * UI auth router -- ``/ui/auth/{login,callback,logout}`` GET
#   routes; reachable unauthenticated (the middleware exempts
#   ``/ui/auth/``).
# * UI router -- ``GET /ui/`` dashboard + the five
#   ``GET /ui/{slug}`` surface stubs. All routes require a session;
#   ``UISessionMiddleware`` redirects to login on miss.
app.mount(
    "/ui/static",
    StaticFiles(directory=str(static_root_dir()), check_dir=False),
    name="ui_static",
)
app.include_router(build_ui_auth_router())
app.include_router(build_ui_router())

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
