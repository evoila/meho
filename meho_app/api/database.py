# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Database adapter for API routes.

Provides session makers for backward compatibility during migration.
All services now use the same database, so all session makers return
the unified session maker from meho_app.database.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_app.database import get_db_session, get_session_maker


def create_bff_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    Get session maker for BFF routes (backward compatibility).

    Used by routes that need to create their own session contexts
    for background processing or streaming operations.
    """
    return get_session_maker()


def create_openapi_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    Get session maker for OpenAPI routes (backward compatibility).

    Used by connector routes that need to create their own session contexts.
    """
    return get_session_maker()


def create_knowledge_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    Get session maker for knowledge routes (backward compatibility).

    Used by knowledge routes for document processing and ingestion.
    """
    return get_session_maker()


async def get_agent_session():
    """
    Get agent session dependency (backward compatibility).

    Used by admin routes that need a database session via FastAPI dependency.
    """
    async for session in get_db_session():
        yield session


__all__ = [
    "create_bff_session_maker",
    "create_knowledge_session_maker",
    "create_openapi_session_maker",
    "get_agent_session",
]
