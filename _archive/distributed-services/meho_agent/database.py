"""
Database session management for Agent Service.

Provides database access for workflow/plan storage.
"""
# mypy: disable-error-code="no-untyped-def"
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession, AsyncEngine
from typing import AsyncGenerator, Optional
import os

# Module-level engine (shared, long-lived)
_engine: Optional[AsyncEngine] = None


def get_engine() -> AsyncEngine:
    """
    Get or create the Agent database engine (module-level singleton).
    
    Engines are designed to be long-lived and shared.
    
    Returns:
        AsyncEngine for database connections
    """
    global _engine
    
    if _engine is None:
        # Agent service uses the same database as the BFF (shared database architecture)
        database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
        )
        
        _engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    
    return _engine


def create_session_maker() -> async_sessionmaker:
    """
    Create an async session maker for the Agent database.
    
    Returns:
        Async session maker that can be used to create sessions
    """
    engine = get_engine()
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Get a database session (for FastAPI dependency injection).
    
    Yields:
        AsyncSession for database operations
    """
    session_maker = create_session_maker()
    async with session_maker() as session:
        yield session


def reset_engine():
    """
    Reset the engine singleton (for testing).
    
    This forces a new engine to be created on next access.
    """
    global _engine
    if _engine is not None:
        # Note: In production, you'd want to properly dispose of the engine
        # For now, just reset the reference
        _engine = None

