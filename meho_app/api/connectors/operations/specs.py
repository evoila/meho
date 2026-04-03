# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
OpenAPI spec operations.

Handles OpenAPI specification upload and download for connectors.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from meho_app.api.auth import get_current_user
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/{connector_id}/openapi-spec")
async def upload_openapi_spec(  # NOSONAR (cognitive complexity)
    connector_id: str, file: UploadFile = File(...), user: UserContext = Depends(get_current_user)
):
    """
    Upload OpenAPI specification for a connector.

    This:
    1. Stores the original file in object storage for debugging/auditing
    2. Parses the spec and creates endpoint descriptors
    3. Ingests endpoints into knowledge base for natural language search
    """
    from meho_app.api.database import create_knowledge_session_maker, create_openapi_session_maker
    from meho_app.modules.connectors.repositories import (
        ConnectorRepository,
        ConnectorTypeRepository,
    )
    from meho_app.modules.connectors.rest.instruction_generator import (
        InstructionGenerator,
        should_generate_instructions,
    )
    from meho_app.modules.connectors.rest.knowledge_ingestion import ingest_openapi_to_knowledge
    from meho_app.modules.connectors.rest.repository import (
        EndpointDescriptorRepository,
        OpenAPISpecRepository,
    )
    from meho_app.modules.connectors.rest.schemas import (
        EndpointDescriptorCreate,
        EndpointFilter,
        EndpointUpdate,
    )
    from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate
    from meho_app.modules.knowledge.embeddings import get_embedding_provider
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
    from meho_app.modules.knowledge.object_storage import ObjectStorage
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Read file content
        spec_content = await file.read()

        # Step 1: Parse and validate the OpenAPI spec FIRST (before storing)
        parser = OpenAPIParser()
        try:
            spec_dict = parser.parse(spec_content.decode("utf-8"))
            parser.validate_spec(spec_dict)

            openapi_version = spec_dict.get("openapi", "unknown")
            api_title = spec_dict.get("info", {}).get("title", "Unknown API")
            api_version = spec_dict.get("info", {}).get("version", "unknown")
            endpoint_count = len(spec_dict.get("paths", {}))

            logger.info(
                f"✅ OpenAPI spec validated successfully:\n"
                f"   API: {api_title} v{api_version}\n"
                f"   OpenAPI version: {openapi_version}\n"
                f"   Endpoints: {endpoint_count}"
            )

        except ValueError as e:
            logger.error(f"❌ OpenAPI spec validation failed: {e}")
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"❌ Failed to parse OpenAPI spec: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to parse OpenAPI spec: {e}") from e

        # Step 2: Store original file in object storage
        try:
            object_storage = ObjectStorage()
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
            filename = file.filename or "openapi-spec.json"
            file_ext = filename.split(".")[-1] if "." in filename else "json"
            storage_key = f"connectors/{connector_id}/openapi-spec-{timestamp}.{file_ext}"
            content_type = file.content_type or "application/json"
            if file_ext in ["yaml", "yml"]:
                content_type = "application/x-yaml"

            storage_uri = object_storage.upload_document(
                file_bytes=spec_content, key=storage_key, content_type=content_type
            )

            logger.info(f"✅ Stored OpenAPI spec in object storage: {storage_uri}")
        except Exception as e:
            logger.error(f"Failed to store OpenAPI spec in object storage: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to store spec file: {e}") from e

        # Step 3: Save OpenAPI spec metadata to database
        spec_repo = OpenAPISpecRepository(session)
        openapi_spec = await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version=openapi_version,
            spec_version=api_version,
        )

        logger.info(f"✅ Created OpenAPI spec metadata record (id={openapi_spec.id})")

        # Step 4: Extract and save endpoints
        endpoints = parser.extract_endpoints(spec_dict)
        endpoint_repo = EndpointDescriptorRepository(session)

        for endpoint_data in endpoints:
            endpoint_create = EndpointDescriptorCreate(
                connector_id=connector_id,
                method=endpoint_data["method"],
                path=endpoint_data["path"],
                operation_id=endpoint_data.get("operation_id"),
                summary=endpoint_data.get("summary", ""),
                description=endpoint_data.get("description", ""),
                tags=endpoint_data.get("tags", []),
                path_params_schema=endpoint_data.get("path_params_schema", {}),
                query_params_schema=endpoint_data.get("query_params_schema", {}),
                body_schema=endpoint_data.get("body_schema", {}),
                response_schema=endpoint_data.get("response_schema", {}),
                parameter_metadata=endpoint_data.get("parameter_metadata"),
            )
            await endpoint_repo.upsert_endpoint(endpoint_create)

        await session.commit()

        # Step 5: Extract and store OpenAPI schema types
        schema_types_created = 0
        try:
            schema_types = parser.extract_schema_types(spec_dict)
            if schema_types:
                type_repo = ConnectorTypeRepository(session)
                await type_repo.delete_by_connector(connector_id)

                type_creates = []
                for schema_type in schema_types:
                    type_creates.append(
                        ConnectorEntityTypeCreate(
                            connector_id=connector_id,
                            tenant_id=user.tenant_id,
                            type_name=schema_type["type_name"],
                            description=schema_type["description"],
                            category=schema_type["category"],
                            properties=schema_type["properties"],
                            search_content=schema_type["search_content"],
                        )
                    )

                schema_types_created = await type_repo.create_types_bulk(type_creates)
                await session.commit()

                logger.info(
                    f"✅ Extracted {schema_types_created} schema types from OpenAPI spec "
                    f"for connector {connector.name}"
                )
        except Exception as e:
            logger.warning(f"Failed to extract OpenAPI schema types: {e}")

        # Step 6: Generate LLM instructions for complex write endpoints
        instructions_generated = 0
        try:
            instruction_generator = InstructionGenerator(use_llm=False)

            for endpoint_data in endpoints:
                method = endpoint_data["method"]
                body_schema = endpoint_data.get("body_schema", {})

                if not should_generate_instructions(method, body_schema):
                    continue

                instructions = await instruction_generator.generate_for_endpoint(
                    endpoint_id=endpoint_data.get("operation_id", ""),
                    method=method,
                    path=endpoint_data["path"],
                    body_schema=body_schema,
                    description=endpoint_data.get("description"),
                    summary=endpoint_data.get("summary"),
                    operation_id=endpoint_data.get("operation_id"),
                )

                existing_endpoints = await endpoint_repo.list_endpoints(
                    EndpointFilter(connector_id=connector_id, method=method, limit=500)
                )

                for existing in existing_endpoints:
                    if existing.path == endpoint_data["path"]:
                        await endpoint_repo.update_endpoint(
                            existing.id,
                            EndpointUpdate(llm_instructions=instructions.model_dump()),
                            modified_by="system:instruction_generator",
                        )
                        instructions_generated += 1
                        break

            if instructions_generated > 0:
                await session.commit()
                logger.info(
                    f"✅ Generated LLM instructions for {instructions_generated} complex write endpoints"
                )
        except Exception as e:
            logger.warning(f"Failed to generate LLM instructions: {e}")

        # Step 7: Ingest OpenAPI endpoints into knowledge base
        knowledge_chunks_created = 0
        try:
            knowledge_session_maker = create_knowledge_session_maker()
            async with knowledge_session_maker() as knowledge_session:
                repository = KnowledgeRepository(knowledge_session)
                embedding_provider = get_embedding_provider()
                hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
                knowledge_store = KnowledgeStore(
                    repository=repository,
                    embedding_provider=embedding_provider,
                    hybrid_search_service=hybrid_search,
                )

                knowledge_chunks_created = await ingest_openapi_to_knowledge(
                    spec_dict=spec_dict,
                    connector_id=connector_id,
                    connector_name=connector.name,
                    knowledge_store=knowledge_store,
                    user_context=user,
                )

                await knowledge_session.commit()

                logger.info(
                    f"✅ Ingested {knowledge_chunks_created} OpenAPI endpoints to knowledge base "
                    f"for connector {connector.name}"
                )
        except Exception as e:
            logger.error(
                f"⚠️  Failed to ingest OpenAPI endpoints to knowledge base: {e}. "
                f"Endpoints are saved but won't be searchable by natural language.",
                exc_info=True,
            )

        # Step 8: Generate skill from operations
        skill_generation_result = None
        try:
            from meho_app.modules.connectors.skill_generation import SkillGenerator

            generator = SkillGenerator()
            skill_generation_result = await generator.generate_skill(
                session=session,
                connector_id=connector_id,
                connector_type=connector.connector_type,
                connector_name=connector.name,
            )
            await session.commit()
            logger.info(
                f"Generated skill for {connector.name}: "
                f"quality={skill_generation_result.quality_score}/5, "
                f"ops={skill_generation_result.operation_count}"
            )
        except Exception as e:
            logger.warning(f"Failed to generate skill for {connector.name}: {e}")

        return {
            "message": f"Uploaded OpenAPI spec with {len(endpoints)} endpoints and {schema_types_created} types",
            "api_title": api_title,
            "api_version": api_version,
            "openapi_version": openapi_version,
            "endpoints_count": len(endpoints),
            "schema_types_count": schema_types_created,
            "knowledge_chunks_created": knowledge_chunks_created,
            "storage_uri": storage_uri,
            "spec_id": openapi_spec.id,
            "skill_quality_score": skill_generation_result.quality_score
            if skill_generation_result
            else None,
            "skill_generated": skill_generation_result is not None,
        }


