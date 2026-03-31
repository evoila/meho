# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repositories for SOAP connector type.

Contains:
- SoapOperationRepository: CRUD operations for SOAP operation descriptors
- SoapTypeRepository: CRUD operations for SOAP type descriptors
"""

# mypy: disable-error-code="arg-type,assignment"
import uuid

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.soap.db_models import (
    SoapOperationDescriptorModel,
    SoapTypeDescriptorModel,
)
from meho_app.modules.connectors.soap.schemas import (
    SoapOperationDescriptor,
    SoapOperationDescriptorCreate,
    SoapTypeDescriptor,
    SoapTypeDescriptorCreate,
)


class SoapOperationRepository:
    """Repository for SOAP operation descriptor operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_op: SoapOperationDescriptorModel) -> SoapOperationDescriptor:
        """Convert SQLAlchemy model to Pydantic schema"""
        return SoapOperationDescriptor(
            id=str(db_op.id),
            connector_id=str(db_op.connector_id),
            tenant_id=db_op.tenant_id,
            service_name=db_op.service_name,
            port_name=db_op.port_name,
            operation_name=db_op.operation_name,
            name=db_op.name,
            description=db_op.description,
            soap_action=db_op.soap_action,
            namespace=db_op.namespace,
            style=db_op.style,
            input_schema=db_op.input_schema or {},
            output_schema=db_op.output_schema or {},
            protocol_details=db_op.protocol_details or {},
            search_content=db_op.search_content,
            is_enabled=db_op.is_enabled,
            safety_level=db_op.safety_level,
            requires_approval=db_op.requires_approval,
            created_at=db_op.created_at,
            updated_at=db_op.updated_at,
        )

    async def create_operation(
        self, operation: SoapOperationDescriptorCreate
    ) -> SoapOperationDescriptor:
        """Create SOAP operation descriptor"""
        db_op = SoapOperationDescriptorModel(id=uuid.uuid4(), **operation.model_dump())
        self.session.add(db_op)
        await self.session.flush()
        await self.session.refresh(db_op)
        return self._to_schema(db_op)

    async def create_operations_bulk(self, operations: list[SoapOperationDescriptorCreate]) -> int:
        """Bulk create SOAP operations. Returns count of created operations."""
        for op in operations:
            db_op = SoapOperationDescriptorModel(id=uuid.uuid4(), **op.model_dump())
            self.session.add(db_op)
        await self.session.flush()
        return len(operations)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all SOAP operations for a connector. Returns count deleted."""
        query = delete(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_operation(self, operation_id: str) -> SoapOperationDescriptor | None:
        """Get SOAP operation by ID"""
        try:
            query = select(SoapOperationDescriptorModel).where(
                SoapOperationDescriptorModel.id == uuid.UUID(operation_id)
            )
            result = await self.session.execute(query)
            db_op = result.scalar_one_or_none()
            return self._to_schema(db_op) if db_op else None
        except ValueError:
            return None

    async def list_operations(
        self,
        connector_id: str,
        service_name: str | None = None,
        is_enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SoapOperationDescriptor]:
        """List SOAP operations with filters"""
        query = select(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id)
        )

        if service_name:
            query = query.where(SoapOperationDescriptorModel.service_name == service_name)
        if is_enabled is not None:
            query = query.where(SoapOperationDescriptorModel.is_enabled == is_enabled)

        query = query.limit(limit).offset(offset).order_by(SoapOperationDescriptorModel.name)

        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def search_operations(
        self, connector_id: str, query_text: str, limit: int = 10
    ) -> list[SoapOperationDescriptor]:
        """
        Search SOAP operations using SQL ILIKE.

        For full BM25 search, use the BM25Service with this repository's data.
        This method provides fast, basic search capability.
        """
        search = f"%{query_text}%"
        query = (
            select(SoapOperationDescriptorModel)
            .where(
                and_(
                    SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id),
                    or_(
                        SoapOperationDescriptorModel.name.ilike(search),
                        SoapOperationDescriptorModel.operation_name.ilike(search),
                        SoapOperationDescriptorModel.description.ilike(search),
                        SoapOperationDescriptorModel.search_content.ilike(search),
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
        Get all operations for a connector in format suitable for BM25 indexing.

        Returns list of dicts with 'id' and 'text' for BM25 corpus building.
        """
        query = select(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id),
            SoapOperationDescriptorModel.is_enabled,
        )
        result = await self.session.execute(query)
        db_ops = result.scalars().all()

        return [
            {"id": str(op.id), "text": op.search_content or f"{op.name} {op.description or ''}"}
            for op in db_ops
        ]


