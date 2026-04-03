# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repositories for REST/OpenAPI connector type.

Contains:
- EndpointDescriptorRepository: CRUD operations for API endpoints
- OpenAPISpecRepository: CRUD operations for OpenAPI specifications
"""

# mypy: disable-error-code="arg-type,assignment"
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.rest.models import (
    EndpointDescriptorModel,
    OpenAPISpecModel,
)
from meho_app.modules.connectors.rest.schemas import (
    EndpointDescriptor,
    EndpointDescriptorCreate,
    EndpointFilter,
    EndpointUpdate,
    OpenAPISpec,
)


class EndpointDescriptorRepository:
    """Repository for endpoint descriptor operations"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _to_endpoint_schema(db_endpoint: EndpointDescriptorModel) -> EndpointDescriptor:
        """
        Convert SQLAlchemy model to Pydantic schema.
        Ensures UUID fields are serialized as strings and JSON columns have defaults.
        """
        return EndpointDescriptor(
            id=str(db_endpoint.id),
            connector_id=str(db_endpoint.connector_id),
            method=db_endpoint.method,
            path=db_endpoint.path,
            operation_id=db_endpoint.operation_id,
            summary=db_endpoint.summary,
            description=db_endpoint.description,
            tags=db_endpoint.tags or [],
            required_params=db_endpoint.required_params or [],
            path_params_schema=db_endpoint.path_params_schema or {},
            query_params_schema=db_endpoint.query_params_schema or {},
            body_schema=db_endpoint.body_schema or {},
            response_schema=db_endpoint.response_schema or {},
            parameter_metadata=db_endpoint.parameter_metadata,
            llm_instructions=db_endpoint.llm_instructions,
            is_enabled=db_endpoint.is_enabled,
            safety_level=db_endpoint.safety_level,
            requires_approval=db_endpoint.requires_approval,
            custom_description=db_endpoint.custom_description,
            custom_notes=db_endpoint.custom_notes,
            usage_examples=db_endpoint.usage_examples,
            agent_notes=db_endpoint.agent_notes,
            common_errors=db_endpoint.common_errors,
            success_patterns=db_endpoint.success_patterns,
            created_at=db_endpoint.created_at,
            last_modified_by=db_endpoint.last_modified_by,
            last_modified_at=db_endpoint.last_modified_at,
        )

    async def create_endpoint(self, endpoint: EndpointDescriptorCreate) -> EndpointDescriptor:
        """Create endpoint descriptor"""
        db_endpoint = EndpointDescriptorModel(id=uuid.uuid4(), **endpoint.model_dump())
        self.session.add(db_endpoint)
        await self.session.flush()
        await self.session.refresh(db_endpoint)

        return self._to_endpoint_schema(db_endpoint)

    async def upsert_endpoint(self, endpoint: EndpointDescriptorCreate) -> EndpointDescriptor:
        """
        Create or update endpoint descriptor.

        If an endpoint with the same connector_id + method + path exists, updates it.
        Otherwise, creates a new endpoint.
        """
        # Check if endpoint exists by method + path
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.connector_id == uuid.UUID(endpoint.connector_id),
                EndpointDescriptorModel.method == endpoint.method,
                EndpointDescriptorModel.path == endpoint.path,
            )
            result = await self.session.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing endpoint
                update_query = (
                    update(EndpointDescriptorModel)
                    .where(EndpointDescriptorModel.id == existing.id)
                    .values(
                        operation_id=endpoint.operation_id,
                        summary=endpoint.summary,
                        description=endpoint.description,
                        tags=endpoint.tags,
                        required_params=endpoint.required_params,
                        path_params_schema=endpoint.path_params_schema,
                        query_params_schema=endpoint.query_params_schema,
                        body_schema=endpoint.body_schema,
                        response_schema=endpoint.response_schema,
                        parameter_metadata=endpoint.parameter_metadata,
                        llm_instructions=endpoint.llm_instructions,
                        last_modified_at=datetime.now(tz=UTC),
                    )
                )
                await self.session.execute(update_query)
                await self.session.flush()
                await self.session.refresh(existing)
                return self._to_endpoint_schema(existing)
        except ValueError:
            pass

        # Create new endpoint
        return await self.create_endpoint(endpoint)

    async def get_endpoint(self, endpoint_id: str) -> EndpointDescriptor | None:
        """Get endpoint descriptor by ID"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.id == uuid.UUID(endpoint_id)
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()

            if not db_endpoint:
                return None

            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None

    async def get_endpoint_by_operation_id(
        self, connector_id: str, operation_id: str
    ) -> EndpointDescriptor | None:
        """Get endpoint descriptor by connector_id and operation_id"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.connector_id == uuid.UUID(connector_id),
                EndpointDescriptorModel.operation_id == operation_id,
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()

            if not db_endpoint:
                return None

            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None

    async def list_endpoints(self, filter: EndpointFilter) -> list[EndpointDescriptor]:
        """List endpoints with filters"""
        query = select(EndpointDescriptorModel)

        conditions = []
        if filter.connector_id:
            conditions.append(
                EndpointDescriptorModel.connector_id == uuid.UUID(filter.connector_id)
            )
        if filter.method:
            conditions.append(EndpointDescriptorModel.method == filter.method.upper())
        if filter.tags:
            for tag in filter.tags:
                conditions.append(EndpointDescriptorModel.tags.contains([tag]))
        if filter.search_text:
            search = f"%{filter.search_text}%"
            conditions.append(
                or_(
                    EndpointDescriptorModel.summary.ilike(search),
                    EndpointDescriptorModel.description.ilike(search),
                    EndpointDescriptorModel.path.ilike(search),
                )
            )

        # Filter by activation and safety
        if filter.is_enabled is not None:
            conditions.append(EndpointDescriptorModel.is_enabled == filter.is_enabled)
        if filter.safety_level:
            conditions.append(EndpointDescriptorModel.safety_level == filter.safety_level)

        if conditions:
            query = query.where(and_(*conditions))

        query = (
            query.limit(filter.limit).offset(filter.offset).order_by(EndpointDescriptorModel.path)
        )

        result = await self.session.execute(query)
        db_endpoints = result.scalars().all()

        return [self._to_endpoint_schema(e) for e in db_endpoints]

    async def update_endpoint(
        self,
        endpoint_id: str,
        update_data: EndpointUpdate,
        modified_by: str | None = None,
    ) -> EndpointDescriptor | None:
        """Update endpoint configuration"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.id == uuid.UUID(endpoint_id)
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()

            if not db_endpoint:
                return None

            # Update fields
            data = update_data.model_dump(exclude_unset=True)
            for key, value in data.items():
                setattr(db_endpoint, key, value)

            # Update audit trail
            if modified_by:
                db_endpoint.last_modified_by = modified_by
                db_endpoint.last_modified_at = datetime.now(tz=UTC)

            await self.session.flush()
            await self.session.refresh(db_endpoint)

            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None

    async def delete_endpoints_by_connector(self, connector_id: str) -> int:
        """Delete all endpoints for a connector. Returns count deleted."""
        try:
            query = delete(EndpointDescriptorModel).where(
                EndpointDescriptorModel.connector_id == uuid.UUID(connector_id)
            )
            result = await self.session.execute(query)
            await self.session.flush()
            return result.rowcount  # type: ignore[attr-defined, no-any-return]  # SQLAlchemy Result.rowcount exists at runtime
        except ValueError:
            return 0


class OpenAPISpecRepository:
    """Repository for OpenAPI spec operations"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_spec(
        self,
        connector_id: str,
        storage_uri: str,
        version: str | None = None,
        spec_version: str | None = None,
    ) -> OpenAPISpec:
        """Create a new OpenAPI spec record"""
        db_spec = OpenAPISpecModel(
            id=uuid.uuid4(),
            connector_id=uuid.UUID(connector_id),
            storage_uri=storage_uri,
            version=version,
            spec_version=spec_version,
        )
        self.session.add(db_spec)
        await self.session.flush()
        await self.session.refresh(db_spec)

        return OpenAPISpec(
            id=str(db_spec.id),
            connector_id=str(db_spec.connector_id),
            storage_uri=db_spec.storage_uri,
            version=db_spec.version,
            spec_version=db_spec.spec_version,
            created_at=db_spec.created_at,
        )

    async def get_spec_by_connector(self, connector_id: str) -> OpenAPISpec | None:
        """Get the latest OpenAPI spec for a connector"""
        try:
            query = (
                select(OpenAPISpecModel)
                .where(OpenAPISpecModel.connector_id == uuid.UUID(connector_id))
                .order_by(OpenAPISpecModel.created_at.desc())
                .limit(1)
            )

            result = await self.session.execute(query)
            db_spec = result.scalar_one_or_none()

            if not db_spec:
                return None

            return OpenAPISpec(
                id=str(db_spec.id),
                connector_id=str(db_spec.connector_id),
                storage_uri=db_spec.storage_uri,
                version=db_spec.version,
                spec_version=db_spec.spec_version,
                created_at=db_spec.created_at,
            )
        except ValueError:
            return None


__all__ = [
    "EndpointDescriptorRepository",
    "OpenAPISpecRepository",
]
