# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for connector export/import service.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from meho_app.modules.connectors.export_encryption import (
    PasswordTooShortError,
)
from meho_app.modules.connectors.export_service import (
    ConnectorExportData,
    ConnectorExportService,
    ExportError,
    ExportFile,
    ExportMetadata,
    ImportError,
)
from meho_app.modules.connectors.schemas import Connector, ConnectorCreate

# =============================================================================
# Fixtures
# =============================================================================


def make_connector(
    name: str = "Test Connector",
    connector_type: str = "rest",
    connector_id: str | None = None,
) -> Connector:
    """Create a test connector."""
    return Connector(
        id=connector_id or str(uuid4()),
        tenant_id="tenant-1",
        name=name,
        description="Test description",
        base_url="https://api.example.com",
        connector_type=connector_type,
        auth_type="API_KEY",
        auth_config={"header": "X-API-Key"},
        credential_strategy="USER_PROVIDED",
        protocol_config={"timeout": 30},
        login_url=None,
        login_method=None,
        login_config=None,
        allowed_methods=["GET", "POST"],
        blocked_methods=[],
        default_safety_level="safe",
        related_connector_ids=[],
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def make_export_file(connectors: list[ConnectorExportData]) -> ExportFile:
    """Create a test export file."""
    return ExportFile(
        meho_export=ExportMetadata(
            version="1.0",
            exported_at=datetime.now(UTC).isoformat(),
            encrypted=True,
        ),
        connectors=connectors,
    )


@pytest.fixture
def mock_session() -> AsyncMock:
    """Create mock database session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def export_service(mock_session: AsyncMock) -> ConnectorExportService:
    """Create export service with mocked dependencies."""
    service = ConnectorExportService(mock_session)
    service.connector_repo = AsyncMock()
    service.credential_repo = AsyncMock()
    return service


# =============================================================================
# Export Tests
# =============================================================================


@pytest.mark.unit
async def test_export_single_connector_json(
    export_service: ConnectorExportService,
) -> None:
    """Test exporting a single connector to JSON format."""
    connector = make_connector("My API")
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[connector])
    export_service.credential_repo.get_credentials = AsyncMock(return_value=None)

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
        connector_ids=[connector.id],
        format="json",
    )

    # Should be valid JSON
    data = json.loads(result)
    assert "meho_export" in data
    assert data["meho_export"]["version"] == "1.0"
    assert data["meho_export"]["encrypted"] is True
    assert "connectors" in data
    assert len(data["connectors"]) == 1
    assert data["connectors"][0]["name"] == "My API"


@pytest.mark.unit
async def test_export_single_connector_yaml(
    export_service: ConnectorExportService,
) -> None:
    """Test exporting a single connector to YAML format."""
    connector = make_connector("My API")
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[connector])
    export_service.credential_repo.get_credentials = AsyncMock(return_value=None)

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
        format="yaml",
    )

    # Should be valid YAML
    data = yaml.safe_load(result)
    assert data["meho_export"]["version"] == "1.0"
    assert len(data["connectors"]) == 1
    assert data["connectors"][0]["name"] == "My API"


@pytest.mark.unit
async def test_export_multiple_connectors(
    export_service: ConnectorExportService,
) -> None:
    """Test exporting multiple connectors."""
    connectors = [
        make_connector("API 1"),
        make_connector("API 2"),
        make_connector("API 3"),
    ]
    export_service.connector_repo.list_connectors = AsyncMock(return_value=connectors)
    export_service.credential_repo.get_credentials = AsyncMock(return_value=None)

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
    )

    data = json.loads(result)
    assert len(data["connectors"]) == 3
    names = [c["name"] for c in data["connectors"]]
    assert "API 1" in names
    assert "API 2" in names
    assert "API 3" in names


@pytest.mark.unit
async def test_export_selected_connectors(
    export_service: ConnectorExportService,
) -> None:
    """Test exporting only selected connectors."""
    c1 = make_connector("API 1", connector_id="id-1")
    c2 = make_connector("API 2", connector_id="id-2")
    c3 = make_connector("API 3", connector_id="id-3")
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[c1, c2, c3])
    export_service.credential_repo.get_credentials = AsyncMock(return_value=None)

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
        connector_ids=["id-1", "id-3"],
    )

    data = json.loads(result)
    assert len(data["connectors"]) == 2
    names = [c["name"] for c in data["connectors"]]
    assert "API 1" in names
    assert "API 3" in names
    assert "API 2" not in names


@pytest.mark.unit
async def test_export_with_credentials(
    export_service: ConnectorExportService,
) -> None:
    """Test exporting connector with encrypted credentials."""
    connector = make_connector("My API")
    credentials = {"username": "admin", "password": "secret123"}

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[connector])
    export_service.credential_repo.get_credentials = AsyncMock(return_value=credentials)

    # Mock _get_credential_type
    export_service._get_credential_type = AsyncMock(return_value="PASSWORD")

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
    )

    data = json.loads(result)
    exported = data["connectors"][0]

    # Should have encrypted credentials
    assert exported["credentials_encrypted"] is not None
    assert "admin" not in exported["credentials_encrypted"]
    assert "secret123" not in exported["credentials_encrypted"]
    assert exported["credential_type"] == "PASSWORD"


@pytest.mark.unit
async def test_export_no_connectors_raises_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that export raises error when no connectors found."""
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])

    with pytest.raises(ExportError) as exc_info:
        await export_service.export_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            password="secure-password-123",
        )

    assert "No connectors found" in str(exc_info.value)


