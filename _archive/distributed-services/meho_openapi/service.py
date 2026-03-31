"""
OpenAPI Service - Full HTTP service for connector management (Task 32).

Provides complete CRUD operations for connectors, endpoints, and credentials.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meho_openapi.routes import router
import logging


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application for OpenAPI service.
    
    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="MEHO OpenAPI Service",
        description="Dynamic API integration service - connector and endpoint management",
        version="0.1.0"
    )
    
    # Configure CORS
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
