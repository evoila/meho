# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO - Machine Enhanced Human Operator
Unified Application Entry Point

This is the single FastAPI application that combines all service modules.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from meho_app.api.auth import get_current_user
from meho_app.core.config import get_config
from meho_app.core.observability import configure_observability
from meho_app.core.otel import get_logger
from meho_app.database import get_engine

logger = get_logger(__name__)


async def audit_purge_task() -> None:
    """Background task: purge audit events older than 90 days."""
    from meho_app.database import get_session_maker
    from meho_app.modules.audit.service import AuditService

    sm = get_session_maker()
    async with sm() as session:
        svc = AuditService(session)
        deleted = await svc.purge_old_events(retention_days=90)
        if deleted:
            logger.info(f"Audit purge completed: {deleted} events removed")


async def _rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle rate limit exceeded exceptions.

    Returns a 429 Too Many Requests response with details.
    """
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded. Please try again later.",
            "type": "rate_limit_exceeded",
        },
    )


async def _start_slack_bot():
    """Start Slack bot from first active Slack connector's credentials.

    Queries for the first active Slack connector, resolves its credentials
    via CredentialResolver (SENTINEL_SERVICE_USER), and starts the bot
    in Socket Mode.

    Returns:
        SlackBot instance if started successfully, None if no connector found.
    """
    import os

    from sqlalchemy import select

    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.models import ConnectorModel

    session_maker = get_session_maker()
    async with session_maker() as db:
        # Find first active Slack connector
        stmt = (
            select(ConnectorModel)
            .where(
                ConnectorModel.connector_type == "slack",
                ConnectorModel.is_active.is_(True),
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        connector = result.scalar_one_or_none()

        if not connector:
            logger.info("No active Slack connector found -- bot not started")
            return None

        connector_id = str(connector.id)
        tenant_id = str(connector.tenant_id)
        connector_name = str(connector.name) if connector.name else "Slack"

        # Resolve credentials via CredentialResolver (same pattern as event executor)
        from meho_app.api.config import get_api_config
        from meho_app.modules.connectors.credential_resolver import (
            CredentialResolver,
            SessionType,
        )
        from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker
        from meho_app.modules.connectors.repositories.credential_repository import (
            UserCredentialRepository,
        )

        config = get_api_config()
        cred_repo = UserCredentialRepository(db)
        keycloak_checker = KeycloakUserChecker(
            keycloak_url=config.keycloak_url,
            admin_username=config.keycloak_admin_username,
            admin_password=config.keycloak_admin_password,
        )
        resolver = CredentialResolver(cred_repo, keycloak_checker)
        resolved = await resolver.resolve(
            session_type=SessionType.AUTOMATED_EVENT,
            user_id=CredentialResolver.SENTINEL_SERVICE_USER,
            connector_id=connector_id,
            tenant_id=tenant_id,
        )

        bot_token = resolved.credentials.get("slack_bot_token")
        app_token = resolved.credentials.get("slack_app_token")

        if not bot_token:
            logger.warning(
                f"Slack connector '{connector_name}' has no bot token -- bot not started"
            )
            return None

        mode = os.environ.get("MEHO_SLACK_MODE", "socket")

        from meho_app.modules.connectors.slack.bot import SlackBot

        bot = SlackBot(
            bot_token=bot_token,
            app_token=app_token,
            connector_id=connector_id,
            tenant_id=tenant_id,
            mode=mode,
        )
        await bot.start()
        logger.info(f"Slack bot started for connector '{connector_name}' (mode={mode})")
        return bot


# Import module routers
from meho_app.api.connectors import (
    router as connectors_router,
)
from meho_app.api.observability import (
    router as observability_router,
)
from meho_app.api.routes_admin import (
    router as admin_router,
)
from meho_app.api.routes_audit import (
    router as audit_router,
)

# Import enterprise-only routers (Phase 80)
from meho_app.api.routes_enterprise_sessions import (
    router as enterprise_sessions_router,
)
from meho_app.api.routes_license import (
    router as license_router,
)

# Import API routers (BFF layer)
from meho_app.api.routes_auth import (
    router as auth_router,
)
from meho_app.api.routes_chat import (
    router as chat_router,
)
from meho_app.api.routes_chat_sessions import (
    router as sessions_router,
)
from meho_app.api.routes_data import (
    router as data_router,
)
from meho_app.api.routes_orchestrator_skills import (
    router as orchestrator_skills_router,
)
from meho_app.api.routes_recipes import (
    router as recipes_router,
)
from meho_app.api.routes_tenants import (
    router as tenants_router,
)
from meho_app.modules.agents.routes import (
    router as agent_router,
)
from meho_app.modules.connectors.routes import (
    router as connectors_service_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    # Observability is configured in create_app() before routes are mounted
    # This ensures all HTTP requests are traced from the start

    config = get_config()
    from meho_app.core.feature_flags import get_feature_flags

    flags = get_feature_flags()
    processor = None
    scheduler = None

    # === STARTUP ===
    # TASK-143: Start topology auto-discovery background processor
    if flags.topology and config.topology_auto_discovery_enabled:
        try:
            from meho_app.core.redis import get_redis_client
            from meho_app.database import get_session_maker
            from meho_app.modules.topology.auto_discovery import (
                get_batch_processor,
                get_discovery_queue,
            )

            # Initialize Redis-backed queue
            redis_client = await get_redis_client(config.redis_url)
            queue = await get_discovery_queue(
                redis_client=redis_client,
                queue_key=config.topology_discovery_queue_key,
            )

            # Create and start batch processor using singleton factory
            # This ensures get_processor_instance() can return the processor
            # for immediate triggering when new items are queued
            processor = await get_batch_processor(
                queue=queue,
                session_maker=get_session_maker(),
                batch_size=config.topology_discovery_batch_size,
                interval_seconds=config.topology_discovery_interval_seconds,
            )
            await processor.start()
            logger.info(
                f"Topology auto-discovery processor started "
                f"(batch_size={config.topology_discovery_batch_size}, "
                f"interval={config.topology_discovery_interval_seconds}s)"
            )
        except Exception as e:
            logger.warning(f"Failed to start auto-discovery processor: {e}")
            # Non-fatal - application can run without auto-discovery

    # Sync typed connector operations into knowledge base
    if flags.knowledge:
        try:
            from meho_app.database import get_session_maker
            from meho_app.modules.knowledge.embeddings import get_embedding_provider
            from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
            from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
            from meho_app.modules.knowledge.repository import KnowledgeRepository

            session_maker = get_session_maker()

            # Phase 90.2: Clean up stuck ingestion jobs from previous crashes
            try:
                from meho_app.modules.knowledge.startup import cleanup_stuck_ingestion_jobs

                cleaned = await cleanup_stuck_ingestion_jobs()
                if cleaned:
                    logger.info(f"Cleaned up {cleaned} stuck ingestion job(s)")
            except Exception as e:
                logger.warning(f"Failed to clean up stuck ingestion jobs: {e}")

            async with session_maker() as session:
                repo = KnowledgeRepository(session)
                embedding = get_embedding_provider()
                hybrid = PostgresFTSHybridService(repo, embedding)
                ks = KnowledgeStore(repo, embedding, hybrid)

                sync_funcs = [
                    (
                        "kubernetes",
                        "meho_app.modules.connectors.kubernetes.sync",
                        "sync_all_kubernetes_connectors",
                    ),
                    (
                        "vmware",
                        "meho_app.modules.connectors.vmware.sync",
                        "sync_all_vmware_connectors",
                    ),
                    ("gcp", "meho_app.modules.connectors.gcp.sync", "sync_all_gcp_connectors"),
                    (
                        "proxmox",
                        "meho_app.modules.connectors.proxmox.sync",
                        "sync_all_proxmox_connectors",
                    ),
                    (
                        "prometheus",
                        "meho_app.modules.connectors.prometheus.sync",
                        "sync_all_prometheus_connectors",
                    ),
                    ("loki", "meho_app.modules.connectors.loki.sync", "sync_all_loki_connectors"),
                    (
                        "tempo",
                        "meho_app.modules.connectors.tempo.sync",
                        "sync_all_tempo_connectors",
                    ),
                    (
                        "alertmanager",
                        "meho_app.modules.connectors.alertmanager.sync",
                        "sync_all_alertmanager_connectors",
                    ),
                    (
                        "jira",
                        "meho_app.modules.connectors.jira.sync",
                        "sync_all_jira_connectors",
                    ),
                    (
                        "confluence",
                        "meho_app.modules.connectors.confluence.sync",
                        "sync_all_confluence_connectors",
                    ),
                    (
                        "email",
                        "meho_app.modules.connectors.email.sync",
                        "sync_all_email_connectors",
                    ),
                    (
                        "argocd",
                        "meho_app.modules.connectors.argocd.sync",
                        "sync_all_argocd_connectors",
                    ),
                    (
                        "github",
                        "meho_app.modules.connectors.github.sync",
                        "sync_all_github_connectors",
                    ),
                    (
                        "slack",
                        "meho_app.modules.connectors.slack.sync",
                        "sync_all_slack_connectors",
                    ),
                    (
                        "aws",
                        "meho_app.modules.connectors.aws.sync",
                        "sync_all_aws_connectors",
                    ),
                    (
                        "azure",
                        "meho_app.modules.connectors.azure.sync",
                        "sync_all_azure_connectors",
                    ),
                ]
                for ctype, module_path, func_name in sync_funcs:
                    try:
                        import importlib

                        # nosemgrep: non-literal-import -- module paths from hardcoded (ctype, module_path, func_name) tuples, not user input
                        mod = importlib.import_module(module_path)
                        sync_fn = getattr(mod, func_name)
                        summary = await sync_fn(session, knowledge_store=ks)
                        synced = summary.get("connectors_synced", 0)
                        if synced:
                            logger.info(f"Synced {synced} {ctype} connector(s): {summary}")
                    except Exception as e:
                        logger.warning(f"Failed to sync {ctype} connectors: {e}")

                # Reconciliation pass: ensure knowledge chunks exist for ALL
                # typed connectors. This catches connectors whose chunk creation
                # failed silently during setup (embedding provider down, rate
                # limit, etc.) -- the Kubernetes controller pattern applied to
                # knowledge indexing: check desired state vs actual state on
                # every startup, fix gaps idempotently.
                try:
                    from meho_app.modules.connectors.chunk_reconciler import (
                        reconcile_knowledge_chunks,
                    )

                    reconciled = await reconcile_knowledge_chunks(session, ks)
                    if reconciled:
                        logger.info(
                            f"Knowledge chunk reconciliation: "
                            f"repaired {reconciled} connector(s)"
                        )
                except Exception as e:
                    logger.warning(f"Knowledge chunk reconciliation skipped: {e}")
        except Exception as e:
            logger.warning(f"Connector operations sync skipped: {e}")
    else:
        logger.info("Feature flag: knowledge DISABLED -- skipping connector operations sync")

    # === Seed orchestrator skills ===
    try:
        from sqlalchemy import text

        from meho_app.database import get_session_maker as _get_sm
        from meho_app.modules.orchestrator_skills.seed import (
            ensure_change_correlation_skill,
            ensure_pipeline_trace_skill,
        )

        _session_maker = _get_sm()
        async with _session_maker() as session:
            result = await session.execute(
                text("SELECT DISTINCT tenant_id FROM connector WHERE tenant_id IS NOT NULL")
            )
            tenant_ids = [row[0] for row in result.fetchall()]

        for tid in tenant_ids:
            await ensure_pipeline_trace_skill(tid)
            await ensure_change_correlation_skill(tid)

        if tenant_ids:
            logger.info(f"Seeded orchestrator skills for {len(tenant_ids)} tenant(s)")
    except Exception as e:
        logger.warning(f"Orchestrator skill seeding skipped: {e}")

    # === APScheduler for scheduled tasks ===
    if flags.scheduled_tasks:
        try:
            import meho_app.modules.scheduled_tasks.scheduler as sched_module
            from meho_app.modules.scheduled_tasks.scheduler import (
                create_scheduler,
                sync_scheduler_with_db,
            )

            scheduler = create_scheduler(config.database_url)
            sched_module._scheduler = scheduler  # Set the singleton
            scheduler.start()
            await sync_scheduler_with_db(scheduler)
            logger.info("Scheduled tasks scheduler started")

            # Phase 80: Audit purge only runs in enterprise mode (audit is enterprise-only per D-02)
            from meho_app.core.licensing import get_license_service

            _license_svc = get_license_service()
            if _license_svc.is_enterprise:
                try:
                    scheduler.add_job(
                        audit_purge_task,
                        "interval",
                        hours=24,
                        id="audit_purge",
                        replace_existing=True,
                        name="Audit event purge (90-day retention)",
                    )
                    logger.info("Audit purge job registered (daily, 90-day retention)")
                except Exception as e:
                    logger.warning(f"Failed to register audit purge job: {e}")
        except Exception as e:
            logger.warning(f"Failed to start scheduled tasks scheduler: {e}")
            # Non-fatal -- app can run without scheduler
    else:
        logger.info("Feature flag: scheduled_tasks DISABLED -- skipping scheduler")

    # === Slack bot (Socket Mode) ===
    slack_bot = None
    if flags.slack:
        try:
            slack_bot = await _start_slack_bot()
        except Exception as e:
            logger.warning(f"Slack bot startup failed: {e}")
    else:
        logger.info("Feature flag: slack DISABLED -- skipping Slack bot")

    yield

    # === SHUTDOWN ===
    if processor:
        try:
            await processor.stop()
            logger.info(
                f"Topology auto-discovery processor stopped "
                f"(processed {processor.stats['messages_processed']} messages)"
            )
        except Exception as e:
            logger.warning(f"Error stopping auto-discovery processor: {e}")

    # Shut down APScheduler
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
            logger.info("Scheduled tasks scheduler stopped")
        except Exception as e:
            logger.warning(f"Error stopping scheduler: {e}")

    # Stop Slack bot
    if slack_bot:
        try:
            await slack_bot.stop()
        except Exception as e:
            logger.warning(f"Error stopping Slack bot: {e}")

    # Close shared Redis connection pool
    try:
        from meho_app.core.redis import close_redis_client

        await close_redis_client()
    except Exception as e:
        logger.warning(f"Error closing Redis client: {e}")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from meho_app.core.health import validate_startup_config

    validate_startup_config()

    config = get_config()

    app = FastAPI(
        title="MEHO",
        description="Machine Enhanced Human Operator - Multi-System Diagnostic Agent",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Configure observability for end-to-end request tracing
    # This enables: FastAPI HTTP tracing, PydanticAI LLM tracing,
    # SQLAlchemy DB tracing, httpx outbound call tracing
    engine = get_engine()
    configure_observability(
        app=app,
        engine=engine,
        service_name="meho",
        environment=config.env,
    )

    # CORS middleware (Phase 58: explicit origins, methods, headers -- no wildcards)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    )

    # Rate limiting (TASK-186)
    if config.enable_rate_limiting:
        try:
            from slowapi.errors import RateLimitExceeded
            from slowapi.middleware import SlowAPIMiddleware

            from meho_app.core.rate_limiting import get_limiter

            limiter = get_limiter()
            app.state.limiter = limiter
            app.add_middleware(SlowAPIMiddleware)
            app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
            logger.info("Rate limiting enabled")
        except Exception as e:
            logger.warning(f"Failed to configure rate limiting: {e}")
    else:
        logger.info("Rate limiting disabled")

    # Exception handlers (Phase 23: structured error responses)
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from meho_app.api.errors import (
        MEHOAPIError,
        general_exception_handler,
        http_exception_handler,
        meho_api_error_handler,
        validation_error_handler,
    )

    app.add_exception_handler(MEHOAPIError, meho_api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)

    # Phase 24: Three-tier health endpoints
    # /health — liveness probe, zero I/O (safe for K8s liveness probes)
    @app.get("/health")
    async def health_check():
        return {"status": "healthy"}

    # /ready — readiness probe, checks PostgreSQL, Redis, Keycloak in parallel
    @app.get("/ready")
    async def readiness_check():
        from meho_app.core.health import check_ready

        all_pass, checks = await check_ready()
        body = {
            "status": "ready" if all_pass else "not_ready",
            "checks": {c["name"]: c for c in checks},
        }
        if not all_pass:
            return JSONResponse(status_code=503, content=body)
        return body

    # /status — full diagnostic, requires JWT auth (exposes infrastructure details)
    @app.get("/status")
    async def status_check(user=Depends(get_current_user)):
        from meho_app.core.health import check_status

        return await check_status()

    # Phase 89: Feature-flagged module registration
    from meho_app.core.feature_flags import get_feature_flags

    flags = get_feature_flags()

    # Include always-on module routers (internal APIs for backwards compatibility)
    app.include_router(connectors_service_router, prefix="/connectors", tags=["connectors"])
    app.include_router(agent_router, prefix="/agent", tags=["agent"])

    # Feature-flagged module routers (conditional imports prevent import-time side effects)
    if flags.knowledge:
        from meho_app.api.routes_knowledge import router as knowledge_api_router
        from meho_app.modules.ingestion.routes import router as ingestion_router
        from meho_app.modules.knowledge.routes import router as knowledge_router

        app.include_router(knowledge_router, prefix="/knowledge", tags=["knowledge"])
        app.include_router(ingestion_router, prefix="/ingestion", tags=["ingestion"])
        app.include_router(knowledge_api_router, prefix="/api", tags=["knowledge-api"])
    else:
        logger.info("Feature flag: knowledge DISABLED")

    if flags.topology:
        from meho_app.modules.topology.routes import router as topology_router

        app.include_router(topology_router, prefix="/api", tags=["topology"])
    else:
        logger.info("Feature flag: topology DISABLED")

    if flags.webhooks:
        from meho_app.api.routes_events import router as webhooks_router

        app.include_router(webhooks_router, prefix="/api", tags=["webhooks"])
    else:
        logger.info("Feature flag: webhooks DISABLED")

    if flags.scheduled_tasks:
        from meho_app.api.routes_scheduled_tasks import router as scheduled_tasks_router

        app.include_router(scheduled_tasks_router, prefix="/api", tags=["scheduled-tasks"])
    else:
        logger.info("Feature flag: scheduled_tasks DISABLED")

    # Include always-on API routers (public BFF layer -- community edition)
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(chat_router, prefix="/api", tags=["chat"])
    app.include_router(sessions_router, prefix="/api", tags=["chat-sessions"])
    app.include_router(connectors_router, prefix="/api", tags=["connectors"])
    app.include_router(recipes_router, prefix="/api", tags=["recipes"])
    app.include_router(data_router, prefix="/api", tags=["data"])
    app.include_router(orchestrator_skills_router, prefix="/api", tags=["orchestrator-skills"])

    # Phase 80: Enterprise routers -- only registered with valid license key (D-10)
    # Router-level exclusion: enterprise endpoints don't exist in community mode.
    # This automatically excludes them from the OpenAPI spec (D-11).
    from meho_app.core.licensing import get_license_service

    license_svc = get_license_service()

    if license_svc.is_enterprise:
        app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
        app.include_router(tenants_router, prefix="/api", tags=["tenants"])
        app.include_router(audit_router, prefix="/api/audit", tags=["audit"])
        app.include_router(enterprise_sessions_router, prefix="/api", tags=["enterprise-sessions"])
        logger.info(f"Enterprise edition active (org={license_svc.org})")
    else:
        logger.info("Community edition -- enterprise routers excluded")

    # License endpoint -- always available, public (no auth) per D-13
    app.include_router(license_router, prefix="/api/v1", tags=["license"])

    # Include observability router (conditionally based on feature flag -- community per D-04)
    if config.enable_observability_api:
        app.include_router(observability_router, prefix="/api", tags=["observability"])
        logger.info("Observability API enabled")
    else:
        logger.info("Observability API disabled")

    # Phase 93: MCP Server (Streamable HTTP, gated by feature flag)
    if flags.mcp_server:
        try:
            from meho_app.api.mcp_server import get_mcp_http_app

            mcp_app = get_mcp_http_app()
            app.mount("/mcp", mcp_app)
            logger.info("MCP server mounted at /mcp")
        except Exception as e:
            logger.warning(f"Failed to mount MCP server: {e}")
    else:
        logger.info("Feature flag: mcp_server DISABLED -- skipping MCP server mount")

    return app


# Create app instance on module load for uvicorn
# Usage: uvicorn meho_app.main:app --host 0.0.0.0 --port 8000
app = create_app()