@pytest.mark.unit
async def test_export_short_password_raises_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that export raises error for short password."""
    connector = make_connector("My API")
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[connector])
    export_service.credential_repo.get_credentials = AsyncMock(return_value={"key": "value"})
    export_service._get_credential_type = AsyncMock(return_value="API_KEY")

    with pytest.raises(PasswordTooShortError):
        await export_service.export_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            password="short",
        )


@pytest.mark.unit
async def test_export_preserves_connector_type(
    export_service: ConnectorExportService,
) -> None:
    """Test that export preserves typed connector types."""
    connectors = [
        make_connector("vSphere", connector_type="vmware"),
        make_connector("Proxmox", connector_type="proxmox"),
        make_connector("GCP", connector_type="gcp"),
    ]
    export_service.connector_repo.list_connectors = AsyncMock(return_value=connectors)
    export_service.credential_repo.get_credentials = AsyncMock(return_value=None)

    result = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="secure-password-123",
    )

    data = json.loads(result)
    types = {c["name"]: c["connector_type"] for c in data["connectors"]}
    assert types["vSphere"] == "vmware"
    assert types["Proxmox"] == "proxmox"
    assert types["GCP"] == "gcp"


# =============================================================================
# Import Tests
# =============================================================================


@pytest.mark.unit
async def test_import_single_connector(
    export_service: ConnectorExportService,
) -> None:
    """Test importing a single connector."""
    export_data = ConnectorExportData(
        name="Imported API",
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])
    new_connector = make_connector("Imported API")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
    )

    assert result.imported == 1
    assert result.skipped == 0
    assert len(result.errors) == 0
    assert "Imported API" in result.connectors


@pytest.mark.unit
async def test_import_from_yaml(
    export_service: ConnectorExportService,
) -> None:
    """Test importing from YAML format."""
    export_data = ConnectorExportData(
        name="YAML API",
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = yaml.dump(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])
    new_connector = make_connector("YAML API")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
    )

    assert result.imported == 1
    assert "YAML API" in result.connectors


@pytest.mark.unit
async def test_import_with_credentials(
    export_service: ConnectorExportService,
) -> None:
    """Test importing connector with encrypted credentials."""
    # Encrypt credentials
    credentials = {"username": "admin", "password": "secret"}
    encrypted = export_service.encryption.encrypt(json.dumps(credentials), "secure-password-123")

    export_data = ConnectorExportData(
        name="API with Creds",
        base_url="https://api.example.com",
        auth_type="BASIC",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        credentials_encrypted=encrypted,
        credential_type="PASSWORD",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])
    new_connector = make_connector("API with Creds")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)
    export_service.credential_repo.store_credentials = AsyncMock()

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
    )

    assert result.imported == 1
    # Verify credentials were stored
    export_service.credential_repo.store_credentials.assert_called_once()


@pytest.mark.unit
async def test_import_skip_strategy(
    export_service: ConnectorExportService,
) -> None:
    """Test import with skip conflict strategy."""
    existing = make_connector("Existing API")
    export_data = ConnectorExportData(
        name="Existing API",  # Same name
        base_url="https://new-api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[existing])

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
        conflict_strategy="skip",
    )

    assert result.imported == 0
    assert result.skipped == 1
    export_service.connector_repo.create_connector.assert_not_called()


@pytest.mark.unit
async def test_import_overwrite_strategy(
    export_service: ConnectorExportService,
) -> None:
    """Test import with overwrite conflict strategy."""
    existing = make_connector("Existing API", connector_id="existing-id")
    export_data = ConnectorExportData(
        name="Existing API",
        base_url="https://new-api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[existing])
    export_service.connector_repo.delete_connector = AsyncMock(return_value=True)
    new_connector = make_connector("Existing API", connector_id="new-id")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
        conflict_strategy="overwrite",
    )

    assert result.imported == 1
    assert result.skipped == 0
    export_service.connector_repo.delete_connector.assert_called_once_with(
        "existing-id", "tenant-1"
    )


@pytest.mark.unit
async def test_import_rename_strategy(
    export_service: ConnectorExportService,
) -> None:
    """Test import with rename conflict strategy."""
    existing = make_connector("My API")
    export_data = ConnectorExportData(
        name="My API",  # Same name
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[existing])
    new_connector = make_connector("My API (2)")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
        conflict_strategy="rename",
    )

    assert result.imported == 1
    assert "My API (2)" in result.connectors

    # Verify the name was changed in the create call
    call_args = export_service.connector_repo.create_connector.call_args
    assert call_args[0][0].name == "My API (2)"


@pytest.mark.unit
async def test_import_rename_strategy_multiple_conflicts(
    export_service: ConnectorExportService,
) -> None:
    """Test rename strategy with multiple existing conflicts."""
    existing = [
        make_connector("My API"),
        make_connector("My API (2)"),
    ]
    export_data = ConnectorExportData(
        name="My API",
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="SYSTEM",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=existing)
    new_connector = make_connector("My API (3)")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="secure-password-123",
        conflict_strategy="rename",
    )

    assert result.imported == 1
    # Should be (3) since (2) already exists
    call_args = export_service.connector_repo.create_connector.call_args
    assert call_args[0][0].name == "My API (3)"


@pytest.mark.unit
async def test_import_invalid_json_raises_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that invalid JSON raises ImportError."""
    with pytest.raises(ImportError) as exc_info:
        await export_service.import_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            file_content="not valid json or yaml!!!",
            password="secure-password-123",
        )

    assert "Failed to parse" in str(exc_info.value)


