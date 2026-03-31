"""
MEHO API - Backend-for-Frontend (BFF) Service.

Aggregates all MEHO backend services and provides unified API for frontend.
"""
# mypy: disable-error-code="arg-type,no-untyped-def"
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from meho_api.config import get_api_config
from meho_api.errors import (
    MEHOAPIError,
    meho_api_error_handler,
    validation_error_handler,
    http_exception_handler,
    general_exception_handler
)
from meho_core.logging import setup_logging
from meho_core.logfire import configure_logfire
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env file
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    load_dotenv(env_file)


def create_app() -> FastAPI:
    """
    Create and configure MEHO API application.
    
    Returns:
        Configured FastAPI app
    """
    config = get_api_config()
    
    # Configure observability (Logfire for LLM tracing)
    configure_logfire(service_name="meho-api", environment=config.environment)
    
    setup_logging(log_level="INFO", env=config.environment)
    
    app = FastAPI(
        title="MEHO API",
        description="Backend-for-Frontend API for MEHO",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc"
    )
    
    # CORS middleware (production-ready)
    # Use config.cors_origins for allowed origins
    # Development: ["http://localhost:3000", "http://localhost:5173"]
    # Production: Specific frontend domains
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Exception handlers
    app.add_exception_handler(MEHOAPIError, meho_api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
    
    # Include routers
    from meho_api import routes_chat, routes_chat_sessions, routes_knowledge, routes_connectors, routes_auth, routes_recipes, routes_admin
    app.include_router(routes_auth.router, prefix="/api")
    app.include_router(routes_chat.router, prefix="/api")
    app.include_router(routes_chat_sessions.router, prefix="/api")
    app.include_router(routes_knowledge.router, prefix="/api")
    app.include_router(routes_connectors.router, prefix="/api")
    app.include_router(routes_recipes.router, prefix="/api")  # Recipes (TASK-83 unified execution)
    app.include_router(routes_admin.router, prefix="/api")  # Admin config (TASK-77 externalized prompts)
    
    @app.get("/health")
    async def health():
        """Health check endpoint"""
        return {
            "status": "healthy",
            "service": "meho-api",
            "version": "0.1.0"
        }
    
    @app.on_event("startup")
    async def sync_vmware_operations_on_startup():
        """
        Auto-sync VMware operations for all connectors on startup.
        
        This ensures existing connectors get new operations (like PerformanceManager
        metrics) without requiring manual intervention.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            from meho_api.database import create_openapi_session_maker
            from meho_openapi.connectors.vmware import sync_all_vmware_connectors
            
            session_maker = create_openapi_session_maker()
            
            async with session_maker() as session:
                result = await sync_all_vmware_connectors(session)
                
                if result.get("connectors_synced", 0) > 0:
                    logger.info(
                        f"VMware operations synced on startup: "
                        f"{result['connectors_synced']} connector(s) updated"
                    )
        except Exception as e:
            # Don't fail startup if sync fails - log and continue
            logger.warning(f"VMware operation sync on startup failed (non-fatal): {e}")
    
    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn
    config = get_api_config()
    
    uvicorn.run(
        "meho_api.service:app",
        host=config.api_host,
        port=config.api_port,
        reload=config.environment == "development"
    )

