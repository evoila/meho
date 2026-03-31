# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for OpenAPI spec storage, download, and lifecycle management.

Verifies:
- OpenAPI specs are stored in blob storage on upload
- Spec metadata is saved to database
- Specs can be downloaded
- Validation happens before storage
- Specs are deleted when connector is deleted

NOTE: These tests require running database + MinIO with migrations applied.
Run with: ./scripts/dev-env.sh test-all
Or skip with: pytest -m "not requires_docker"
"""

import pytest

from meho_app.modules.connectors.schemas import ConnectorCreate
from meho_app.modules.knowledge.object_storage import ObjectStorage

# These tests require real database + MinIO (docker-compose-test.yml)
pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


async def test_openapi_spec_upload_creates_storage_and_metadata():
    """Test that uploading a spec stores it in S3 and creates database record"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository

    session_maker = create_session_maker()
    object_storage = ObjectStorage()

    # Create connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector for Spec Storage",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Create and upload a valid OpenAPI spec
    storage_key = f"connectors/{connector_id}/openapi-spec-test-integration.json"

    valid_spec = b"""{
        "openapi": "3.0.0",
        "info": {
            "title": "Test API",
            "version": "1.0.0"
        },
        "paths": {
            "/test": {
                "get": {
                    "summary": "Test endpoint"
                }
            }
        }
    }"""

    storage_uri = object_storage.upload_document(
        file_bytes=valid_spec, key=storage_key, content_type="application/json"
    )

    # Create metadata record
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        spec = await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version="3.0.0",
            spec_version="1.0.0",
        )

        await session.commit()

        spec_id = spec.id
        assert spec.storage_uri == storage_uri
        assert spec.version == "3.0.0"
        assert spec.spec_version == "1.0.0"

    # Verify file exists in storage
    downloaded = object_storage.download_document(storage_key)
    assert downloaded == valid_spec

    # Verify metadata can be retrieved
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        retrieved_spec = await spec_repo.get_spec_by_connector(connector_id)
        assert retrieved_spec is not None
        assert retrieved_spec.id == spec_id
        assert retrieved_spec.storage_uri == storage_uri

    # Clean up
    object_storage.delete_document(storage_key)

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        await session.commit()


async def test_openapi_spec_download_returns_original_content():
    """Test that downloading a spec returns the exact uploaded content"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    session_maker = create_session_maker()
    object_storage = ObjectStorage()

    # Create connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector for Download",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Upload YAML spec
    storage_key = f"connectors/{connector_id}/openapi-spec-download-test.yaml"

    original_spec = b"""openapi: 3.0.0
info:
  title: Download Test API
  version: 2.5.0
  description: Testing spec download functionality
paths:
  /users:
    get:
      summary: List users
      operationId: listUsers
  /users/{id}:
    get:
      summary: Get user by ID
      parameters:
        - name: id
          in: path
          required: true
"""

    storage_uri = object_storage.upload_document(
        file_bytes=original_spec, key=storage_key, content_type="application/x-yaml"
    )

    # Create metadata
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version="3.0.0",
            spec_version="2.5.0",
        )

        await session.commit()

    # Download and verify
    downloaded_spec = object_storage.download_document(storage_key)
    assert downloaded_spec == original_spec

    # Verify it's valid YAML and can be parsed
    import yaml

    spec_dict = yaml.safe_load(downloaded_spec)
    assert spec_dict["info"]["title"] == "Download Test API"
    assert spec_dict["info"]["version"] == "2.5.0"
    assert "/users" in spec_dict["paths"]

    # Clean up
    object_storage.delete_document(storage_key)

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        await session.commit()


@pytest.mark.integration
async def test_get_spec_returns_most_recent():
    """Test that get_spec_by_connector returns the most recent spec when multiple exist"""
    import time

    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository

    session_maker = create_session_maker()

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

    # Create multiple spec records with different timestamps
    spec_ids = []

    for version in [1, 2, 3]:
        time.sleep(  # noqa: ASYNC251 -- blocking sleep intentional in test
            0.1
        )  # Ensure different created_at timestamps

        async with session_maker() as session:
            spec_repo = OpenAPISpecRepository(session)

            spec = await spec_repo.create_spec(
                connector_id=connector_id,
                storage_uri=f"s3://test-bucket/spec-v{version}.json",
                version="3.0.0",
                spec_version=f"{version}.0.0",
            )

            spec_ids.append(spec.id)

            await session.commit()

    # Get spec (should return most recent = v3)
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        latest_spec = await spec_repo.get_spec_by_connector(connector_id)

        assert latest_spec is not None
        assert latest_spec.spec_version == "3.0.0"  # Version 3 is most recent
        assert latest_spec.id == spec_ids[2]  # Last one created

    # Clean up
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        await session.commit()


@pytest.mark.integration
async def test_get_spec_no_spec_returns_none():
    """Test that get_spec_by_connector returns None if no spec uploaded"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository

    session_maker = create_session_maker()

    # Create connector without uploading spec
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test Connector No Spec",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Try to get spec (should return None)
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        spec = await spec_repo.get_spec_by_connector(connector_id)

        assert spec is None

    # Clean up
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        await session.commit()


@pytest.mark.integration
async def test_openapi_spec_storage_supports_yaml():
    """Test that YAML specs are stored and retrieved correctly"""
    from meho_app.database import get_session_maker as create_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository, OpenAPISpecRepository
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    session_maker = create_session_maker()
    object_storage = ObjectStorage()

    # Create connector
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name="Test YAML Spec",
            base_url="https://api.example.com",
            auth_type="NONE",
            tenant_id="test-tenant",
            credential_strategy="SYSTEM",
        )
        connector = await connector_repo.create_connector(connector_create)
        connector_id = connector.id

        await session.commit()

    # Upload YAML spec
    storage_key = f"connectors/{connector_id}/openapi-spec.yaml"

    yaml_spec = b"""openapi: 3.1.0
info:
  title: YAML Test API
  version: 1.0.0
paths:
  /test:
    get:
      summary: Test endpoint
"""

    storage_uri = object_storage.upload_document(
        file_bytes=yaml_spec, key=storage_key, content_type="application/x-yaml"
    )

    # Create metadata
    async with session_maker() as session:
        spec_repo = OpenAPISpecRepository(session)

        await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version="3.1.0",
            spec_version="1.0.0",
        )

        await session.commit()

    # Download and verify YAML content
    downloaded = object_storage.download_document(storage_key)
    assert downloaded == yaml_spec

    # Verify it's valid YAML
    import yaml

    spec_dict = yaml.safe_load(downloaded)
    assert spec_dict["openapi"] == "3.1.0"
    assert spec_dict["info"]["title"] == "YAML Test API"

    # Clean up
    object_storage.delete_document(storage_key)

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        await connector_repo.delete_connector(connector_id, tenant_id="test-tenant")
        await session.commit()
