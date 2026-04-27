# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for connector export/import API endpoints.

Tests the POST /api/connectors/export and POST /api/connectors/import endpoints.

NOTE: These tests require a running database with migrations applied.
Run with: ./scripts/dev-env.sh test-all
Or skip with: pytest -m "not requires_docker"
"""

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from meho_app.core.auth_context import UserContext
from meho_app.modules.connectors.schemas import ConnectorCreate

# These tests require real database (docker-compose-test.yml)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def test_user() -> UserContext:
    """Create a test user context."""
    return UserContext(
        user_id="test-user-export",
        tenant_id="test-tenant-export",
        roles=["admin"],
        groups=[],
    )


@pytest.fixture
def test_client(test_user: UserContext) -> TestClient:
    """Create a test client with mocked auth."""
    from meho_app.api.auth import get_current_user
    from meho_app.main import app

    app.dependency_overrides[get_current_user] = lambda: test_user

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def test_connector(test_user: UserContext) -> str:
    """Create a test connector and return its ID."""
    from meho_app.database import get_session_maker

    session_maker = get_session_maker()

    async with session_maker() as session:
        from meho_app.modules.connectors.repositories import ConnectorRepository

        repo = ConnectorRepository(session)

        connector_create = ConnectorCreate(
            name=f"Test Export Connector {uuid.uuid4().hex[:8]}",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            tenant_id=test_user.tenant_id,
            credential_strategy="USER_PROVIDED",
        )
        connector = await repo.create_connector(connector_create)
        await session.commit()

        connector_id = connector.id

    yield connector_id

    # Cleanup
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        await repo.delete_connector(connector_id, tenant_id=test_user.tenant_id)
        await session.commit()


# =============================================================================
# Export Endpoint Tests
# =============================================================================


def test_export_connectors_json_format(
    test_client: TestClient,
    test_connector: str,
    test_user: UserContext,
) -> None:
    """Test exporting connectors to JSON format."""
    response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [test_connector],
            "password": "secure-password-123",
            "format": "json",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "attachment" in response.headers.get("content-disposition", "")
    assert ".json" in response.headers.get("content-disposition", "")

    # Verify valid JSON
    data = response.json()
    assert "meho_export" in data
    assert "connectors" in data
    assert len(data["connectors"]) == 1


def test_export_connectors_yaml_format(
    test_client: TestClient,
    test_connector: str,
) -> None:
    """Test exporting connectors to YAML format."""
    import yaml

    response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [test_connector],
            "password": "secure-password-123",
            "format": "yaml",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-yaml"
    assert ".yaml" in response.headers.get("content-disposition", "")

    # Verify valid YAML
    data = yaml.safe_load(response.content)
    assert "meho_export" in data
    assert "connectors" in data


def test_export_all_connectors(
    test_client: TestClient,
    test_connector: str,
) -> None:
    """Test exporting all connectors (empty connector_ids)."""
    response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [],  # Empty = export all
            "password": "secure-password-123",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["connectors"]) >= 1  # At least our test connector


def test_export_short_password_returns_400(
    test_client: TestClient,
    test_connector: str,
) -> None:
    """Test that short password returns 400 error."""
    response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [test_connector],
            "password": "short",  # Too short
        },
    )

    assert response.status_code == 422  # Pydantic validation error


def test_export_nonexistent_connector_returns_400(
    test_client: TestClient,
) -> None:
    """Test that exporting non-existent connector returns 400."""
    fake_id = str(uuid.uuid4())

    response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [fake_id],
            "password": "secure-password-123",
        },
    )

    assert response.status_code == 400
    assert "No connectors found" in response.json()["detail"]


# =============================================================================
# Import Endpoint Tests
# =============================================================================


async def test_import_connectors_basic(
    test_client: TestClient,
    test_user: UserContext,
) -> None:
    """Test importing connectors from exported file."""
    # Create export data
    export_data = {
        "meho_export": {
            "version": "1.0",
            "exported_at": "2026-01-02T10:00:00Z",
            "encrypted": True,
        },
        "connectors": [
            {
                "name": f"Imported Connector {uuid.uuid4().hex[:8]}",
                "base_url": "https://imported-api.example.com",
                "auth_type": "API_KEY",
                "auth_config": {},
                "credential_strategy": "SYSTEM",
            }
        ],
    }

    file_content = base64.b64encode(json.dumps(export_data).encode()).decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "secure-password-123",
            "conflict_strategy": "skip",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert len(result["errors"]) == 0
    assert len(result["connectors"]) == 1

    # Cleanup: delete imported connector
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        connectors = await repo.list_connectors(test_user.tenant_id)
        for c in connectors:
            if c.name in result["connectors"]:
                await repo.delete_connector(c.id, test_user.tenant_id)
        await session.commit()


async def test_import_skip_conflict_strategy(
    test_client: TestClient,
    test_connector: str,
    test_user: UserContext,
) -> None:
    """Test import with skip conflict strategy."""
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    # Get existing connector name
    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        existing = await repo.get_connector(test_connector, test_user.tenant_id)
        existing_name = existing.name

    # Try to import connector with same name
    export_data = {
        "meho_export": {
            "version": "1.0",
            "exported_at": "2026-01-02T10:00:00Z",
            "encrypted": True,
        },
        "connectors": [
            {
                "name": existing_name,  # Same name as existing
                "base_url": "https://different-api.example.com",
                "auth_type": "NONE",
                "auth_config": {},
                "credential_strategy": "SYSTEM",
            }
        ],
    }

    file_content = base64.b64encode(json.dumps(export_data).encode()).decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "secure-password-123",
            "conflict_strategy": "skip",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["imported"] == 0
    assert result["skipped"] == 1


async def test_import_rename_conflict_strategy(
    test_client: TestClient,
    test_connector: str,
    test_user: UserContext,
) -> None:
    """Test import with rename conflict strategy."""
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    # Get existing connector name
    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        existing = await repo.get_connector(test_connector, test_user.tenant_id)
        existing_name = existing.name

    # Try to import connector with same name using rename strategy
    export_data = {
        "meho_export": {
            "version": "1.0",
            "exported_at": "2026-01-02T10:00:00Z",
            "encrypted": True,
        },
        "connectors": [
            {
                "name": existing_name,  # Same name as existing
                "base_url": "https://different-api.example.com",
                "auth_type": "NONE",
                "auth_config": {},
                "credential_strategy": "SYSTEM",
            }
        ],
    }

    file_content = base64.b64encode(json.dumps(export_data).encode()).decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "secure-password-123",
            "conflict_strategy": "rename",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["imported"] == 1
    assert result["skipped"] == 0
    # Should have renamed to "Name (2)"
    assert "(2)" in result["connectors"][0]

    # Cleanup: delete imported connector
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        connectors = await repo.list_connectors(test_user.tenant_id)
        for c in connectors:
            if c.name in result["connectors"]:
                await repo.delete_connector(c.id, test_user.tenant_id)
        await session.commit()


def test_import_invalid_base64_returns_400(
    test_client: TestClient,
) -> None:
    """Test that invalid base64 content returns 400."""
    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": "not-valid-base64!!!",
            "password": "secure-password-123",
        },
    )

    assert response.status_code == 400
    assert "Invalid file content encoding" in response.json()["detail"]


def test_import_invalid_file_format_returns_400(
    test_client: TestClient,
) -> None:
    """Test that invalid file format returns 400."""
    # Valid base64, but not valid export file
    file_content = base64.b64encode(b"not json or yaml!!!").decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "secure-password-123",
        },
    )

    assert response.status_code == 400
    assert "Failed to parse" in response.json()["detail"]


def test_import_missing_metadata_returns_400(
    test_client: TestClient,
) -> None:
    """Test that missing meho_export metadata returns 400."""
    invalid_export = {"connectors": []}
    file_content = base64.b64encode(json.dumps(invalid_export).encode()).decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "secure-password-123",
        },
    )

    assert response.status_code == 400
    assert "meho_export" in response.json()["detail"]


def test_import_short_password_returns_422(
    test_client: TestClient,
) -> None:
    """Test that short password returns 422 validation error."""
    file_content = base64.b64encode(b"{}").decode()

    response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "short",  # Too short
        },
    )

    assert response.status_code == 422  # Pydantic validation error


# =============================================================================
# Roundtrip Tests
# =============================================================================


async def test_export_import_roundtrip(
    test_client: TestClient,
    test_connector: str,
    test_user: UserContext,
) -> None:
    """Test that exported connectors can be imported back."""
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    # Export the test connector
    export_response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [test_connector],
            "password": "roundtrip-password!",
            "format": "json",
        },
    )

    assert export_response.status_code == 200
    exported_content = export_response.content.decode("utf-8")

    # Verify export contains our connector
    export_data = json.loads(exported_content)
    assert len(export_data["connectors"]) == 1
    original_name = export_data["connectors"][0]["name"]

    # Import into same tenant (will trigger rename since name exists)
    file_content = base64.b64encode(exported_content.encode()).decode()

    import_response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "roundtrip-password!",
            "conflict_strategy": "rename",
        },
    )

    assert import_response.status_code == 200
    result = import_response.json()
    assert result["imported"] == 1
    assert len(result["errors"]) == 0

    # Verify the imported connector exists
    imported_name = result["connectors"][0]
    assert f"{original_name} (2)" == imported_name

    # Cleanup
    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        connectors = await repo.list_connectors(test_user.tenant_id)
        for c in connectors:
            if c.name == imported_name:
                await repo.delete_connector(c.id, test_user.tenant_id)
        await session.commit()


async def test_export_import_with_credentials_roundtrip(
    test_client: TestClient,
    test_connector: str,
    test_user: UserContext,
) -> None:
    """Test that credentials are properly encrypted and decrypted."""
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.repositories import (
        ConnectorRepository,
        CredentialRepository,
    )
    from meho_app.modules.connectors.schemas import UserCredentialProvide

    session_maker = get_session_maker()

    # Store credentials for the test connector
    async with session_maker() as session:
        cred_repo = CredentialRepository(session)
        credential = UserCredentialProvide(
            connector_id=test_connector,
            credential_type="API_KEY",
            credentials={"api_key": "super-secret-key-12345"},
        )
        await cred_repo.store_credentials(test_user.user_id, credential)
        await session.commit()

    # Export with credentials
    export_response = test_client.post(
        "/api/connectors/export",
        json={
            "connector_ids": [test_connector],
            "password": "credential-test-pwd",
            "format": "json",
        },
    )

    assert export_response.status_code == 200
    export_data = json.loads(export_response.content.decode("utf-8"))

    # Verify credentials are encrypted (not plaintext)
    exported_connector = export_data["connectors"][0]
    assert exported_connector.get("credentials_encrypted") is not None
    assert "super-secret-key" not in exported_connector.get("credentials_encrypted", "")

    # Import with correct password (using rename to avoid conflict)
    file_content = base64.b64encode(export_response.content).decode()

    import_response = test_client.post(
        "/api/connectors/import",
        json={
            "file_content": file_content,
            "password": "credential-test-pwd",
            "conflict_strategy": "rename",
        },
    )

    assert import_response.status_code == 200
    result = import_response.json()
    assert result["imported"] == 1
    assert len(result["errors"]) == 0

    # Verify credentials were stored for the new connector
    imported_name = result["connectors"][0]
    async with session_maker() as session:
        conn_repo = ConnectorRepository(session)
        cred_repo = CredentialRepository(session)

        connectors = await conn_repo.list_connectors(test_user.tenant_id)
        imported_connector = next((c for c in connectors if c.name == imported_name), None)
        assert imported_connector is not None

        # Check credentials were stored
        creds = await cred_repo.get_credentials(test_user.user_id, imported_connector.id)
        assert creds is not None
        assert creds.get("api_key") == "super-secret-key-12345"

        # Cleanup
        await conn_repo.delete_connector(imported_connector.id, test_user.tenant_id)
        await session.commit()
