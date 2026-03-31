# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for Connector Entity Type database operations.

TASK-97: VMware/Kubernetes/etc typed connectors.
Stores entity type definitions (VirtualMachine, Cluster, Pod, etc.).
"""

# mypy: disable-error-code="arg-type,assignment"
import uuid

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.models import ConnectorTypeModel
from meho_app.modules.connectors.schemas import ConnectorEntityType, ConnectorEntityTypeCreate


class ConnectorTypeRepository:
    """
    Repository for typed connector entity types (TASK-97).

    Stores entity type definitions (VirtualMachine, Cluster, Pod, etc.)
    for typed connectors. Used by agent to understand available entities.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_type: ConnectorTypeModel) -> ConnectorEntityType:
        """Convert SQLAlchemy model to Pydantic schema"""
        return ConnectorEntityType(
            id=str(db_type.id),
            connector_id=str(db_type.connector_id),
            tenant_id=db_type.tenant_id,
            type_name=db_type.type_name,
            description=db_type.description,
            category=db_type.category,
            properties=db_type.properties or [],
            search_content=db_type.search_content,
            created_at=db_type.created_at,
            updated_at=db_type.updated_at,
        )

    async def create_type(self, type_def: ConnectorEntityTypeCreate) -> ConnectorEntityType:
        """Create connector entity type"""
        db_type = ConnectorTypeModel(id=uuid.uuid4(), **type_def.model_dump())
        self.session.add(db_type)
        await self.session.flush()
        await self.session.refresh(db_type)
        return self._to_schema(db_type)

    async def create_types_bulk(self, types: list[ConnectorEntityTypeCreate]) -> int:
        """Bulk create entity types. Returns count created."""
        for type_def in types:
            db_type = ConnectorTypeModel(id=uuid.uuid4(), **type_def.model_dump())
            self.session.add(db_type)
        await self.session.flush()
        return len(types)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all types for a connector. Returns count deleted."""
        query = delete(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_type(self, type_id: str) -> ConnectorEntityType | None:
        """Get entity type by ID"""
        try:
            query = select(ConnectorTypeModel).where(ConnectorTypeModel.id == uuid.UUID(type_id))
            result = await self.session.execute(query)
            db_type = result.scalar_one_or_none()
            return self._to_schema(db_type) if db_type else None
        except ValueError:
            return None

    async def get_type_by_name(
        self, connector_id: str, type_name: str
    ) -> ConnectorEntityType | None:
        """Get entity type by name within a connector"""
        query = select(ConnectorTypeModel).where(
            and_(
                ConnectorTypeModel.connector_id == uuid.UUID(connector_id),
                ConnectorTypeModel.type_name == type_name,
            )
        )
        result = await self.session.execute(query)
        db_type = result.scalar_one_or_none()
        return self._to_schema(db_type) if db_type else None

    async def list_types(
        self, connector_id: str, category: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[ConnectorEntityType]:
        """List entity types with filters"""
        query = select(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )

        if category:
            query = query.where(ConnectorTypeModel.category == category)

        query = query.limit(limit).offset(offset).order_by(ConnectorTypeModel.type_name)

        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def search_types(
        self, connector_id: str, query_text: str, limit: int = 10
    ) -> list[ConnectorEntityType]:
        """Search entity types using SQL ILIKE"""
        search = f"%{query_text}%"
        query = (
            select(ConnectorTypeModel)
            .where(
                and_(
                    ConnectorTypeModel.connector_id == uuid.UUID(connector_id),
                    or_(
                        ConnectorTypeModel.type_name.ilike(search),
                        ConnectorTypeModel.description.ilike(search),
                        ConnectorTypeModel.search_content.ilike(search),
                    ),
                )
            )
            .limit(limit)
        )

        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def get_all_for_bm25(self, connector_id: str) -> list[dict]:
        """Get all entity types for BM25 indexing"""
        query = select(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        db_types = result.scalars().all()

        return [
            {"id": str(t.id), "text": t.search_content or f"{t.type_name} {t.description or ''}"}
            for t in db_types
        ]


__all__ = ["ConnectorTypeRepository"]
