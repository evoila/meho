# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector export/import operations.

Export connectors to encrypted JSON/YAML files and import them back.
Supports encrypted credentials and conflict resolution strategies.
"""

import base64
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from meho_app.api.connectors.schemas import (
    ExportConnectorsRequest,
    ImportConnectorsRequest,
    ImportConnectorsResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/export")
async def export_connectors(
    request: ExportConnectorsRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
) -> Response:
    """
    Export connectors to encrypted JSON/YAML file.

    Exports selected connectors (or all if none specified) with encrypted
    credentials to a downloadable file. The file can be imported on any
    MEHO instance using the same password.

    Returns:
        Downloadable file with encrypted connector configurations.

    Raises:
        400: Password too short or no connectors found
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.export_encryption import PasswordTooShortError
    from meho_app.modules.connectors.export_service import (
        ConnectorExportService,
        ExportError,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        export_service = ConnectorExportService(session)

        try:
            file_content = await export_service.export_connectors(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                password=request.password,
                connector_ids=request.connector_ids if request.connector_ids else None,
                format=request.format,
            )
        except PasswordTooShortError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ExportError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # Determine content type and filename
    if request.format == "yaml":
        content_type = "application/x-yaml"
        file_ext = "yaml"
    else:
        content_type = "application/json"
        file_ext = "json"

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"meho-connectors-{timestamp}.{file_ext}"

    logger.info(
        f"📤 Exported {len(request.connector_ids) if request.connector_ids else 'all'} "
        f"connectors for user {user.user_id} (tenant: {user.tenant_id})"
    )

    return Response(
        content=file_content.encode("utf-8"),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=ImportConnectorsResponse)
async def import_connectors(
    request: ImportConnectorsRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
) -> ImportConnectorsResponse:
    """
    Import connectors from encrypted file.

    Imports connectors from a previously exported file. Credentials are
    decrypted using the provided password and stored for the importing user.

    Conflict strategies:
    - skip: Don't import connectors with existing names
    - overwrite: Replace existing connectors with imported ones
    - rename: Import with suffix (e.g., "Name" -> "Name (2)")

    Returns:
        Import result with counts and any errors.

    Raises:
        400: Invalid file format, wrong password, or password too short
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.export_encryption import PasswordTooShortError
    from meho_app.modules.connectors.export_service import (
        ConnectorExportService,
        ImportError,
    )

    # Decode base64 file content
    try:
        file_content = base64.b64decode(request.file_content).decode("utf-8")
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid file content encoding. Expected base64-encoded text.",
        ) from None

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        export_service = ConnectorExportService(session)

        try:
            result = await export_service.import_connectors(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                file_content=file_content,
                password=request.password,
                conflict_strategy=request.conflict_strategy,
            )
        except PasswordTooShortError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ImportError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        await session.commit()

    logger.info(
        f"📥 Imported {result.imported} connectors for user {user.user_id} "
        f"(tenant: {user.tenant_id}, skipped: {result.skipped}, errors: {len(result.errors)})"
    )

    return ImportConnectorsResponse(
        imported=result.imported,
        skipped=result.skipped,
        errors=result.errors,
        connectors=result.connectors,
    )
