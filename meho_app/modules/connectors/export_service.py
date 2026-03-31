# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector Export/Import Service.

Exports connectors with encrypted credentials to JSON/YAML files.
Imports connectors from encrypted files with conflict resolution.
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.export_encryption import (
    DecryptionError,
    PasswordBasedEncryption,
)
from meho_app.modules.connectors.repositories import (
    ConnectorRepository,
    CredentialRepository,
)
from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    UserCredentialProvide,
)

logger = get_logger(__name__)


# =============================================================================
# Export/Import Schemas
# =============================================================================


class ExportMetadata(BaseModel):
    """Metadata for export file."""

    version: str = "1.0"
    exported_at: str
    encrypted: bool = True


class ConnectorExportData(BaseModel):
    """Single connector in export file."""

    name: str
    connector_type: str = "rest"
    description: str | None = None
    base_url: str
    auth_type: str
    auth_config: dict[str, Any] = Field(default_factory=dict)
    credential_strategy: str = "SYSTEM"
    protocol_config: dict[str, Any] | None = None
    login_url: str | None = None
    login_method: str | None = None
    login_config: dict[str, Any] | None = None
    allowed_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    blocked_methods: list[str] = Field(default_factory=list)
    default_safety_level: str = "safe"
    related_connector_ids: list[str] = Field(default_factory=list)
    # Encrypted credentials blob (only if user has credentials)
    credentials_encrypted: str | None = None
    # Original credential type (needed for import)
    credential_type: str | None = None


class ExportFile(BaseModel):
    """Complete export file structure."""

    meho_export: ExportMetadata
    connectors: list[ConnectorExportData]


class ImportResult(BaseModel):
    """Result of import operation."""

    imported: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)  # Names of imported connectors
    # Phase 9: Operations sync results
    warnings: list[str] = Field(default_factory=list)
    operations_synced: int = 0


class ExportError(Exception):
    """Error during export operation."""

    pass


class ImportError(Exception):
    """Error during import operation."""

    pass


# =============================================================================
# Export/Import Service
# =============================================================================


