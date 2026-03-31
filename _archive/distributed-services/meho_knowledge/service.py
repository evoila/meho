"""
Knowledge Service - FastAPI HTTP application.

Exposes knowledge management functionality via REST API.
"""
# mypy: disable-error-code="no-untyped-def"
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meho_knowledge.routes import router
from meho_core.config import get_config
from meho_core.logfire import configure_logfire
from meho_core.structured_logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.
    
    Returns:
        Configured FastAPI app
    """
    config = get_config()
    
    # Configure structured logging
    configure_logging(
        service_name="meho-knowledge",
        log_level=config.log_level,
        json_logs=(config.env != "dev")
    )
    
    # Configure Logfire for LLM tracing (if configured)
    configure_logfire(service_name="meho-knowledge", environment=config.env)
    
    logger.info("creating_knowledge_service", environment=config.env)
    
    app = FastAPI(
        title="MEHO Knowledge Service",
        description="Knowledge management with RAG and ACL",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc"
    )
    
    # CORS middleware (configure properly in production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # TODO: Configure for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include routes
    app.include_router(router)
    
    # Pre-initialize singletons to avoid blocking on first request
    @app.on_event("startup")
    async def startup_event():
        """Initialize services at startup"""
        from meho_knowledge.deps import get_object_storage
        
        logger.info("initializing_singletons")
        
        # Initialize ObjectStorage
        try:
            _ = get_object_storage()  # Initialize singleton
            logger.info("object_storage_initialized")
        except Exception as e:
            logger.error("object_storage_init_failed", error=str(e))
        
        logger.info("singletons_initialized")
    
    logger.info("knowledge_service_created", docs_url="/docs")
    
    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn
    config = get_config()
    
    uvicorn.run(
        "meho_knowledge.service:app",
        host=config.api_host,
        port=config.api_port,
        reload=config.env == "dev"
    )
