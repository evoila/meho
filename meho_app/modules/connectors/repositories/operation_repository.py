# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for Connector Operation database operations.

TASK-97: VMware/Kubernetes/etc typed connectors.
Used by typed connectors for uniform search/list operations.
"""

# mypy: disable-error-code="arg-type,assignment"
import uuid

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.models import ConnectorOperationModel
from meho_app.modules.connectors.schemas import (
    ConnectorOperationCreate,
    ConnectorOperationDescriptor,
)


class ConnectorOperationRepository:
    """
    Repository for typed connector operations (TASK-97).

    Used by VMware, Kubernetes, and other typed connectors.
    Provides uniform search/list operations regardless of connector type.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _to_schema(db_op: ConnectorOperationModel) -> ConnectorOperationDescriptor:
        """Convert SQLAlchemy model to Pydantic schema"""
        return ConnectorOperationDescriptor(
            id=str(db_op.id),
            connector_id=str(db_op.connector_id),
            tenant_id=db_op.tenant_id,
            operation_id=db_op.operation_id,
            name=db_op.name,
            description=db_op.description,
            category=db_op.category,
            parameters=db_op.parameters or [],
            example=db_op.example,
            search_content=db_op.search_content,
            is_enabled=db_op.is_enabled,
            safety_level=db_op.safety_level,
            requires_approval=db_op.requires_approval,
            # Response schema for Brain-Muscle architecture (TASK-161)
            response_entity_type=db_op.response_entity_type,
            response_identifier_field=db_op.response_identifier_field,
            response_display_name_field=db_op.response_display_name_field,
            created_at=db_op.created_at,
            updated_at=db_op.updated_at,
        )

    async def create_operation(
        self, operation: ConnectorOperationCreate
    ) -> ConnectorOperationDescriptor:
        """Create connector operation descriptor"""
        db_op = ConnectorOperationModel(id=uuid.uuid4(), **operation.model_dump())
        self.session.add(db_op)
        await self.session.flush()
        await self.session.refresh(db_op)
        return self._to_schema(db_op)

    async def create_operations_bulk(self, operations: list[ConnectorOperationCreate]) -> int:
        """Bulk create operations. Returns count of created operations."""
        for op in operations:
            db_op = ConnectorOperationModel(id=uuid.uuid4(), **op.model_dump())
            self.session.add(db_op)
        await self.session.flush()
        return len(operations)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all operations for a connector. Returns count deleted."""
        query = delete(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, "rowcount", 0) or 0)

    async def update_operation(
        self,
        connector_id: str,
        operation_id: str,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        parameters: list[dict] | None = None,
        example: str | None = None,
        search_content: str | None = None,
        safety_level: str | None = None,
        # Response schema for Brain-Muscle architecture (TASK-161)
        response_entity_type: str | None = None,
        response_identifier_field: str | None = None,
        response_display_name_field: str | None = None,
    ) -> bool:
        """
        Update an existing operation by connector_id and operation_id.

        Returns True if operation was found and updated, False otherwise.
        """
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                ConnectorOperationModel.operation_id == operation_id,
            )
        )
        result = await self.session.execute(query)
        db_op = result.scalar_one_or_none()

        if not db_op:
            return False

        if name is not None:
            db_op.name = name
        if description is not None:
            db_op.description = description
        if category is not None:
            db_op.category = category
        if parameters is not None:
            db_op.parameters = parameters
        if example is not None:
            db_op.example = example
        if search_content is not None:
            db_op.search_content = search_content
        if safety_level is not None:
            db_op.safety_level = safety_level
        # Response schema for Brain-Muscle architecture (TASK-161)
        if response_entity_type is not None:
            db_op.response_entity_type = response_entity_type
        if response_identifier_field is not None:
            db_op.response_identifier_field = response_identifier_field
        if response_display_name_field is not None:
            db_op.response_display_name_field = response_display_name_field

        await self.session.flush()
        return True

    async def get_operation(self, operation_id: str) -> ConnectorOperationDescriptor | None:
        """Get operation by ID"""
        try:
            query = select(ConnectorOperationModel).where(
                ConnectorOperationModel.id == uuid.UUID(operation_id)
            )
            result = await self.session.execute(query)
            db_op = result.scalar_one_or_none()
            return self._to_schema(db_op) if db_op else None
        except ValueError:
            return None

    async def get_operation_by_op_id(
        self, connector_id: str, operation_id: str
    ) -> ConnectorOperationDescriptor | None:
        """Get operation by connector_id and operation_id"""
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                ConnectorOperationModel.operation_id == operation_id,
            )
        )
        result = await self.session.execute(query)
        db_op = result.scalar_one_or_none()
        return self._to_schema(db_op) if db_op else None

    async def list_operations(
        self,
        connector_id: str,
        category: str | None = None,
        is_enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConnectorOperationDescriptor]:
        """List operations with filters"""
        query = select(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id)
        )

        if category:
            query = query.where(ConnectorOperationModel.category == category)
        if is_enabled is not None:
            query = query.where(ConnectorOperationModel.is_enabled == is_enabled)

        query = query.limit(limit).offset(offset).order_by(ConnectorOperationModel.name)

        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def search_operations(
        self, connector_id: str, query_text: str, limit: int = 10
    ) -> list[ConnectorOperationDescriptor]:
        """Search operations using SQL ILIKE"""
        search = f"%{query_text}%"
        query = (
            select(ConnectorOperationModel)
            .where(
                and_(
                    ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                    or_(
                        ConnectorOperationModel.name.ilike(search),
                        ConnectorOperationModel.operation_id.ilike(search),
                        ConnectorOperationModel.description.ilike(search),
                        ConnectorOperationModel.search_content.ilike(search),
                    ),
                )
            )
            .limit(limit)
        )

        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def get_all_for_bm25(self, connector_id: str) -> list[dict]:
        """
        Get all operations for BM25 indexing with rich search content.

        Returns all fields needed for:
        1. BM25 search (text field with operation_id, name, description, category)
        2. Agent response (operation_id, name, description, parameters, example)

        The 'text' field combines multiple fields for better stemming matches:
        - "list_virtual_machines" becomes searchable via "list", "virtual", "machine"
        - Category helps match "compute", "storage", "networking" queries
        """
        query = select(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
            ConnectorOperationModel.is_enabled,
        )
        result = await self.session.execute(query)
        db_ops = result.scalars().all()

        return [
            {
                # ID for cache key generation
                "id": str(op.id),
                # Rich text for BM25 indexing (all searchable content)
                "text": " ".join(
                    [
                        str(op.operation_id) if op.operation_id else "",
                        str(op.name) if op.name else "",
                        str(op.description) if op.description else "",
                        str(op.category) if op.category else "",
                        str(op.search_content) if op.search_content else "",
                    ]
                ),
                # Fields needed for agent response
                "operation_id": op.operation_id,
                "name": op.name,
                "description": op.description,
                "category": op.category,
                "parameters": op.parameters,
                "example": op.example,
            }
            for op in db_ops
        ]


__all__ = ["ConnectorOperationRepository"]
