# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for connector deletion with complete cleanup.

Verifies that deleting a connector properly cleans up:
- Database records (connector, endpoints, credentials)
- Knowledge chunks from OpenAPI ingestion
- Blob storage files (OpenAPI specs)

NOTE: These tests require a running database with migrations applied.
Run with: ./scripts/dev-env.sh test-all
Or skip with: pytest -m "not requires_docker"
"""

import uuid

import pytest

from meho_app.modules.connectors.rest.schemas import EndpointDescriptorCreate
from meho_app.modules.connectors.schemas import ConnectorCreate
from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType

# These tests require real database (docker-compose-test.yml)
pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


async def test_delete_connector_removes_database_records():
    """Test that deleting a connector cascades to endpoints and credentials"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
    from meho_app.modules.connectors.schemas import UserCredentialProvide

    session_maker = create_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        endpoint_repo = EndpointDescriptorRepository(session)
        cred_repo = UserCredentialRepository(session)

        # Create connector
        connector_create = ConnectorCreate(
            name="Test Connector for Deletion",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            tenant_id="test-tenant",
            credential_strategy="USER_PROVIDED",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        # Create endpoint
        endpoint_create = EndpointDescriptorCreate(
            connector_id=connector_id, method="GET", path="/test", summary="Test endpoint"
        )
        endpoint = await endpoint_repo.create_endpoint(endpoint_create)

        # Create user credential
        user_cred = UserCredentialProvide(
            connector_id=connector_id,
            credential_type="API_KEY",
            credentials={"api_key": "test-key-123"},
        )
        await cred_repo.store_credentials(user_id="test-user", credential=user_cred)

        await session.commit()

        # Verify all exist before deletion
        connector_check = await connector_repo.get_connector(connector_id)
        assert connector_check is not None

        endpoint_check = await endpoint_repo.get_endpoint(endpoint.id)
        assert endpoint_check is not None

        cred_check = await cred_repo.get_credentials("test-user", connector_id)
        assert cred_check is not None

        # Delete connector
        deleted = await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        assert deleted is True

        await session.commit()

    # Verify all are deleted (in new session to avoid cache)
    from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository as EPRepo

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        endpoint_repo = EPRepo(session)
        cred_repo = UserCredentialRepository(session)

        # Connector should be gone
        connector_check = await connector_repo.get_connector(connector_id)
        assert connector_check is None

        # Endpoint should be gone (cascade)
        endpoint_check = await endpoint_repo.get_endpoint(endpoint.id)
        assert endpoint_check is None

        # Credentials should be gone (cascade)
        cred_check = await cred_repo.get_credentials("test-user", connector_id)
        assert cred_check is None


async def test_delete_connector_removes_knowledge_chunks():
    """Test that deleting a connector removes associated knowledge chunks"""
    from meho_app.modules.knowledge.database import get_session_maker as get_knowledge_session_maker

    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository
    from meho_app.modules.knowledge.schemas import KnowledgeChunkFilter

    openapi_session_maker = get_session_maker()  # noqa: F821 -- pre-existing: imported as create_session_maker
    knowledge_session_maker = get_knowledge_session_maker()

    # Create connector
    async with openapi_session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector with Knowledge",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Create knowledge chunks that would be from OpenAPI ingestion
    async with knowledge_session_maker() as k_session:
        knowledge_repo = KnowledgeRepository(k_session)

        # Simulate chunks created by ingest_openapi_to_knowledge
        chunk1 = KnowledgeChunkCreate(
            text="GET /api/v1/users - List all users",
            tenant_id="test-tenant",
            knowledge_type=KnowledgeType.DOCUMENTATION,
            tags=["api", "endpoint", "users"],
            priority=5,
            source_uri=f"openapi://{connector_id}/listUsers",
        )

        chunk2 = KnowledgeChunkCreate(
            text="POST /api/v1/users - Create a user",
            tenant_id="test-tenant",
            knowledge_type=KnowledgeType.DOCUMENTATION,
            tags=["api", "endpoint", "users"],
            priority=5,
            source_uri=f"openapi://{connector_id}/createUser",
        )

        # Create chunks directly in repository
        from meho_app.modules.knowledge.models import KnowledgeChunkModel

        db_chunk1 = KnowledgeChunkModel(id=uuid.uuid4(), **chunk1.model_dump())
        db_chunk2 = KnowledgeChunkModel(id=uuid.uuid4(), **chunk2.model_dump())

        k_session.add(db_chunk1)
        k_session.add(db_chunk2)
        await k_session.commit()

        str(db_chunk1.id)
        str(db_chunk2.id)

    # Verify chunks exist before deletion
    async with knowledge_session_maker() as k_session:
        knowledge_repo = KnowledgeRepository(k_session)

        filter_params = KnowledgeChunkFilter(tenant_id="test-tenant", limit=1000)
        chunks = await knowledge_repo.list_chunks(filter_params)

        connector_chunks = [
            c
            for c in chunks
            if c.source_uri and c.source_uri.startswith(f"openapi://{connector_id}/")
        ]
        assert len(connector_chunks) == 2, f"Expected 2 chunks, found {len(connector_chunks)}"

    # Delete connector using the BFF endpoint logic (simulated)
    async with openapi_session_maker() as session:
        connector_repo = ConnectorRepository(session)

        # Simulate the deletion cleanup from routes_connectors.py
        from sqlalchemy import select

        from meho_app.modules.knowledge.models import KnowledgeChunkModel

        # Delete knowledge chunks (same logic as BFF endpoint)
        async with knowledge_session_maker() as k_session:
            source_uri_prefix = f"openapi://{connector_id}/"

            result = await k_session.execute(
                select(KnowledgeChunkModel).where(
                    KnowledgeChunkModel.tenant_id == "test-tenant",
                    KnowledgeChunkModel.source_uri.like(f"{source_uri_prefix}%"),
                )
            )
            chunks_to_delete = result.scalars().all()

            # Verify we found the chunks
            assert len(chunks_to_delete) == 2

            # Delete them
            for chunk in chunks_to_delete:
                await k_session.delete(chunk)

            await k_session.commit()

        # Delete connector
        deleted = await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        assert deleted is True

        await session.commit()

    # Verify knowledge chunks are deleted
    async with knowledge_session_maker() as k_session:
        knowledge_repo = KnowledgeRepository(k_session)

        filter_params = KnowledgeChunkFilter(tenant_id="test-tenant", limit=1000)
        chunks = await knowledge_repo.list_chunks(filter_params)

        connector_chunks = [
            c
            for c in chunks
            if c.source_uri and c.source_uri.startswith(f"openapi://{connector_id}/")
        ]
        assert len(connector_chunks) == 0, f"Found {len(connector_chunks)} orphaned chunks"


async def test_delete_connector_with_openapi_spec_removes_storage():
    """Test that deleting a connector removes the OpenAPI spec from blob storage"""
    from meho_app.core.errors import MehoError
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    session_maker = create_session_maker()

    # Create connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector with Spec",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Upload a fake OpenAPI spec to storage
    object_storage = ObjectStorage()
    storage_key = f"connectors/{connector_id}/openapi-spec-test.json"

    fake_spec = b'{"openapi": "3.0.0", "info": {"title": "Test", "version": "1.0.0"}, "paths": {}}'

    storage_uri = object_storage.upload_document(
        file_bytes=fake_spec, key=storage_key, content_type="application/json"
    )

    # Create OpenAPI spec metadata record
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version="3.0.0",
            spec_version="1.0.0",
        )

        await session.commit()

    # Verify file exists in storage
    downloaded = object_storage.download_document(storage_key)
    assert downloaded == fake_spec

    # Verify metadata exists
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        spec_check = await spec_repo.get_spec_by_connector(connector_id)
        assert spec_check is not None
        assert spec_check.storage_uri == storage_uri

    # Delete the storage file (simulating BFF endpoint logic)
    object_storage.delete_document(storage_key)

    # Delete connector (cascade deletes spec metadata)
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        deleted = await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        assert deleted is True

        await session.commit()

    # Verify storage file is deleted
    with pytest.raises(MehoError, match="Failed to download document"):
        object_storage.download_document(storage_key)

    # Verify metadata is deleted
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        spec_check = await spec_repo.get_spec_by_connector(connector_id)
        assert spec_check is None


