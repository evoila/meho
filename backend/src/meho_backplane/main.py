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
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane import __version__
from meho_backplane.agent.reaper import (
    start_agent_run_reaper,
    stop_agent_run_reaper,
)
from meho_backplane.agents import (
    start_grant_expiry_sweeper,
    stop_grant_expiry_sweeper,
)
from meho_backplane.api.v1.agent_grants import router as api_v1_agent_grants_router
from meho_backplane.api.v1.agent_principals import (
    router as api_v1_agent_principals_router,
)
from meho_backplane.api.v1.agent_runs import router as api_v1_agent_runs_router
from meho_backplane.api.v1.agents import router as api_v1_agents_router
from meho_backplane.api.v1.approvals import router as api_v1_approvals_router
from meho_backplane.api.v1.ask_docs import router as api_v1_ask_docs_router
from meho_backplane.api.v1.audit import router as api_v1_audit_router
from meho_backplane.api.v1.auth_config import router as api_v1_auth_config_router
from meho_backplane.api.v1.broadcast_overrides import (
    router as api_v1_broadcast_overrides_router,
)
from meho_backplane.api.v1.connectors_ingest import (
    router as api_v1_connectors_ingest_router,
)
from meho_backplane.api.v1.connectors_ingest import (
    set_llm_client_factory,
)
from meho_backplane.api.v1.conventions import router as api_v1_conventions_router
from meho_backplane.api.v1.doc_collections import router as api_v1_doc_collections_router
from meho_backplane.api.v1.feed import router as api_v1_feed_router
from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.api.v1.kb import router as api_v1_kb_router
from meho_backplane.api.v1.memory import router as api_v1_memory_router
from meho_backplane.api.v1.operations import router as api_v1_operations_router
from meho_backplane.api.v1.retrieve import router as api_v1_retrieve_router
from meho_backplane.api.v1.retrieve_eval import router as api_v1_retrieve_eval_router
from meho_backplane.api.v1.retrieve_retire import router as api_v1_retrieve_retire_router
from meho_backplane.api.v1.retrieve_usage import router as api_v1_retrieve_usage_router
from meho_backplane.api.v1.runbook_runs import router as api_v1_runbook_runs_router
from meho_backplane.api.v1.runbook_templates import router as api_v1_runbook_templates_router
from meho_backplane.api.v1.scheduler import router as api_v1_scheduler_router
from meho_backplane.api.v1.search_docs import router as api_v1_search_docs_router
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
    dispose_broadcast_blocking_client,
    dispose_broadcast_client,
    get_broadcast_client,
)
from meho_backplane.connectors.registry import _eager_import_connectors, registered_product_tokens
from meho_backplane.db.engine import dispose_engine, get_engine
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.docs_search.readiness_probe import docs_backends_readiness_probe
from meho_backplane.events import start_event_drain, stop_event_drain
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
from meho_backplane.operations import run_typed_op_registrars, set_default_reducer
from meho_backplane.operations.ingest import (
    build_anthropic_ingest_llm_client,
    load_catalog,
    stamp_catalog_profiled_connectors,
    validate_catalog_registry_coverage,
    validate_shipped_artifacts,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.retrieval.embedding import get_embedding_service
from meho_backplane.scheduler import start_scheduler, stop_scheduler
from meho_backplane.settings import get_settings, parse_bool_env
from meho_backplane.topology import (
    start_topology_history_retention_sweeper,
    start_topology_refresh_scheduler,
    stop_topology_history_retention_sweeper,
    stop_topology_refresh_scheduler,
)
from meho_backplane.ui.auth import (
    UISessionMiddleware,
    ui_session_expired_exception_handler,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import ensure_static_dist_dir, static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.security_headers import UIFramingHeadersMiddleware
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


def _wire_ingest_llm_client() -> None:
    """Install the production spec-ingestion grouping LLM client (#1386).

    Replaces the fail-closed ``default_llm_client_factory`` holder with
    :func:`~meho_backplane.operations.ingest.build_anthropic_ingest_llm_client`,
    which reuses ``settings.anthropic_api_key`` (the key the agent
    runtime already reads) so non-dry-run ``--catalog`` ingest grouping
    works on deployed backplanes instead of failing closed with 503.

    Installs the factory *callable* — it is not invoked here, so startup
    never crashes on a missing key. The fail-closed
    :class:`~meho_backplane.operations.ingest.LlmClientUnavailable`
    (-> HTTP 503) surfaces only when an ingest actually runs the
    grouping pass on a deploy that configured no key.
    """
    set_llm_client_factory(build_anthropic_ingest_llm_client)


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


def _advise_vault_tenant_scope_unenforced() -> None:
    """Emit a one-time startup advisory when the Vault tenant-scope guard is off.

    The application-layer ``vault.kv.*`` tenant-scope guard (#1643) is
    **default-on** as of #1725
    (``VAULT_KV_TENANT_SCOPE_PREFIX="secret/tenants/{tenant_id}/"``), so the
    common deploy is silent here. An operator only reaches this advisory by
    **explicitly disabling** the guard (setting the prefix back to ``""``) —
    e.g. while still mid-migration with secrets under the retired per-``sub``
    layout. The consequence of running unenforced is silent otherwise: with
    the empty prefix
    :func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`
    is a no-op and cross-tenant ``vault.kv.*`` isolation is unenforced at the
    app layer, with no other signal.

    This logs **one** structured advisory at startup naming the env var that
    re-enables the guard. It is purely observability — it does **not** change
    dispatch behaviour or flip the default; whether to keep the guard
    disabled (and finish migrating the Vault layout) is an explicit
    human/infra decision, documented under "Choosing a layout" in
    ``docs/codebase/connectors-vault-tenant-scope.md``. A deploy that has
    deliberately opted out can ignore the line; otherwise it is the cue that
    tenant isolation is not being enforced at the app layer.

    Mirrors the loud-but-non-fatal advisory shape the rest of the
    lifespan uses (e.g. :func:`_preload_embedding_model`): a single
    ``structlog`` event, no f-strings, no raise.
    """
    if not get_settings().vault_kv_tenant_scope_prefix:
        structlog.get_logger().warning(
            "vault_tenant_scope_unenforced",
            enable_via="VAULT_KV_TENANT_SCOPE_PREFIX",
            doc="docs/codebase/connectors-vault-tenant-scope.md",
        )


async def _run_lifespan_shutdown() -> None:
    """Dispose every long-lived resource the lifespan opened.

    Per-disposer ``try`` / ``except`` so an asyncpg-pool teardown
    failure in :func:`dispose_engine` cannot short-circuit
    :func:`dispose_broadcast_client` (or
    :func:`dispose_broadcast_blocking_client`) and leak the redis
    pool; structlog captures the failure class so operators can chase
    the leak from logs. The two broadcast clients (fast for
    PING/XADD, long-poll for XREAD BLOCK readers — see
    :mod:`meho_backplane.broadcast.client`) get independent dispose
    arms so a failure in one doesn't leak the other's pool.
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
    try:
        await dispose_broadcast_blocking_client()
    except Exception:
        log.exception("dispose_broadcast_blocking_client_failed")


# code-quality-allow: linear boot-step sequence at the 100-line limit;
# #1975 adds one ordered guard whose extraction would obscure the
# documented startup order.
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
    6. MCP audience guard after the eager-init steps, so a failure there
       carries the MCP-audience message, not a stale earlier one; the
       Vault tenant-scope advisory (operability-only) follows it.
    """
    configure_logging()
    register_probe("keycloak", keycloak_readiness_probe)
    register_probe("vault", vault_readiness_probe)
    register_probe("db", db_migration_probe)
    register_probe("broadcast", broadcast_readiness_probe)
    # G4.6-T6 (#1555) — coarse "which search backends are configured"
    # check; observability-only since #1606 (unconfigured optional
    # backends are skipped, never failed — no live round-trip).
    register_probe("docs_backends", docs_backends_readiness_probe)
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
    # GET /api/v1/connectors/catalog.
    load_catalog()
    # Catalog ↔ registry triple coverage (G3.11-T10 #1253). At chassis
    # boot the registry is fully populated (_eager_import_connectors
    # above ran every connectors/<product>/__init__.py registration),
    # so the validator can assert both the class-presence check
    # (#743 crit. b) and the (product, version, impl_id) triple-
    # registration check (T10) without the pytest-xdist import-order
    # hazard the test suite hits. A mismatch fails-fast here rather
    # than surfacing as ``no_connector`` on the first dispatch — the
    # T8 #1242 class of bug (catalog ``version: v3`` vs registry
    # ``version: "3"``) would have crashed the lifespan instead of
    # shipping silently.
    validate_catalog_registry_coverage()
    # Shipped spec/profile dry-run parse (#1964 T1 #1975): a malformed
    # packaged spec_resource / profile_resource crashes boot. See the fn.
    validate_shipped_artifacts()
    # Boot-time ExecutionProfile stamping (#2288): register a
    # ProfiledRestConnector for every catalog row carrying a profile_resource
    # so a shipped, reviewed profile is dispatchable from boot instead of
    # inert package data. Idempotent + gated — a triple already served by a
    # hand-coded class (vmware/sddc) no-ops, and stamping never enables an op
    # (the #1971 review gate stays the interlock). Runs after the dry-run
    # validator above, so it only ever sees well-formed profiles.
    await stamp_catalog_profiled_connectors()
    # Production spec-ingestion grouping LLM client (#1386). Installs the
    # Anthropic-backed factory so non-dry-run `--catalog` ingest grouping
    # works on deployed backplanes (reusing settings.anthropic_api_key)
    # instead of the build-time-only 503. Fail-closed when no key is set
    # — the factory raises LlmClientUnavailable only when invoked.
    _wire_ingest_llm_client()
    # Typed-op registration (G0.6-T-Refactor-Vault #390). See the
    # docstring for the contract; runs registrars connectors appended
    # to during the import pass above so descriptor rows are populated
    # before the first dispatch.
    await run_typed_op_registrars()
    # Real JSONFlux reducer install (G0.6.1-T3 #753). Swaps the
    # dispatcher's module-level :class:`PassThroughReducer` default for
    # the production :class:`JsonFluxReducer`, so every connector's
    # set-shaped response over the v0.1-spec §4 threshold (50 rows / 4 KB)
    # comes back as a markdown summary + :class:`ResultHandle` instead of
    # the raw list. Production-only: tests construct their own reducers
    # via :func:`set_default_reducer`.
    set_default_reducer(JsonFluxReducer())
    # MCP tool / resource auto-discovery (G0.5-T3, #248). Same shape
    # as connector auto-discovery: top-level register_mcp_tool /
    # register_mcp_resource calls run at module import.
    eager_import_mcp_modules()
    # Startup guards (each self-documented in its own docstring): MCP
    # audience guard crashes loudly on an unresolvable /mcp audience
    # (G0.8-T4 #633); the Vault tenant-scope advisory is operability-only
    # (#1673).
    _assert_mcp_resource_uri_configured()
    _advise_vault_tenant_scope_unenforced()
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
    grant_expiry: asyncio.Task[None] | None
    scheduler: asyncio.Task[None] | None
    agent_run_reaper: asyncio.Task[None] | None
    event_drain: asyncio.Task[None] | None


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
    # G11.2-T6 #819 — gated on GRANT_EXPIRY_ENABLED so operators using
    # an external cleanup mechanism don't double-sweep.
    grant_expiry: asyncio.Task[None] | None = None
    if settings.grant_expiry_enabled:
        grant_expiry = start_grant_expiry_sweeper()
    # G11.3-T2 #823 — cron + one-off agent-trigger scheduler. Gated on
    # SCHEDULER_ENABLED so operators using an external orchestrator
    # (or running the test path without a scheduler) can opt out.
    scheduler: asyncio.Task[None] | None = None
    if settings.scheduler_enabled:
        scheduler = start_scheduler()
    # G11.3-T4 #825 — gated on AGENT_RUN_REAPER_ENABLED so operators
    # running an external lease-reclaim mechanism (DBOS Transact, a
    # workflow engine) can disable the in-tree reaper without
    # patching code.
    agent_run_reaper: asyncio.Task[None] | None = None
    if settings.agent_run_reaper_enabled:
        agent_run_reaper = start_agent_run_reaper()
    # G11.3-T3 #824 — event-outbox drain loop. Gated on
    # EVENT_DRAIN_ENABLED so operators using an external orchestrator
    # (or running the test path without the drain) can opt out.
    event_drain: asyncio.Task[None] | None = None
    if settings.event_drain_enabled:
        event_drain = start_event_drain()
    return _BackgroundTasks(
        topology_scheduler=topology_scheduler,
        memory_expiry=memory_expiry,
        topology_history=topology_history,
        grant_expiry=grant_expiry,
        scheduler=scheduler,
        agent_run_reaper=agent_run_reaper,
        event_drain=event_drain,
    )


async def _stop_background_tasks(tasks: _BackgroundTasks) -> None:
    """Cancel + await every background task, then dispose pooled resources.

    Stop order is the reverse of start order so each task's session
    borrow can never outlive the engine pool teardown. The opted-out
    branches (``None`` task handles) are tolerated cleanly so a
    disable-and-shutdown sequence does not raise.
    """
    if tasks.event_drain is not None:
        await stop_event_drain(tasks.event_drain)
    if tasks.agent_run_reaper is not None:
        await stop_agent_run_reaper(tasks.agent_run_reaper)
    if tasks.scheduler is not None:
        await stop_scheduler(tasks.scheduler)
    if tasks.grant_expiry is not None:
        await stop_grant_expiry_sweeper(tasks.grant_expiry)
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
#   client → UIFramingHeadersMiddleware → UISessionMiddleware
#          → CSRFMiddleware → RequestContextMiddleware
#          → BroadcastDetailMiddleware → AuditMiddleware → router
#          → handler
#
# - ``UIFramingHeadersMiddleware`` (L12 hardening) outermost so the
#   clickjacking-defence headers
#   (``Content-Security-Policy: frame-ancestors 'none'`` +
#   ``X-Frame-Options: DENY``) land on EVERY ``/ui/*`` response,
#   including the 302-to-login the inner ``UISessionMiddleware``
#   short-circuits on an unauthenticated request -- a framed login
#   page is itself a clickjacking surface. It is ``/ui/``-scoped by
#   construction (out-of-prefix ``/api/*`` / ``/mcp`` responses pass
#   through unstamped) and is header-only: it never reads the body,
#   the session, or the request context, so its position relative to
#   the inner chain is not load-bearing beyond "outside the redirect".
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
# then ``CSRFMiddleware``, then ``UISessionMiddleware``, then
# ``UIFramingHeadersMiddleware`` (becomes outermost). Middleware is
# registered before routers so every endpoint (including the Task #19
# health/version/ready surfaces and the Task #20 ``/metrics`` route)
# inherits the request-id binding and the http_requests_total counter.
app.add_middleware(AuditMiddleware)
app.add_middleware(BroadcastDetailMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(UISessionMiddleware)
app.add_middleware(UIFramingHeadersMiddleware)

# G0.25 (#1694): app-level HTTPException handler. Intercepts exactly
# the BFF refresh path's ``401 session_expired`` on ``/ui/*`` paths
# and maps it to a ``302 /ui/auth/login?return_to=...`` for HTML
# requests (cookie cleared); every other HTTPException -- including
# the ``/api/*`` structured 401 codes -- delegates to FastAPI's stock
# ``http_exception_handler`` byte-for-byte. Registered against the
# Starlette base class so the one registration covers
# ``fastapi.HTTPException`` raises from route dependencies too (the
# FastAPI handling-errors override pattern).
app.add_exception_handler(StarletteHTTPException, ui_session_expired_exception_handler)

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
# G4.5-T3 (#1521) — federated vendor-document retrieval at
# `POST /api/v1/search_docs` (the `meho-docs` add-on, Initiative #1518).
# Operator role minimum; tenant-scoped via the forwarded operator JWT.
# Enforces the mandatory binary product+version scope (422 fail-closed
# under `corpus_require_filters`, default on), federates to the external
# corpus via the T2 client (`CorpusUnavailable` → 503, never an empty
# 200), and binds the central audit row under the canonical op_id
# `meho.docs.search` + `read` class via the `audit_op_id` /
# `audit_op_class` contextvar overrides. The MCP tool (T4) and CLI verb
# (T5) reuse the same `docs_search.search_docs` service this route fronts.
app.include_router(api_v1_search_docs_router)
# G4.6-T2 (#1917) — grounded, cited answer at `POST /api/v1/ask_docs` (the
# corpus grounded-answer pipeline, Initiative #1912). The synthesis sibling
# of `search_docs`: same operator role + per-collection `meho-docs:<key>`
# entitlement + readiness gate (403 / 409 / 422 mirror `search_docs`),
# single-collection only (no `collections` fan-out), runs the #1916
# expand→retrieve-per-variant→RRF→synthesize pipeline in-process and returns
# `{answer, citations[]}` with #1919-resolved citation links. The #1918
# per-leg structured error model maps onto 502 (`synthesis_malformed`) / 503
# (`expand_failed` / `corpus_unavailable` / `model_unavailable`) — the SAME
# `{detail, leg, cause, message}` envelope the MCP `ask_docs` tool returns on
# `error.data`. Binds the central audit row under the canonical
# `meho.docs.ask` op_id + `read` class.
app.include_router(api_v1_ask_docs_router)
# G4.6-T6 (#1555) — doc-collection readiness probe + lifecycle. Tenant-
# admin-gated probe (success-only write-back of liveness onto the row,
# mirroring probe_target → Target.fingerprint) + enable/disable
# transitions. The collection-scoped search wiring that reads the
# probe-written status lands in T3 (#1552).
app.include_router(api_v1_doc_collections_router)
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
# G12.2-T3 (#1297) -- runbook template REST surface at
# /api/v1/runbooks/templates*. Six routes (POST / GET / GET /{slug} /
# PATCH /{slug} / POST /{slug}/publish / POST /{slug}/deprecate) that
# expose the T2 :class:`RunbookTemplateService` to operators + ops UIs.
# Tenant-scoped via the JWT's tenant_id claim; cross-tenant probes return
# 404 (not 403) by the service's tenant filter. ``list`` requires
# ``operator`` minimum; the other five require ``tenant_admin`` (``show``
# is admin-only -- the opacity floor; the post-completion operator
# exception lives on the run surface in G12.3). Typed-exception mapping:
# TemplateNotFoundError -> 404, TemplateNotDraftError /
# TemplateNotPublishedError -> 400, DuplicateDraftError -> 409,
# InvalidKbSlugError -> 422. Audit + broadcast op_ids:
# ``runbook.draft_template`` / ``runbook.list_templates`` /
# ``runbook.show_template`` / ``runbook.edit_template`` /
# ``runbook.publish_template`` / ``runbook.deprecate_template`` -- bound
# via the ``audit_op_id`` / ``audit_op_class`` contextvar overrides the
# chassis publisher honours. The MCP tools (T4 #1298) reach the same
# service over MCP.
app.include_router(api_v1_runbook_templates_router)
# G12.3-T5 (#1311) -- runbook run REST surface at /api/v1/runbooks/runs*.
# Five routes (POST / POST /{run_id}/next / POST /{run_id}/abort /
# POST /{run_id}/reassign / GET) that expose the T3
# :class:`RunbookRunService` (#1308) to operators + ops UIs. Tenant-scoped
# via the JWT's tenant_id claim; cross-tenant probes on someone else's
# ``run_id`` return 404 by the service's tenant filter (anti-enumeration).
# ``start`` / ``next`` / ``abort`` / ``list`` require ``operator``
# minimum; ``reassign`` requires ``tenant_admin``. The single-assignee
# invariant is enforced at the service layer for ``next`` (a
# tenant_admin who is not the assignee still gets 403); ``abort``
# widens the allowance via a ``caller_is_admin`` flag the route
# computes from ``operator.tenant_role``. Typed-exception mapping:
# RunNotFoundError / TemplateNotFoundError -> 404, NotRunAssigneeError ->
# 403, RunAlreadyTerminalError / PreviousStepFailedError /
# PreviousStepNotVerifiedError / DeprecatedTemplateError -> 400,
# MissingParamsError / VerifyResponseRequiredError /
# VerifyResponseMismatchError -> 422. Audit + broadcast op_ids:
# ``runbook.start_run`` / ``runbook.next_step`` / ``runbook.abort_run`` /
# ``runbook.reassign_run`` / ``runbook.list_runs`` -- bound via the
# ``audit_op_id`` / ``audit_op_class`` contextvar overrides the chassis
# publisher honours. The MCP tools (T6 #1313) reach the same service
# over MCP.
app.include_router(api_v1_runbook_runs_router)
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
# G11.2-T6 (#819) -- agent permission grant management (grant / revoke /
# list / elevate). All verbs gated to tenant_admin. Tenant-scoped via
# the JWT; cross-tenant probes return 404. Every mutation writes an
# audit row and broadcasts under op_class=write.
#
# Registered BEFORE ``api_v1_agents_router`` (G11.2 follow-up #1168):
# FastAPI dispatches routes in include order, so the agents-router's
# ``GET /{name}`` would otherwise shadow ``GET /api/v1/agents/grants``
# (matching ``name="grants"``). Specific prefix first → grants-list
# route resolves correctly; ``GET /api/v1/agents/incident-triage``
# (and every other non-``grants`` name) still falls through to
# ``show_agent`` because the grants router only matches paths under
# its own ``/api/v1/agents/grants`` prefix.
app.include_router(api_v1_agent_grants_router)
# G11.1-T4 (#811) -- agent invocation surface. POST /agents/{name}/run
# (sync block-and-return, or async handle on the timeout / async flag),
# GET /agents/runs (list the tenant's runs, --work-ref filter; work_ref
# I3-T2 #1662), GET /agents/runs/{handle} (poll the durable run state),
# and POST /agents/{name}/run/events (SSE stream of a fresh run's turn /
# tool-call / final events). Operator-level; tenant-scoped via the JWT;
# runs only an enabled definition in the operator's tenant.
#
# Registered BEFORE ``api_v1_agents_router`` for the same reason the
# grants router is (G11.2 follow-up #1168): FastAPI dispatches routes in
# include order, so the agents-router's ``GET /{name}`` would otherwise
# shadow the one-segment ``GET /api/v1/agents/runs`` (matching
# ``name="runs"``). Specific ``/runs`` route first → the list resolves;
# every other name still falls through to ``show_agent``.
app.include_router(api_v1_agent_runs_router)
# G11.1-T2 (#809) -- agent-definition CRUD verbs (list / show / create
# / edit / delete) over the AgentDefinition ORM model. Reads gated to
# operator-level, writes to tenant_admin. Tenant-scoped via the JWT's
# tenant_id claim; cross-tenant probes return 404 (never 403) so
# existence is not leaked across tenant boundaries. Every mutation
# writes an audit row and broadcasts under op_class=write.
app.include_router(api_v1_agents_router)
# G11.2-T1 (#815) -- agent-principal lifecycle (register / list / revoke).
# register creates a Keycloak client tagged kind=agent + inserts a DB row.
# revoke disables the Keycloak client (kill switch) + marks the row revoked.
# Reads gated to operator+; writes gated to tenant_admin.
# Tenant-scoped via the JWT; cross-tenant probes return 404.
app.include_router(api_v1_agent_principals_router)
# G11.3-T5 (#826) -- scheduler-admin surface. GET /scheduler/triggers
# (list, paginated, operator-level), POST /scheduler/triggers (create,
# tenant_admin), DELETE /scheduler/triggers/{id} (cancel, tenant_admin).
# Tenant-scoped via the JWT; tenant_admin may pass tenant_filter / a
# body tenant_id to act cross-tenant for admin operations. Every
# mutation writes an audit row and broadcasts under op_class=write.
app.include_router(api_v1_scheduler_router)
# G11.2-T4/T5 (#817/#818) -- approval queue + surfacing channel.
# GET /approvals (list pending), GET /approvals/{id} (inspect — T5 #818),
# POST /approvals/{id}/approve (approve + re-dispatch via the ``_approved``
# bypass), POST /approvals/{id}/reject. POST routes write the decision
# audit row in the same transaction as the status flip; approve then
# re-dispatches the original call with the original params and returns
# the result. Each create / approve / reject publishes a broadcast event
# (T5) so a ``broadcast_watch`` operator session learns of pending
# requests without polling. Operator-level; tenant-scoped via the JWT.
app.include_router(api_v1_approvals_router)
# G7.1-T2 (#314) -- tenant-conventions CRUD + history (list / show /
# create / update / delete / history). Reads gated to operator+;
# writes gated to tenant_admin. Tenant-scoped via the JWT's
# tenant_id claim; cross-tenant probes return 404. Every write
# inserts both a convention mutation and a
# ``tenant_convention_history`` row in the same transaction, with
# the history row's ``audit_id`` soft-FK referencing the chassis
# audit row via the ``preallocated_audit_id`` contextvar this Task
# adds to the AuditMiddleware. POST/PATCH on a single
# ``operational`` body exceeding the preamble budget surface 422
# at write time per the "kubectl apply --dry-run=server"
# discipline.
app.include_router(api_v1_conventions_router)
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


def _inject_target_product_enum(schema: dict[str, object]) -> None:
    """Mutate ``TargetCreate.product`` in ``schema`` with the live product enum.

    G0.14-T3 (#1144). Reads :func:`registered_product_tokens` and sets
    the ``enum`` + ``description`` on the ``TargetCreate.product``
    JSON Schema property in place. Defensive ``isinstance`` walks let
    the override survive future schema-shape rearrangements without
    raising on a missing key (the worst case becomes "the enum is not
    injected" rather than "the OpenAPI doc fails to serve").
    """
    valid_products = sorted(registered_product_tokens())
    if not valid_products:
        return
    components = schema.get("components")
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return
    target_create = schemas.get("TargetCreate")
    if not isinstance(target_create, dict):
        return
    properties = target_create.get("properties")
    if not isinstance(properties, dict):
        return
    product_field = properties.get("product")
    if not isinstance(product_field, dict):
        return
    product_field["enum"] = list(valid_products)
    product_field["description"] = (
        "Connector product slug. Must match the ``product`` "
        "field of a registered connector class; see "
        "``GET /api/v1/connectors`` for the live list and "
        "``docs/codebase/error-message-shape.md`` for the "
        "422 shape returned on miss."
    )


def build_openapi_schema() -> dict[str, object]:
    """Generate the OpenAPI schema with the ``TargetCreate.product`` enum injected.

    G0.14-T3 (#1144). Overrides the default :meth:`FastAPI.openapi` so
    the ``product`` property on the ``TargetCreate`` request schema
    renders as a JSON Schema enum populated from the live connector
    registry. Without this hook the schema would surface ``product`` as
    a free-form string — the dogfood signal 5 UX miss in
    ``claude-rdc-hetzner-dc#697`` (operator typed ``'kubernetes'``,
    resolver matched on ``'k8s'``, no early signal). Paired with the
    runtime 422 in :func:`~meho_backplane.api.v1.targets.create_target`
    (Options A + C from the task body, sharing one source-of-truth
    helper so they cannot drift).

    Calls :func:`_eager_import_connectors` **unconditionally** so the
    schema is correct even when the override fires before the FastAPI
    lifespan (the OpenAPI snapshot script under
    ``cli/api/snapshot-openapi.py`` calls :meth:`app.openapi` directly
    without running the lifespan). The call is idempotent — every
    connector subpackage is already in ``sys.modules`` after the first
    import, so a second call re-imports nothing.

    It must run unconditionally rather than behind an
    ``if not registered_product_tokens()`` guard: a single connector
    self-registering as an import side-effect (e.g.
    :mod:`meho_backplane.connectors.vault.tenant_paths` importing
    ``connectors.vault.ops``, which triggers the ``connectors.vault``
    package ``__init__`` to register ``VaultConnector`` at import time)
    leaves the registry non-empty but only *partially* populated. The
    old guard would then short-circuit and skip the eager import, so the
    other connector subpackages never load and ``TargetCreate.product``
    collapses to just the one product that happened to self-register
    (#1723 / CI ``CLI API snapshot freshness`` truncated the enum
    18 -> 1). "Registry non-empty" is not "all connectors loaded".

    Caches the result on ``app.openapi_schema`` to match FastAPI's
    own caching behaviour.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema

    _eager_import_connectors()

    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        terms_of_service=app.terms_of_service,
        contact=app.contact,
        license_info=app.license_info,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    _inject_target_product_enum(schema)
    app.openapi_schema = schema
    return schema


app.openapi = build_openapi_schema  # type: ignore[method-assign]


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
