# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration test fixtures.

Provides common fixtures for integration tests including:
- Database session (PostgreSQL with pgvector)
- Knowledge repository and embeddings
- Tenant fixtures
- User context fixtures

Schema is set up exactly once per pytest session by running
``alembic -c meho_app/alembic.ini upgrade head`` against the test database;
each test gets a clean database via ``TRUNCATE ... RESTART IDENTITY CASCADE``
(see ``db_session``). This mirrors how production initializes the schema --
if a migration is broken, the test suite fails at fixture setup rather than
silently masking the problem with ``metadata.create_all``.
"""

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text


# ============================================================================
# Database Fixtures
# ============================================================================


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _REPO_ROOT / "meho_app" / "alembic.ini"


def _run_alembic_upgrade(database_url: str) -> None:
    """
    Apply the unified Alembic migration tree against ``database_url``.

    Synchronous on purpose: Alembic's command API is sync, and the
    session-scoped fixture that drives this is sync as well to dodge the
    pytest-asyncio session-loop coordination problem. Uses
    :mod:`alembic.command` rather than shelling out so the test suite fails
    with a Python traceback when something is wrong, not a process exit code
    in CI logs.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def _test_database_url() -> str:
    """Resolve the test database URL with a sane default (matches pytest setup)."""
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
    )


@pytest.fixture(scope="session")
def _migrate_test_database() -> None:
    """
    Run Alembic ``upgrade head`` against the test database exactly once per
    pytest session. Synchronous and session-scoped on purpose:

    * pytest-asyncio's default fixture loop scope is ``function``; promoting
      this to a session-scoped *async* fixture would require coordinating a
      session-scoped event loop, which collides with per-test event loops
      that integration tests already rely on. Migrations are blocking work
      anyway -- there is no reason to schedule them on an event loop.
    * Failures here are fatal by design. A broken migration must abort the
      whole suite at setup with the Alembic traceback intact, rather than
      silently no-op'ing tests against a half-built schema (which is what
      the pre-#315 ``metadata.create_all`` path did).

    Not ``autouse``: the bootstrap-style integration tests (e.g.
    ``test_first_run_arm64.py``) intentionally run *before* a stack is up,
    so an autouse Alembic upgrade against ``localhost:5432`` would fail at
    session setup before the test's own ``clean_compose_stack`` fixture got
    a chance to run. Tests that need the schema declare ``db_session``
    (which lists this fixture as a positional dependency); tests that bring
    up their own stack are unaffected.
    """
    _run_alembic_upgrade(_test_database_url())


async def _truncate_all_tables(engine) -> None:
    """
    TRUNCATE every table in the public schema except ``alembic_version``.

    ``RESTART IDENTITY CASCADE`` resets sequences and follows foreign-key
    chains so tests do not have to know table dependency order. Wrapping
    every table in a single ``TRUNCATE`` statement is dramatically faster
    than per-test ``DROP/CREATE`` (~10ms vs ~2s on a typical dev laptop).
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
            )
        )
        tables = [row[0] for row in result.fetchall()]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table names sourced from pg_tables (Postgres catalog), never user input; quoting is double-quote-wrapped on each name above
            await conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def db_session(_migrate_test_database) -> AsyncGenerator:
    """
    Function-scoped database session with per-test isolation.

    Schema setup happens exactly once per session via
    :func:`_migrate_test_database` (Alembic upgrade head); this fixture
    creates a fresh async engine per test (cheap with ``NullPool``), opens
    a session, then TRUNCATEs every non-Alembic table after the test
    finishes. Tests can rely on a clean database without paying the cost
    of re-running ``CREATE TABLE`` statements, and the test suite fails
    loudly at fixture setup if a migration is broken.

    Requires PostgreSQL with pgvector running and ``DATABASE_URL`` to point
    at the test database (defaults to ``meho_test`` on localhost:5432).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_test_database_url(), echo=False, poolclass=NullPool)
    try:
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_maker() as session:
            yield session
        await _truncate_all_tables(engine)
    finally:
        await engine.dispose()


# ============================================================================
# Knowledge Service Fixtures
# ============================================================================


@pytest.fixture
async def knowledge_repository(db_session):
    """Create KnowledgeRepository for testing"""
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    return KnowledgeRepository(db_session)


async def create_test_chunk(
    repository,
    text: str,
    tenant_id: str,
    chunk_id: str | None = None,
    source_id: str | None = None,
    source_type: str | None = None,
    metadata: dict | None = None,
    embedding: list | None = None,
):
    """
    Helper function to create test chunks with simplified API.

    Maps old test parameters to new KnowledgeChunkCreate schema.
    """
    from meho_app.modules.knowledge.schemas import (
        ChunkMetadata,
        KnowledgeChunkCreate,
        KnowledgeType,
    )

    # Build metadata if provided
    search_metadata = None
    if metadata:
        search_metadata = ChunkMetadata(**metadata)

    # Build source_uri from source_id and source_type
    source_uri = None
    if source_id and source_type:
        source_uri = f"{source_type}://{source_id}"
    elif source_id:
        source_uri = source_id

    chunk_create = KnowledgeChunkCreate(
        text=text,
        tenant_id=tenant_id,
        source_uri=source_uri,
        search_metadata=search_metadata,
        knowledge_type=KnowledgeType.DOCUMENTATION,
    )

    return await repository.create_chunk(chunk_create, embedding=embedding)


