# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration test fixtures.

Provides common fixtures for integration tests including:
- Database session (PostgreSQL with pgvector)
- Knowledge repository and embeddings
- BM25 index manager
- Tenant fixtures
- User context fixtures
"""

import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text


# ============================================================================
# Database Fixtures
# ============================================================================


@pytest.fixture
async def db_session() -> AsyncGenerator:
    """
    Create test database session with tables created/dropped per test.

    Function-scoped fixture for integration tests that need a real database.
    Requires PostgreSQL with pgvector extension to be running.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    # Import all Base classes
    try:
        from meho_app.modules.knowledge.models import Base as KnowledgeBase
    except ImportError:
        KnowledgeBase = None

    try:
        from meho_app.modules.connectors.models import Base as ConnectorsBase
    except ImportError:
        ConnectorsBase = None

    try:
        from meho_app.modules.agents.models import Base as AgentBase
    except ImportError:
        AgentBase = None

    database_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
    )

    engine = create_async_engine(
        database_url,
        echo=False,
        poolclass=NullPool,
    )

    # Create pgvector extension and all tables
    async with engine.begin() as conn:
        # Enable pgvector extension (required for knowledge_chunk table)
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        if KnowledgeBase:
            await conn.run_sync(KnowledgeBase.metadata.create_all)
        if ConnectorsBase:
            await conn.run_sync(ConnectorsBase.metadata.create_all)
        if AgentBase:
            await conn.run_sync(AgentBase.metadata.create_all)

    # Create session
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        yield session

    # Drop all tables after test
    async with engine.begin() as conn:
        if KnowledgeBase:
            await conn.run_sync(KnowledgeBase.metadata.drop_all)
        if ConnectorsBase:
            await conn.run_sync(ConnectorsBase.metadata.drop_all)
        if AgentBase:
            await conn.run_sync(AgentBase.metadata.drop_all)

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
        async def embed_query(self, text: str):
            """Generate deterministic 1536-dimensional embedding"""
            hash_val = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)  # noqa: S324 -- non-security hash context in tests
            # Return a 1536-dimensional vector (OpenAI embedding size)
            vector = [(hash_val + i) % 100 / 100.0 for i in range(1536)]
            return vector

        async def embed_text(self, text: str):
            """Alias for embed_query"""
            return await self.embed_query(text)

        async def embed_documents(self, texts: list):
            """Generate embeddings for multiple documents"""
            return [await self.embed_query(text) for text in texts]

    return MockEmbeddings()


@pytest.fixture
def temp_index_dir():
    """Create temporary directory for BM25 indexes"""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    # Cleanup after test
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def bm25_index_manager(temp_index_dir):
    """
    DEPRECATED: BM25IndexManager replaced with PostgreSQL FTS.

    This fixture is kept for backward compatibility with existing tests.
    PostgreSQL FTS doesn't require manual index management - indexes
    are automatically maintained by the database.

    Tests using this fixture should be updated to remove index build calls.
    """
    # Return None - tests should check for None and skip BM25-specific operations
    return None


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
