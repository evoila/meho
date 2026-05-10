# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SQLAlchemy 2.x async engine + per-request session factory.

The backplane uses **async** SQLAlchemy paired with the ``asyncpg``
driver (per `ADR 0004
<https://github.com/evoila-bosnia/meho-internal/issues/13>`_); every
database I/O path off the request hot loop must be ``await``-able so
the FastAPI event loop never blocks on PostgreSQL. This module owns
three responsibilities:

* **Engine creation** — :func:`get_engine` returns a process-wide
  :class:`sqlalchemy.ext.asyncio.AsyncEngine`. The engine is built
  lazily on first call so that test code can patch ``DATABASE_URL``
  via env vars before the first DB-using request fires; it is cached
  for the lifetime of the process so connection pooling actually
  pools.
* **Session factory** — :func:`get_sessionmaker` returns the
  :func:`sqlalchemy.ext.asyncio.async_sessionmaker` bound to the
  process engine. ``expire_on_commit=False`` matches the SQLAlchemy
  2.x async-doc recommendation: with the default value, attribute
  access on a committed ORM object lazily emits I/O, which inside an
  async route is a footgun (the implicit reload would need its own
  ``await``).
* **Per-request dependency** — :func:`get_session` is the FastAPI
  ``Depends`` used by every authenticated route that touches the
  database (audit middleware in T28; product routes from G3 onward).
  It opens a session, hands it to the route, commits on success, and
  rolls back on any exception. The route never sees ``session.begin``
  directly.

Pool sizing is governed by ``DATABASE_POOL_SIZE`` and
``DATABASE_POOL_TIMEOUT`` (see :mod:`meho_backplane.settings`); the
defaults (10 / 30s) match SQLAlchemy 2.x's published guidance for a
single-replica web service. ``pool_pre_ping=True`` is enabled
unconditionally — the cost is one extra ``SELECT 1`` round-trip per
checkout, the benefit is silent recovery from PG restarts and idle
TCP timeouts which are common in cloud environments. The trade-off is
documented at length in the SQLAlchemy 2.x async docs.

References
----------
* https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
* https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from meho_backplane.settings import get_settings

__all__ = [
    "create_engine_for_url",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "reset_engine_for_testing",
]


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine_for_url(url: str, *, pool_size: int, pool_timeout: float) -> AsyncEngine:
    """Build a configured :class:`AsyncEngine` for *url*.

    Factored out of :func:`get_engine` so tests (and Alembic's
    ``env.py``) can construct one-off engines without hitting the
    process-wide cache. ``pool_pre_ping=True`` is non-negotiable —
    every checkout pays an extra ``SELECT 1`` round-trip in exchange
    for transparent recovery from idle-TCP-timeouts and PG restarts.

    SQLite (the v0.1 dev / test driver via aiosqlite) uses
    :class:`sqlalchemy.pool.StaticPool` and rejects ``pool_size`` /
    ``pool_timeout`` kwargs at engine-construction time. The kwargs
    are pruned out for SQLite URLs so the same factory function works
    for both the production Postgres pool and the local-dev SQLite
    pool. The pruning is keyed on the SQLAlchemy dialect prefix, not
    on a feature-flag, because the asyncpg vs aiosqlite split is
    deterministic from the URL.
    """
    pool_kwargs: dict[str, int | float | bool] = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        pool_kwargs["pool_size"] = pool_size
        pool_kwargs["pool_timeout"] = pool_timeout
    return create_async_engine(url, future=True, **pool_kwargs)


def get_engine() -> AsyncEngine:
    """Return the process-wide :class:`AsyncEngine`, creating on first call.

    The engine is cached at module scope; subsequent callers in the
    same process share the connection pool. Tests that need to drop
    the cache (e.g. after monkeypatching ``DATABASE_URL``) should call
    :func:`reset_engine_for_testing`.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine_for_url(
            settings.database_url,
            pool_size=settings.database_pool_size,
            pool_timeout=settings.database_pool_timeout,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide :class:`async_sessionmaker`.

    ``expire_on_commit=False`` matches the SQLAlchemy 2.x async-doc
    recommendation: with the default ``True``, post-commit attribute
    access lazily emits I/O, which inside an ``async def`` route turns
    into an implicit blocking reload — a footgun the async docs flag
    explicitly.
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional :class:`AsyncSession`.

    Wraps the route handler in an outer transaction: SQLAlchemy commits
    when the ``async with session.begin()`` block exits cleanly, rolls
    back on any exception. The session is closed by the
    ``async_sessionmaker`` context manager regardless of outcome.

    Routes consume this via::

        from fastapi import Depends
        from meho_backplane.db.engine import get_session

        @router.get("/widgets")
        async def list_widgets(session: AsyncSession = Depends(get_session)):
            result = await session.execute(select(Widget))
            return result.scalars().all()

    The audit-write middleware (T28) reuses the same factory but does
    its own session lifecycle management because it must commit *before*
    the route handler returns — synchronous-audit semantics are part of
    Goal #11's DoD.
    """
    async with get_sessionmaker()() as session, session.begin():
        yield session


async def dispose_engine() -> None:
    """Tear down the process-wide engine and reset the cached factory.

    Called from the FastAPI ``lifespan`` shutdown phase so the pool's
    connections are closed cleanly when the worker exits. The
    SQLAlchemy 2.x async docs are emphatic that ``AsyncEngine.dispose``
    must be ``await``\\ ed in async contexts; calling the sync variant
    leaves underlying asyncpg connections reachable only from a
    different event loop, which the GC cannot reliably reach.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_engine_for_testing() -> None:
    """Clear the cached engine + sessionmaker. Test-only.

    Production code never calls this — :func:`dispose_engine` is the
    correct shutdown path. The cache reset is needed in tests that
    swap ``DATABASE_URL`` between cases (e.g. one case using
    aiosqlite, another using a testcontainers PG); without it the
    second case would silently reuse the first case's pool.
    """
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
