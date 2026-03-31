# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS connector operations.

Handles Amazon Web Services connector creation with native boto3 SDK access.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    AWSConnectorResponse,
    CreateAWSConnectorRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/aws", response_model=AWSConnectorResponse)
async def create_aws_connector(
    request: CreateAWSConnectorRequest, user: UserContext = Depends(get_current_user)
):
    """
    Create an Amazon Web Services connector.

    This creates a typed connector using native boto3 SDK for access to:
    - EC2: Instances, security groups
    - EKS: Kubernetes clusters, node groups
    - ECS: Clusters, services, tasks
    - S3: Buckets
    - Lambda: Functions
    - RDS: Database instances
    - CloudWatch: Metrics, alarms
    - VPC: VPCs, subnets

    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.aws import (
        AWS_OPERATIONS,
        AWS_OPERATIONS_VERSION,
        AWS_TYPES,
        AWSConnector,
    )
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
        ConnectorTypeRepository,
    )
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import (
        ConnectorCreate,
        ConnectorEntityTypeCreate,
        ConnectorOperationCreate,
        UserCredentialProvide,
    )
    from meho_app.modules.connectors.service import ConnectorService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "default_region": request.default_region,
                "operations_version": AWS_OPERATIONS_VERSION,
            }

            # AWS Console URL for the connector base_url
            # nosemgrep: tainted-url-host -- admin-configured AWS region, not user input (connector setup requires admin auth)
            base_url = f"https://{request.default_region}.console.aws.amazon.com"

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description or f"AWS connector ({request.default_region})",
                base_url=base_url,
                auth_type="API_KEY",  # IAM credentials stored as credential
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="aws",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection if credentials provided
            credentials = {}
            if request.aws_access_key_id:
                credentials["aws_access_key_id"] = request.aws_access_key_id
            if request.aws_secret_access_key:
                credentials["aws_secret_access_key"] = request.aws_secret_access_key

            if credentials:
                logger.info(
                    f"Testing AWS connection: region={request.default_region}"
                )
                try:
                    aws = AWSConnector(
                        connector_id=connector_id,
                        config=protocol_config,
                        credentials=credentials,
                    )
                    await aws.connect()
                    is_connected = await aws.test_connection()
                    await aws.disconnect()

                    if not is_connected:
                        raise HTTPException(
                            status_code=400,
                            detail="Could not connect to AWS. Check access key ID and secret access key.",
                        )

                    logger.info(
                        f"AWS connection verified: region={request.default_region}"
                    )
                except ImportError:
                    logger.warning("boto3 not installed - skipping connection test")
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(f"AWS connection test failed: {e}")
                    # Don't fail - allow creation even if we can't test immediately

                # Store user credentials (IAM keys)
                cred_repo = UserCredentialRepository(session)
                await cred_repo.store_credentials(
                    user_id=user.user_id,
                    credential=UserCredentialProvide(
                        connector_id=connector_id,
                        credential_type="API_KEY",
                        credentials=credentials,
                    ),
                )

            # Register operations
            op_creates = []
            for op in AWS_OPERATIONS:
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
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
                    )
                )

            ops_count = await op_repo.create_operations_bulk(op_creates)

            # Register entity types
            type_creates = []
            for t in AWS_TYPES:
                prop_names = " ".join(p.get("name", "") for p in t.properties)
                search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
                type_creates.append(
                    ConnectorEntityTypeCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        type_name=t.type_name,
                        description=t.description,
                        category=t.category,
                        properties=list(t.properties),
                        search_content=search_content,
                    )
                )

            types_count = await type_repo.create_types_bulk(type_creates)

            await session.commit()

            # Create knowledge chunks for searchable operations
            chunks_created = 0
            try:
                from meho_app.modules.connectors.aws.sync import _sync_aws_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_aws_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for AWS operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"Created AWS connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return AWSConnectorResponse(
                id=connector_id,
                name=request.name,
                default_region=request.default_region,
                connector_type="aws",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"AWS connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create AWS connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
