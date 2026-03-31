"""
Database session management for knowledge service.
"""
# mypy: disable-error-code="comparison-overlap,no-any-return,no-untyped-def"
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession, AsyncEngine
from typing import AsyncGenerator, Optional
from meho_core.config import get_config


# Module-level singleton for engine and session maker
_engine: Optional[AsyncEngine] = None
_session_maker: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    """
    Get or create the database engine (singleton).
    
    The engine is created once and reused for all sessions to avoid
    creating multiple connection pools.
    
    NOTE: This function is called during app startup (before any requests)
    and then used as a singleton. While the GIL provides some safety,
    this is designed for single-process usage (which is our deployment model).
    
    Returns:
        AsyncEngine for database connections
    """
    global _engine
    
    # Singleton pattern - safe because called during app startup
    # before any concurrent requests
    if _engine is None:
        config = get_config()
        
        # Environment-based pool sizing for scalability
        if config.env == "prod":
            pool_size = 20      # 20 persistent connections
            max_overflow = 30   # +30 overflow = 50 total
        elif config.env == "staging":
            pool_size = 10
            max_overflow = 20   # 30 total
        else:  # dev, test
            pool_size = 5
            max_overflow = 10   # 15 total
        
        _engine = create_async_engine(
            config.database_url,
            echo=False,  # Disable SQL echo - can cause blocking in dev
            pool_pre_ping=True,  # Health check connections
            
            # Connection pool settings
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=30,         # Wait 30s for connection
            pool_recycle=3600,       # Recycle connections after 1h
            
            # Performance and safety
            connect_args={
                "command_timeout": 60,  # 60s query timeout
                "server_settings": {
                    "application_name": "meho_knowledge",
                    "jit": "off"  # Disable JIT for faster connection
                }
            }
        )
    
    return _engine


def get_session_maker() -> async_sessionmaker:
    """
    Get async session maker (singleton).
    
    Creates sessions from the singleton engine.
    
    Returns:
        AsyncSessionMaker for creating sessions
    """
    global _session_maker
    
    # Simple singleton pattern - Python's GIL makes this safe
    if _session_maker is None:
        engine = get_engine()
        _session_maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    
    return _session_maker


async def reset_engine() -> None:
    """
    Reset engine singleton (for testing).
    
    Properly disposes of the async engine to prevent resource leaks.
    This should only be called in tests to reset the engine between test runs.
    """
    global _engine, _session_maker
    
    if _engine is not None:
        # Properly dispose of async engine
        await _engine.dispose()
        _engine = None
    
    _session_maker = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency to get a database session.
    
    IMPORTANT: Each request gets its OWN session from the connection pool.
    - Sessions are ISOLATED (no data mixups between users)
    - Sessions are REUSED (connection pooling for performance)
    - Sessions auto-close after request (returned to pool)
    
    This design supports thousands of concurrent users safely!
    
    Yields:
        AsyncSession for database operations
    
    Example:
        @app.get("/endpoint")
        async def endpoint(session: AsyncSession = Depends(get_session)):
            # Use session (isolated from other requests)
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()  # Returns connection to pool


def get_single_session():
    """
    Helper to get a session maker for non-dependency usage.
    
    Use this in health checks, debug endpoints, or other non-request contexts
    where you need a session but aren't using FastAPI dependency injection.
    
    Returns:
        Callable that returns an async context manager for AsyncSession
    
    Example:
        async with get_single_session()() as session:
            result = await session.execute(...)
    """
    return get_session_maker()

