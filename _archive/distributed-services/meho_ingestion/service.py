"""
Ingestion Service - FastAPI HTTP application.

Exposes webhook endpoints for automated knowledge ingestion.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meho_ingestion.routes import router
from meho_core.logging import setup_logging
from meho_core.config import get_config


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.
    
    Returns:
        Configured FastAPI app
    """
    config = get_config()
    setup_logging(log_level=config.log_level, env=config.env)
    
    app = FastAPI(
        title="MEHO Ingestion Service",
        description="Webhook-based knowledge ingestion",
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
    
    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn
    config = get_config()
    
    uvicorn.run(
        "meho_ingestion.service:app",
        host="0.0.0.0",
        port=config.ingestion_service_port,
        reload=config.env == "dev"
    )