@pytest.mark.unit
async def test_import_missing_metadata_raises_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that missing meho_export metadata raises error."""
    file_content = json.dumps({"connectors": []})

    with pytest.raises(ImportError) as exc_info:
        await export_service.import_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            file_content=file_content,
            password="secure-password-123",
        )

    assert "meho_export" in str(exc_info.value)


@pytest.mark.unit
async def test_import_missing_connectors_raises_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that missing connectors array raises error."""
    file_content = json.dumps(
        {"meho_export": {"version": "1.0", "exported_at": "now", "encrypted": True}}
    )

    with pytest.raises(ImportError) as exc_info:
        await export_service.import_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            file_content=file_content,
            password="secure-password-123",
        )

    assert "connectors" in str(exc_info.value)


@pytest.mark.unit
async def test_import_wrong_password_records_error(
    export_service: ConnectorExportService,
) -> None:
    """Test that wrong password is recorded as error, not exception."""
    # Encrypt with one password
    credentials = {"key": "value"}
    encrypted = export_service.encryption.encrypt(json.dumps(credentials), "correct-password")

    export_data = ConnectorExportData(
        name="API",
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        credentials_encrypted=encrypted,
        credential_type="API_KEY",
    )
    export_file = make_export_file([export_data])
    file_content = json.dumps(export_file.model_dump(mode="json"))

    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])
    new_connector = make_connector("API")
    export_service.connector_repo.create_connector = AsyncMock(return_value=new_connector)

    # Import with wrong password
    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=file_content,
        password="wrong-password-!",
    )

    # Should record error but not raise
    assert result.imported == 0
    assert len(result.errors) == 1
    assert "decrypt" in result.errors[0].lower()


# =============================================================================
# Roundtrip Tests
# =============================================================================


