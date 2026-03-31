"""
Database session management for MEHO API (BFF).

Simplified version that avoids the singleton pattern issues.
Provides session makers for both BFF and OpenAPI databases.
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession, AsyncEngine
from typing import AsyncGenerator, Optional
import os

# Module-level engines (shared, long-lived)
_bff_engine: Optional[AsyncEngine] = None
_openapi_engine: Optional[AsyncEngine] = None
_knowledge_engine: Optional[AsyncEngine] = None
_agent_engine: Optional[AsyncEngine] = None


def get_bff_engine() -> AsyncEngine:
    """
    Get or create the BFF database engine (module-level singleton).
    
    Engines are designed to be long-lived and shared.
    
    Returns:
        AsyncEngine for database connections
    """
    global _bff_engine
    
    if _bff_engine is None:
        database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
        )
        
        _bff_engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    
    return _bff_engine


def create_bff_session_maker() -> async_sessionmaker:
    """
    Create a session maker for BFF using shared engine.
    
    Returns:
        AsyncSessionMaker for creating sessions
    """
    engine = get_bff_engine()
    
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )


async def get_bff_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Get a database session for BFF.
    
    Simplified version that doesn't use global singleton.
    
    Yields:
        AsyncSession for database operations
    """
    session_maker = create_bff_session_maker()
    async with session_maker() as session:
        yield session
        # ✅ Context manager closes session automatically - no manual close() needed!


# ============================================================================
# OpenAPI Database (Task 22)
# ============================================================================

def get_openapi_engine() -> AsyncEngine:
    """
    Get or create the OpenAPI database engine.
    
    Returns:
        AsyncEngine for OpenAPI database connections
    """
    global _openapi_engine
    
    if _openapi_engine is None:
        database_url = os.getenv(
            "OPENAPI_DATABASE_URL",
            os.getenv("DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test")
        )
        
        _openapi_engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    
    return _openapi_engine


def create_openapi_session_maker() -> async_sessionmaker:
    """
    Create a session maker for OpenAPI service database.
    
    Used for direct database access to connectors and endpoints.
    
    Returns:
        AsyncSessionMaker for creating sessions
    """
    engine = get_openapi_engine()
    
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )


# ============================================================================
# Knowledge Database (Task 55)
# ============================================================================

def get_knowledge_engine() -> AsyncEngine:
    """
    Get or create the Knowledge database engine.
    
    Returns:
        AsyncEngine for Knowledge database connections
    """
    global _knowledge_engine
    
    if _knowledge_engine is None:
        database_url = os.getenv(
            "KNOWLEDGE_DATABASE_URL",
            os.getenv("DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test")
        )
        
        _knowledge_engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    
    return _knowledge_engine


def create_knowledge_session_maker() -> async_sessionmaker:
    """
    Create a session maker for Knowledge service database.
    
    Used for ingesting OpenAPI endpoints as searchable knowledge.
    
    Returns:
        AsyncSessionMaker for creating sessions
    """
    engine = get_knowledge_engine()
    
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )


# ============================================================================
# Agent Database (Session 80 - Recipes)
# ============================================================================

def get_agent_engine() -> AsyncEngine:
    """
    Get or create the Agent database engine.
    
    Used for recipes and chat sessions.
    
    Returns:
        AsyncEngine for Agent database connections
    """
    global _agent_engine
    
    if _agent_engine is None:
        database_url = os.getenv(
            "AGENT_DATABASE_URL",
            os.getenv("DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test")
        )
        
        _agent_engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    
    return _agent_engine


def create_agent_session_maker() -> async_sessionmaker:
    """
    Create a session maker for Agent service database.
    
    Used for recipes, chat sessions, and agent plans.
    
    Returns:
        AsyncSessionMaker for creating sessions
    """
    engine = get_agent_engine()
    
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )


async def get_agent_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Get a database session for Agent service.
    
    Yields:
        AsyncSession for database operations
    """
    session_maker = create_agent_session_maker()
    async with session_maker() as session:
        yield session