class ConnectorExportService:
    """
    Export/import connectors with encrypted credentials.

    Supports exporting connectors to JSON or YAML format with password-encrypted
    credentials. Importing supports conflict resolution strategies.

    Example:
        service = ConnectorExportService(session)

        # Export
        file_content = await service.export_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            password="secure-password",
            connector_ids=["id-1", "id-2"],
            format="json"
        )

        # Import
        result = await service.import_connectors(
            tenant_id="tenant-1",
            user_id="user-1",
            file_content=file_content,
            password="secure-password",
            conflict_strategy="skip"
        )
    """

    def __init__(self, session: AsyncSession):
        """
        Initialize export service.

        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.connector_repo = ConnectorRepository(session)
        self.credential_repo = CredentialRepository(session)
        self.encryption = PasswordBasedEncryption()

    async def export_connectors(
        self,
        tenant_id: str,
        user_id: str,
        password: str,
        connector_ids: list[str] | None = None,
        format: Literal["json", "yaml"] = "json",
    ) -> str:
        """
        Export connectors to encrypted JSON/YAML string.

        Args:
            tenant_id: Tenant to export connectors from
            user_id: User whose credentials to include
            password: Password for encrypting credentials (min 8 chars)
            connector_ids: Specific connector IDs to export, or None for all
            format: Output format ("json" or "yaml")

        Returns:
            JSON or YAML string containing encrypted export data

        Raises:
            PasswordTooShortError: If password is too short
            ExportError: If export fails
        """
        # Fetch connectors
        all_connectors = await self.connector_repo.list_connectors(tenant_id, active_only=False)

        # Filter by IDs if specified
        if connector_ids:
            connector_id_set = set(connector_ids)
            connectors = [c for c in all_connectors if c.id in connector_id_set]
        else:
            connectors = all_connectors

        if not connectors:
            raise ExportError("No connectors found to export")

        # Build export data
        export_connectors: list[ConnectorExportData] = []

        for connector in connectors:
            # Get user credentials if available
            credentials = await self.credential_repo.get_credentials(user_id, connector.id)

            # Build export data for this connector
            export_data = ConnectorExportData(
                name=connector.name,
                connector_type=connector.connector_type,
                description=connector.description,
                base_url=connector.base_url,
                auth_type=connector.auth_type,
                auth_config=connector.auth_config or {},
                credential_strategy=connector.credential_strategy,
                protocol_config=connector.protocol_config,
                login_url=connector.login_url,
                login_method=connector.login_method,
                login_config=connector.login_config,
                allowed_methods=connector.allowed_methods,
                blocked_methods=connector.blocked_methods,
                default_safety_level=connector.default_safety_level,
                related_connector_ids=connector.related_connector_ids or [],
            )

            # Encrypt credentials if available
            if credentials:
                credentials_json = json.dumps(credentials)
                export_data.credentials_encrypted = self.encryption.encrypt(
                    credentials_json, password
                )
                # Get credential type from credential record
                export_data.credential_type = await self._get_credential_type(user_id, connector.id)

            export_connectors.append(export_data)

        # Build export file
        export_file = ExportFile(
            meho_export=ExportMetadata(
                version="1.0",
                exported_at=datetime.now(UTC).isoformat(),
                encrypted=True,
            ),
            connectors=export_connectors,
        )

        # Serialize
        export_dict = export_file.model_dump(mode="json")

        if format == "yaml":
            return yaml.dump(export_dict, default_flow_style=False, allow_unicode=True)
        else:
            return json.dumps(export_dict, indent=2)

    async def import_connectors(
        self,
        tenant_id: str,
        user_id: str,
        file_content: str,
        password: str,
        conflict_strategy: Literal["skip", "overwrite", "rename"] = "skip",
    ) -> ImportResult:
        """
        Import connectors from encrypted JSON/YAML string.

        Args:
            tenant_id: Tenant to import connectors into
            user_id: User to store credentials for
            file_content: JSON or YAML export file content
            password: Password for decrypting credentials
            conflict_strategy: How to handle name conflicts:
                - "skip": Skip connectors with existing names
                - "overwrite": Delete existing and create new
                - "rename": Add suffix to name (e.g., "Name" -> "Name (2)")

        Returns:
            ImportResult with counts and imported connector names

        Raises:
            PasswordTooShortError: If password is too short
            ImportError: If import fails due to invalid file format
        """
        result = ImportResult()

        # Parse file content
        try:
            export_file = self._parse_export_file(file_content)
        except Exception as e:
            raise ImportError(f"Failed to parse export file: {e}") from e

        # Get existing connectors for conflict detection
        existing_connectors = await self.connector_repo.list_connectors(
            tenant_id, active_only=False
        )
        existing_by_name = {c.name: c for c in existing_connectors}

        # Import each connector
        for export_data in export_file.connectors:
            try:
                await self._import_single_connector(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    export_data=export_data,
                    password=password,
                    conflict_strategy=conflict_strategy,
                    existing_by_name=existing_by_name,
                    result=result,
                )
            except DecryptionError:
                result.errors.append(
                    f"Failed to decrypt credentials for '{export_data.name}': "
                    "wrong password or corrupted data"
                )
            except Exception as e:
                result.errors.append(f"Failed to import '{export_data.name}': {e}")
                logger.exception(f"Error importing connector '{export_data.name}'")

        return result

    async def _import_single_connector(
        self,
        tenant_id: str,
        user_id: str,
        export_data: ConnectorExportData,
        password: str,
        conflict_strategy: Literal["skip", "overwrite", "rename"],
        existing_by_name: dict[str, Connector],
        result: ImportResult,
    ) -> None:
        """
        Import a single connector with conflict resolution.

        Args:
            tenant_id: Target tenant
            user_id: User for credentials
            export_data: Connector data to import
            password: Decryption password
            conflict_strategy: Conflict resolution strategy
            existing_by_name: Map of existing connector names to connectors
            result: ImportResult to update
        """
        name = export_data.name
        existing = existing_by_name.get(name)

        if existing:
            if conflict_strategy == "skip":
                result.skipped += 1
                logger.info(f"Skipping '{name}': already exists")
                return
            elif conflict_strategy == "overwrite":
                # Delete existing connector
                await self.connector_repo.delete_connector(existing.id, tenant_id)
                logger.info(f"Deleted existing connector '{name}' for overwrite")
            elif conflict_strategy == "rename":
                # Find unique name
                name = self._generate_unique_name(name, existing_by_name)
                logger.info(f"Renamed '{export_data.name}' to '{name}' to avoid conflict")

        # Create connector
        connector_create = ConnectorCreate(
            tenant_id=tenant_id,
            name=name,
            connector_type=export_data.connector_type,  # type: ignore[arg-type]
            description=export_data.description,
            base_url=export_data.base_url,
            auth_type=export_data.auth_type,  # type: ignore[arg-type]
            auth_config=export_data.auth_config,
            credential_strategy=export_data.credential_strategy,  # type: ignore[arg-type]
            protocol_config=export_data.protocol_config,
            login_url=export_data.login_url,
            login_method=export_data.login_method,
            login_config=export_data.login_config,
            allowed_methods=export_data.allowed_methods,
            blocked_methods=export_data.blocked_methods,
            default_safety_level=export_data.default_safety_level,  # type: ignore[arg-type]
            related_connector_ids=export_data.related_connector_ids,
        )

        new_connector = await self.connector_repo.create_connector(connector_create)

        # Update existing_by_name for rename strategy
        existing_by_name[name] = new_connector

        # Decrypt and store credentials if present
        if export_data.credentials_encrypted:
            credentials_json = self.encryption.decrypt(export_data.credentials_encrypted, password)
            credentials = json.loads(credentials_json)

            # Determine credential type
            credential_type = export_data.credential_type or self._infer_credential_type(
                credentials
            )

            credential_provide = UserCredentialProvide(
                connector_id=new_connector.id,
                credential_type=credential_type,  # type: ignore[arg-type]
                credentials=credentials,
            )

            await self.credential_repo.store_credentials(user_id, credential_provide)

        # Phase 9: Sync operations for the imported connector
        from meho_app.modules.connectors.import_operations_sync import (
            sync_operations_for_imported_connector,
        )

        sync_result = await sync_operations_for_imported_connector(
            session=self.session,
            connector_id=new_connector.id,
            connector_type=export_data.connector_type,
            tenant_id=tenant_id,
            connector_name=name,
            protocol_config=export_data.protocol_config,
        )

        # Track operations synced
        result.operations_synced += sync_result.operations_synced

        # Add any warnings (not errors - connector import still succeeded)
        if sync_result.warning:
            result.warnings.append(f"{name}: {sync_result.warning}")

        result.imported += 1
        result.connectors.append(name)
        logger.info(f"Successfully imported connector '{name}'")

    def _parse_export_file(self, file_content: str) -> ExportFile:
        """
        Parse export file content (JSON or YAML).

        Args:
            file_content: JSON or YAML string

        Returns:
            Parsed ExportFile

        Raises:
            ValueError: If file format is invalid
        """
        # Try JSON first
        try:
            data = json.loads(file_content)
        except json.JSONDecodeError:
            # Try YAML
            try:
                data = yaml.safe_load(file_content)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid file format (not JSON or YAML): {e}") from e

        if not isinstance(data, dict):
            raise ValueError("Export file must be a JSON/YAML object")

        if "meho_export" not in data:
            raise ValueError("Missing 'meho_export' metadata in export file")

        if "connectors" not in data:
            raise ValueError("Missing 'connectors' array in export file")

        return ExportFile.model_validate(data)

    def _generate_unique_name(self, base_name: str, existing_by_name: dict[str, Connector]) -> str:
        """
        Generate a unique connector name by adding a suffix.

        Args:
            base_name: Original connector name
            existing_by_name: Map of existing names

        Returns:
            Unique name like "Name (2)", "Name (3)", etc.
        """
        counter = 2
        while True:
            new_name = f"{base_name} ({counter})"
            if new_name not in existing_by_name:
                return new_name
            counter += 1

    def _infer_credential_type(self, credentials: dict[str, str]) -> str:
        """
        Infer credential type from credential dict keys.

        Args:
            credentials: Credential dictionary

        Returns:
            Inferred credential type
        """
        if "api_key" in credentials or "api_token" in credentials:
            return "API_KEY"
        if "access_token" in credentials or "token" in credentials:
            return "OAUTH2_TOKEN"
        if "username" in credentials or "password" in credentials:
            return "PASSWORD"
        return "PASSWORD"  # Default

    async def _get_credential_type(self, user_id: str, connector_id: str) -> str | None:
        """
        Get credential type for a user-connector pair.

        Args:
            user_id: User ID
            connector_id: Connector ID

        Returns:
            Credential type or None
        """
        import uuid

        from sqlalchemy import select

        from meho_app.modules.connectors.models import UserCredentialModel

        try:
            query = select(UserCredentialModel.credential_type).where(
                UserCredentialModel.user_id == user_id,
                UserCredentialModel.connector_id == uuid.UUID(connector_id),
            )
            result = await self.session.execute(query)
            row = result.scalar_one_or_none()
            return row
        except Exception:
            return None