@pytest.mark.unit
async def test_export_import_roundtrip(
    export_service: ConnectorExportService,
) -> None:
    """Test that export then import produces equivalent connectors."""
    original = make_connector("Roundtrip API", connector_type="proxmox")
    credentials = {"api_token_id": "user@pam!token", "api_token_secret": "secret123"}

    # Export
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[original])
    export_service.credential_repo.get_credentials = AsyncMock(return_value=credentials)
    export_service._get_credential_type = AsyncMock(return_value="API_KEY")

    exported = await export_service.export_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        password="roundtrip-password",
    )

    # Import
    export_service.connector_repo.list_connectors = AsyncMock(return_value=[])
    created_connector = make_connector("Roundtrip API", connector_type="proxmox")
    export_service.connector_repo.create_connector = AsyncMock(return_value=created_connector)
    export_service.credential_repo.store_credentials = AsyncMock()

    result = await export_service.import_connectors(
        tenant_id="tenant-1",
        user_id="user-1",
        file_content=exported,
        password="roundtrip-password",
    )

    assert result.imported == 1
    assert len(result.errors) == 0

    # Verify the created connector has correct values
    call_args = export_service.connector_repo.create_connector.call_args
    created: ConnectorCreate = call_args[0][0]
    assert created.name == original.name
    assert created.connector_type == original.connector_type
    assert created.base_url == original.base_url
    assert created.auth_type == original.auth_type


# =============================================================================
# Helper Method Tests
# =============================================================================


@pytest.mark.unit
def test_generate_unique_name(export_service: ConnectorExportService) -> None:
    """Test unique name generation."""
    existing = {
        "API": make_connector("API"),
        "API (2)": make_connector("API (2)"),
        "API (3)": make_connector("API (3)"),
    }

    result = export_service._generate_unique_name("API", existing)
    assert result == "API (4)"


@pytest.mark.unit
def test_generate_unique_name_first_conflict(
    export_service: ConnectorExportService,
) -> None:
    """Test unique name generation for first conflict."""
    existing = {"API": make_connector("API")}

    result = export_service._generate_unique_name("API", existing)
    assert result == "API (2)"


@pytest.mark.unit
def test_infer_credential_type_api_key(
    export_service: ConnectorExportService,
) -> None:
    """Test credential type inference for API key."""
    result = export_service._infer_credential_type({"api_key": "xxx"})
    assert result == "API_KEY"


@pytest.mark.unit
def test_infer_credential_type_password(
    export_service: ConnectorExportService,
) -> None:
    """Test credential type inference for password."""
    result = export_service._infer_credential_type({"username": "user", "password": "pass"})
    assert result == "PASSWORD"


@pytest.mark.unit
def test_infer_credential_type_oauth(
    export_service: ConnectorExportService,
) -> None:
    """Test credential type inference for OAuth token."""
    result = export_service._infer_credential_type({"access_token": "xxx"})
    assert result == "OAUTH2_TOKEN"


@pytest.mark.unit
def test_parse_export_file_json(export_service: ConnectorExportService) -> None:
    """Test parsing JSON export file."""
    data = {
        "meho_export": {
            "version": "1.0",
            "exported_at": "2026-01-02T10:00:00Z",
            "encrypted": True,
        },
        "connectors": [
            {
                "name": "Test",
                "base_url": "https://example.com",
                "auth_type": "NONE",
                "auth_config": {},
            }
        ],
    }
    file_content = json.dumps(data)

    result = export_service._parse_export_file(file_content)

    assert result.meho_export.version == "1.0"
    assert len(result.connectors) == 1
    assert result.connectors[0].name == "Test"


@pytest.mark.unit
def test_parse_export_file_yaml(export_service: ConnectorExportService) -> None:
    """Test parsing YAML export file."""
    data = {
        "meho_export": {
            "version": "1.0",
            "exported_at": "2026-01-02T10:00:00Z",
            "encrypted": True,
        },
        "connectors": [
            {
                "name": "YAML Test",
                "base_url": "https://example.com",
                "auth_type": "NONE",
                "auth_config": {},
            }
        ],
    }
    file_content = yaml.dump(data)

    result = export_service._parse_export_file(file_content)

    assert result.connectors[0].name == "YAML Test"


@pytest.mark.unit
def test_parse_export_file_invalid_format(
    export_service: ConnectorExportService,
) -> None:
    """Test parsing invalid file format raises error."""
    # YAML parses most strings as valid, so use something that parses but isn't an object
    # The "not valid {{{{ json or yaml" actually parses as a YAML string
    with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
        export_service._parse_export_file("[1, 2, 3]")  # Array, not object

    assert "must be a JSON/YAML object" in str(exc_info.value)


@pytest.mark.unit
def test_parse_export_file_not_object(
    export_service: ConnectorExportService,
) -> None:
    """Test parsing non-object raises error."""
    with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
        export_service._parse_export_file('"just a string"')

    assert "must be a JSON/YAML object" in str(exc_info.value)
