# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email connector registration and delivery history endpoints.

Handles Email connector creation with provider-specific credential storage,
test email delivery, and paginated delivery history.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    CreateEmailConnectorRequest,
    EmailConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/email", response_model=EmailConnectorResponse)
async def create_email_connector(  # NOSONAR (cognitive complexity)
    request: CreateEmailConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create an Email typed connector.

    Creates a typed connector for sending branded HTML email notifications
    via SMTP, SendGrid, Mailgun, Amazon SES, or Generic HTTP.
    Operations are pre-registered (send_email, check_status).
    A test email is sent during creation to verify end-to-end delivery.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.email import EmailConnector
    from meho_app.modules.connectors.email.operations import (
        EMAIL_OPERATIONS,
        EMAIL_OPERATIONS_VERSION,
        WRITE_OPERATIONS,
    )
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
    )
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import (
        ConnectorCreate,
        ConnectorOperationCreate,
        UserCredentialProvide,
    )
    from meho_app.modules.connectors.service import ConnectorService

    # Parse default recipients (comma-separated, trimmed)
    recipients = [r.strip() for r in request.default_recipients.split(",") if r.strip()]
    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="At least one default recipient email address is required.",
        )

    # Build provider-specific credentials based on provider_type
    credentials = {}
    if request.provider_type == "smtp":
        credentials = {
            "smtp_username": request.smtp_username or "",
            "smtp_password": request.smtp_password or "",
        }
    elif request.provider_type == "sendgrid":
        credentials = {
            "api_key": request.sendgrid_api_key or "",
        }
    elif request.provider_type == "mailgun":
        credentials = {
            "api_key": request.mailgun_api_key or "",
            "mailgun_domain": request.mailgun_domain or "",
        }
    elif request.provider_type == "ses":
        credentials = {
            "ses_access_key": request.ses_access_key or "",
            "ses_secret_key": request.ses_secret_key or "",
            "ses_region": request.ses_region or "us-east-1",
        }
    elif request.provider_type == "generic_http":
        credentials = {
            "endpoint_url": request.http_endpoint_url or "",
            "auth_header": request.http_auth_header or "",
            "payload_template": request.http_payload_template or "",
        }

    # Map provider_type to credential_type for storage
    credential_type_map = {
        "smtp": "SMTP",
        "sendgrid": "API_KEY",
        "mailgun": "API_KEY",
        "ses": "API_KEY",
        "generic_http": "API_KEY",
    }

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            # Build protocol config
            protocol_config: dict[str, Any] = {
                "provider_type": request.provider_type,
                "from_email": request.from_email,
                "from_name": request.from_name or "MEHO",
                "default_recipients": recipients,
                "operations_version": EMAIL_OPERATIONS_VERSION,
            }

            # Add SMTP-specific config to protocol_config (non-secret)
            if request.provider_type == "smtp":
                protocol_config["smtp_host"] = request.smtp_host or ""
                protocol_config["smtp_port"] = request.smtp_port or 587
                protocol_config["smtp_tls"] = request.smtp_tls or False

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url="",  # Email connectors have no base URL
                auth_type=credential_type_map.get(request.provider_type, "API_KEY"),
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="email",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection and send test email
            test_email_sent = False
            logger.info(f"Testing email connection: provider={request.provider_type}")
            try:
                # Build full config for connector (includes non-secret SMTP fields)
                full_config = {
                    **protocol_config,
                    "connector_name": request.name,
                    "tenant_id": user.tenant_id,
                }

                email_connector = EmailConnector(
                    connector_id=connector_id,
                    config=full_config,
                    credentials=credentials,
                )
                await email_connector.connect()
                is_connected = await email_connector.test_connection()
                await email_connector.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Could not connect to email provider ({request.provider_type}). "
                            "Check credentials and configuration."
                        ),
                    )

                test_email_sent = True
                logger.info(
                    f"Email connection verified: provider={request.provider_type}, "
                    f"recipients={', '.join(recipients)}"
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Email connection test failed: {e}")
                # Don't fail -- allow creation even if we can't test immediately

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type=credential_type_map.get(request.provider_type, "API_KEY"),
                    credentials=credentials,
                ),
            )

            # Register operations with safety_level
            op_creates = []
            for op in EMAIL_OPERATIONS:
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
                safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
                op_creates.append(
                    ConnectorOperationCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        operation_id=op.operation_id,
                        name=op.name,
                        description=op.description,
                        category=op.category,
                        parameters=list(op.parameters),
                        example=op.example,
                        search_content=search_content,
                        safety_level=safety_level,
                    )
                )

            ops_count = 0
            if op_creates:
                ops_count = await op_repo.create_operations_bulk(op_creates)

            await session.commit()

            # Create knowledge chunks for hybrid search (BM25 + semantic)
            chunks_created = 0
            try:
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                from meho_app.modules.connectors.email.sync import (
                    _sync_email_knowledge_chunks,
                )

                chunks_created = await _sync_email_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for Email operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created Email connector '{request.name}' "
                f"with {ops_count} operations "
                f"and {chunks_created} knowledge chunks"
            )

            return EmailConnectorResponse(
                id=connector_id,
                name=request.name,
                connector_type="email",
                provider_type=request.provider_type,
                from_email=request.from_email,
                test_email_sent=test_email_sent,
                operations_registered=ops_count,
                message=f"Email connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks."
                + (" Test email sent." if test_email_sent else ""),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Email connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{connector_id}/email-history")
async def get_email_history(
    connector_id: str,
    user: UserContext = Depends(get_current_user),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
):
    """
    Get paginated email delivery history for a connector.

    Returns EmailDeliveryLogModel entries ordered by created_at desc.
    Verifies tenant_id matches for security.
    """
    import uuid as uuid_mod

    from sqlalchemy import func, select

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.email.models import EmailDeliveryLogModel
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        # Verify connector exists and belongs to tenant
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        if str(connector.tenant_id) != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        if connector.connector_type != "email":
            raise HTTPException(
                status_code=400,
                detail="Email history is only available for email connectors",
            )

        connector_uuid = uuid_mod.UUID(connector_id)

        # Count total
        count_query = (
            select(func.count())
            .select_from(EmailDeliveryLogModel)
            .where(
                EmailDeliveryLogModel.connector_id == connector_uuid,
                EmailDeliveryLogModel.tenant_id == user.tenant_id,
            )
        )
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Fetch page
        offset = (page - 1) * page_size
        query = (
            select(EmailDeliveryLogModel)
            .where(
                EmailDeliveryLogModel.connector_id == connector_uuid,
                EmailDeliveryLogModel.tenant_id == user.tenant_id,
            )
            .order_by(EmailDeliveryLogModel.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await session.execute(query)
        logs = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": str(log.id),
                    "from_email": log.from_email,
                    "to_emails": log.to_emails,
                    "subject": log.subject,
                    "provider_type": log.provider_type,
                    "status": log.status,
                    "error_message": log.error_message,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in logs
            ],
        }