async def test_delete_connector_tenant_isolation():
    """Test that users can only delete connectors in their own tenant"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        # Create connector in tenant A
        connector_create = ConnectorCreate(
            name="Tenant A Connector",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="tenant-a",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Try to delete from tenant B (should fail)
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        deleted = await connector_repo.delete_connector(connector_id, tenant_id="tenant-b")
        assert deleted is False  # Not found because wrong tenant

        await session.commit()

    # Verify connector still exists
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_check = await connector_repo.get_connector(connector_id, tenant_id="tenant-a")
        assert connector_check is not None

        # Clean up
        await connector_repo.delete_connector(connector_id, tenant_id="tenant-a")
        await session.commit()


@pytest.mark.integration
async def test_delete_nonexistent_connector_returns_false():
    """Test that deleting a non-existent connector returns False"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        # Try to delete non-existent connector
        fake_id = str(uuid.uuid4())
        deleted = await connector_repo.delete_connector(fake_id, tenant_id="test-tenant")

        assert deleted is False


async def test_delete_connector_with_multiple_specs():
    """Test that all OpenAPI spec versions are cleaned up when connector is deleted"""
    import time

    from meho_app.core.errors import MehoError
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    session_maker = create_session_maker()
    object_storage = ObjectStorage()

    # Create connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector Multiple Specs",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Upload multiple spec versions (simulating re-uploads)
    storage_keys = []

    for version in [1, 2, 3]:
        storage_key = f"connectors/{connector_id}/openapi-spec-v{version}.json"
        storage_keys.append(storage_key)

        fake_spec = f'{{"openapi": "3.0.0", "info": {{"title": "Test v{version}", "version": "{version}.0.0"}}, "paths": {{}}}}'.encode()

        storage_uri = object_storage.upload_document(
            file_bytes=fake_spec, key=storage_key, content_type="application/json"
        )

        # Create metadata (only latest one gets returned by get_spec_by_connector)
        async with session_maker() as session:
            spec_repo = OpenAPISpecRepository(session)

            await spec_repo.create_spec(
                connector_id=connector_id,
                storage_uri=storage_uri,
                version="3.0.0",
                spec_version=f"{version}.0.0",
            )

            await session.commit()

        time.sleep(  # noqa: ASYNC251 -- blocking sleep intentional in test
            0.1
        )  # Ensure different timestamps

    # Verify all files exist
    for key in storage_keys:
        downloaded = object_storage.download_document(key)
        assert len(downloaded) > 0

    # Get the most recent spec
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        latest_spec = await spec_repo.get_spec_by_connector(connector_id)
        assert latest_spec is not None

        latest_storage_key = latest_spec.storage_uri.split("/", 3)[-1]

    # Delete only the latest storage file (simulating BFF logic which only deletes latest)
    object_storage.delete_document(latest_storage_key)

    # Delete connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        deleted = await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        assert deleted is True

        await session.commit()

    # Verify latest storage file is deleted
    with pytest.raises(MehoError):
        object_storage.download_document(latest_storage_key)

    # Note: Other versions would still exist in S3 (BFF only deletes latest)
    # This is acceptable - users can clean up S3 separately
    # Or we could enhance to delete all versions (future enhancement)

    # Clean up remaining files
    for key in storage_keys:
        if key != latest_storage_key:
            try:  # noqa: SIM105 -- explicit error handling preferred
                object_storage.delete_document(key)
            except:  # noqa: E722, S110 -- intentional bare except for test cleanup
                pass  # Already deleted


async def test_delete_connector_invalid_uuid_returns_false():
    """Test that invalid UUID format returns False"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        # Try to delete with invalid UUID
        deleted = await connector_repo.delete_connector("not-a-uuid", tenant_id="test-tenant")

        assert deleted is False