@pytest.fixture
def knowledge_embeddings():
    """Create mock embedding provider for testing"""
    import hashlib

    class MockEmbeddings:
        def embed_query(self, text: str):
            """Generate deterministic 1536-dimensional embedding"""
            hash_val = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)  # noqa: S324 -- non-security hash context in tests
            # Return a 1536-dimensional vector (OpenAI embedding size)
            vector = [(hash_val + i) % 100 / 100.0 for i in range(1536)]
            return vector

        def embed_text(self, text: str, input_type: str = "query"):
            """Alias for embed_query"""
            return self.embed_query(text)

        def embed_documents(self, texts: list):
            """Generate embeddings for multiple documents"""
            return [self.embed_query(text) for text in texts]

    return MockEmbeddings()


# ============================================================================
# Tenant Fixtures
# ============================================================================


@pytest.fixture
def tenant_a():
    """Test tenant A (for multi-tenant tests)"""
    from meho_app.core.auth_context import UserContext

    tenant_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    return UserContext(tenant_id=tenant_id, user_id="user-a", roles=["user"], groups=[])


@pytest.fixture
def tenant_b():
    """Test tenant B (for multi-tenant tests)"""
    from meho_app.core.auth_context import UserContext

    tenant_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    return UserContext(tenant_id=tenant_id, user_id="user-b", roles=["user"], groups=[])


@pytest.fixture
def test_tenant_id():
    """Generate a test tenant ID (string format)"""
    return str(uuid4())


@pytest.fixture
def test_user_context(test_tenant_id):
    """Create user context for testing"""
    from meho_app.core.auth_context import UserContext

    # tenant_id should be a string, not UUID
    tenant_str = str(test_tenant_id) if not isinstance(test_tenant_id, str) else test_tenant_id

    return UserContext(tenant_id=tenant_str, user_id="test-user", roles=["admin"], groups=[])


# ============================================================================
# API Test Fixtures
# ============================================================================


@pytest.fixture
def client():
    """Create FastAPI test client for BFF"""
    from fastapi.testclient import TestClient
    from meho_app.api.service import create_app

    app = create_app()
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Create a mock user context for testing"""
    from meho_app.core.auth_context import UserContext

    return UserContext(
        user_id="test@example.com", tenant_id="test-tenant-id", roles=["admin"], groups=[]
    )


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user context"""
    from meho_app.core.auth_context import UserContext

    return UserContext(
        user_id="admin@example.com", tenant_id="test-tenant-id", roles=["admin"], groups=[]
    )


@pytest.fixture
def mock_global_admin():
    """Create a mock global admin user context"""
    from meho_app.core.auth_context import UserContext

    return UserContext(
        user_id="superadmin@meho.local", tenant_id="master", roles=["global_admin"], groups=[]
    )


@pytest.fixture
def authenticated_client(client, mock_user):
    """
    Create a test client with authentication mocked.

    Use this for integration tests that need to access protected endpoints.
    """
    from meho_app.api.auth import get_current_user

    # Override the auth dependency
    from meho_app.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: mock_user

    from fastapi.testclient import TestClient

    return TestClient(app)


# ============================================================================
# OpenAPI Service Fixtures
# ============================================================================


@pytest.fixture
async def openapi_repository(db_session):
    """Create OpenAPIRepository for testing"""
    from meho_app.modules.connectors.rest.repository import (
        OpenAPISpecRepository as OpenAPIRepository,
    )

    return OpenAPIRepository(db_session)


@pytest.fixture
def mock_openapi_service():
    """Mock OpenAPI service for testing"""
    mock = AsyncMock()

    # Default mock responses
    mock.register_connector.return_value = {
        "id": "test-connector-id",
        "name": "Test Connector",
        "base_url": "https://api.example.com",
        "auth_type": "BASIC",
    }

    mock.list_connectors.return_value = []
    mock.get_connector.return_value = None
    mock.update_connector.return_value = None
    mock.delete_connector.return_value = None

    return mock


# Dead fixtures removed during Phase 22 architecture simplification:
# - workflow_repository (referenced dead agents.repository.WorkflowRepository)
# - mock_planner_agent (referenced dead agents.schemas.Plan/PlanStep)
# - mock_executor_agent (referenced dead agents.schemas.ExecutionResult/StepResult)
# None of these fixtures had any consumers in the test suite.
