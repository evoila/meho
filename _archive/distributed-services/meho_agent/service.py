"""
Agent HTTP Service - FastAPI application.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meho_agent.routes import router
from meho_core.logfire import configure_logfire
import logging
import os


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application for Agent service.
    
    Returns:
        Configured FastAPI app
    """
    # Configure Logfire for LLM tracing BEFORE creating agents
    # This ensures PydanticAI agents can use Logfire instrumentation
    environment = os.getenv("APP_ENVIRONMENT", "development")
    configure_logfire(service_name="meho-agent", environment=environment)
    
    app = FastAPI(
        title="MEHO Agent Service",
        description="AI agent orchestration for multi-system diagnostics and automation",
        version="0.1.0"
    )
    
    # Configure CORS
    # WARNING: allow_origins=["*"] is a security risk in production
    # This should be restricted to specific origins (e.g., frontend domain)
    # For development/testing only
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # TODO: Restrict to specific origins in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include routes
    app.include_router(router)
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    return app


# Create app instance
app = create_app()