@router.get("/{connector_id}/openapi-spec/download")
async def download_openapi_spec(connector_id: str, user: UserContext = Depends(get_current_user)):
    """
    Download the original OpenAPI specification file.

    Returns the stored spec file for debugging, auditing, or re-use.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.rest.repository import OpenAPISpecRepository
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        spec_repo = OpenAPISpecRepository(session)
        openapi_spec = await spec_repo.get_spec_by_connector(connector_id)

        if not openapi_spec or not openapi_spec.storage_uri:
            raise HTTPException(status_code=404, detail="No OpenAPI spec found for this connector")

        storage_key = openapi_spec.storage_uri.split("/", 3)[-1]

        try:
            object_storage = ObjectStorage()
            file_bytes = object_storage.download_document(storage_key)

            file_ext = storage_key.split(".")[-1]
            if file_ext in ["yaml", "yml"]:
                content_type = "application/x-yaml"
                filename = f"{connector.name.replace(' ', '-')}-openapi.yaml"
            else:
                content_type = "application/json"
                filename = f"{connector.name.replace(' ', '-')}-openapi.json"

            logger.info(
                f"📥 User {user.user_id} downloaded OpenAPI spec for connector {connector.name}"
            )

            return Response(
                content=file_bytes,
                media_type=content_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as e:
            logger.error(f"Failed to download OpenAPI spec: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to download spec: {e}") from e