class SoapTypeRepository:
    """Repository for SOAP type descriptor operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_type: SoapTypeDescriptorModel) -> SoapTypeDescriptor:
        """Convert SQLAlchemy model to Pydantic schema"""
        return SoapTypeDescriptor(
            id=str(db_type.id),
            connector_id=str(db_type.connector_id),
            tenant_id=db_type.tenant_id,
            type_name=db_type.type_name,
            namespace=db_type.namespace,
            base_type=db_type.base_type,
            properties=db_type.properties or [],
            description=db_type.description,
            search_content=db_type.search_content,
            created_at=db_type.created_at,
            updated_at=db_type.updated_at,
        )

    async def create_type(self, type_def: SoapTypeDescriptorCreate) -> SoapTypeDescriptor:
        """Create SOAP type descriptor"""
        db_type = SoapTypeDescriptorModel(id=uuid.uuid4(), **type_def.model_dump())
        self.session.add(db_type)
        await self.session.flush()
        await self.session.refresh(db_type)
        return self._to_schema(db_type)

    async def create_types_bulk(self, types: list[SoapTypeDescriptorCreate]) -> int:
        """Bulk create SOAP types. Returns count of created types."""
        for type_def in types:
            db_type = SoapTypeDescriptorModel(id=uuid.uuid4(), **type_def.model_dump())
            self.session.add(db_type)
        await self.session.flush()
        return len(types)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all SOAP types for a connector. Returns count deleted."""
        query = delete(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_type(self, type_id: str) -> SoapTypeDescriptor | None:
        """Get SOAP type by ID"""
        try:
            query = select(SoapTypeDescriptorModel).where(
                SoapTypeDescriptorModel.id == uuid.UUID(type_id)
            )
            result = await self.session.execute(query)
            db_type = result.scalar_one_or_none()
            return self._to_schema(db_type) if db_type else None
        except ValueError:
            return None

    async def get_type_by_name(
        self, connector_id: str, type_name: str
    ) -> SoapTypeDescriptor | None:
        """Get SOAP type by name within a connector"""
        query = select(SoapTypeDescriptorModel).where(
            and_(
                SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id),
                SoapTypeDescriptorModel.type_name == type_name,
            )
        )
        result = await self.session.execute(query)
        db_type = result.scalar_one_or_none()
        return self._to_schema(db_type) if db_type else None

    async def list_types(
        self, connector_id: str, base_type: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[SoapTypeDescriptor]:
        """List SOAP types with filters"""
        query = select(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )

        if base_type:
            query = query.where(SoapTypeDescriptorModel.base_type == base_type)

        query = query.limit(limit).offset(offset).order_by(SoapTypeDescriptorModel.type_name)

        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def search_types(
        self, connector_id: str, query_text: str, limit: int = 10
    ) -> list[SoapTypeDescriptor]:
        """
        Search SOAP types using SQL ILIKE.

        For full BM25 search, use the BM25Service with this repository's data.
        """
        search = f"%{query_text}%"
        query = (
            select(SoapTypeDescriptorModel)
            .where(
                and_(
                    SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id),
                    or_(
                        SoapTypeDescriptorModel.type_name.ilike(search),
                        SoapTypeDescriptorModel.description.ilike(search),
                        SoapTypeDescriptorModel.search_content.ilike(search),
                    ),
                )
            )
            .limit(limit)
        )

        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def get_all_for_bm25(self, connector_id: str) -> list[dict]:
        """
        Get all types for a connector in format suitable for BM25 indexing.

        Returns list of dicts with 'id' and 'text' for BM25 corpus building.
        """
        query = select(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        db_types = result.scalars().all()

        return [
            {
                "id": str(t.id),
                "text": t.search_content
                or f"{t.type_name} {t.base_type or ''} {t.description or ''}",
            }
            for t in db_types
        ]


__all__ = [
    "SoapOperationRepository",
    "SoapTypeRepository",
]
