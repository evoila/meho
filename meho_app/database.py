# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unified database configuration for the monolith.
Replaces individual service database.py files.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from meho_app.core.config import get_config


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


# Global engine and session maker
_engine = None
_session_maker = None


def get_engine():
    """Get or create the async database engine."""
    global _engine
    if _engine is None:
        config = get_config()
        _engine = create_async_engine(
            config.database_url,
            echo=False,  # SQL queries are traced via OTEL SQLAlchemy instrumentation
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=600,
        )
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session maker."""
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_maker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting a database session."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        yield session
