# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Repository for document families.

A document family groups multiple ingestion jobs that represent different
versions of the same logical document. This repository handles CRUD for
families plus the uniqueness checks (version string, file hash) that the
upload endpoint calls before queuing a new ingestion job.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.knowledge.family_schemas import DocumentFamilyCreate
from meho_app.modules.knowledge.job_models import IngestionJob
from meho_app.modules.knowledge.models import DocumentFamilyModel


class DocumentFamilyRepository:
    """CRUD + version checks for document families."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_family(self, family_create: DocumentFamilyCreate) -> DocumentFamilyModel:
        """Create a new document family."""
        connector_uuid: uuid.UUID | None = None
        if family_create.connector_id:
            connector_uuid = uuid.UUID(family_create.connector_id)

        family = DocumentFamilyModel(
            id=uuid.uuid4(),
            tenant_id=family_create.tenant_id,
            name=family_create.name,
            scope_type=family_create.scope_type,
            connector_id=connector_uuid,
            connector_type_scope=family_create.connector_type_scope,
            knowledge_type=family_create.knowledge_type,
            tags=family_create.tags,
            created_by_user_id=family_create.created_by_user_id,
        )
        self.session.add(family)
        await self.session.flush()
        await self.session.refresh(family)
        return family

    async def get_family(self, family_id: str | uuid.UUID) -> DocumentFamilyModel | None:
        """Get a family by ID."""
        try:
            family_uuid = family_id if isinstance(family_id, uuid.UUID) else uuid.UUID(family_id)
        except ValueError:
            return None

        result = await self.session.execute(
            select(DocumentFamilyModel).where(DocumentFamilyModel.id == family_uuid)
        )
        return result.scalar_one_or_none()

    async def find_by_name(
        self,
        *,
        tenant_id: str,
        name: str,
        scope_type: str,
        connector_id: str | None,
        connector_type_scope: str | None,
    ) -> DocumentFamilyModel | None:
        """Look up a family by its scope-unique name."""
        query = select(DocumentFamilyModel).where(
            DocumentFamilyModel.tenant_id == tenant_id,
            DocumentFamilyModel.name == name,
            DocumentFamilyModel.scope_type == scope_type,
        )

        if connector_id:
            try:
                query = query.where(DocumentFamilyModel.connector_id == uuid.UUID(connector_id))
            except ValueError:
                return None
        else:
            query = query.where(DocumentFamilyModel.connector_id.is_(None))

        if connector_type_scope:
            query = query.where(DocumentFamilyModel.connector_type_scope == connector_type_scope)
        else:
            query = query.where(DocumentFamilyModel.connector_type_scope.is_(None))

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_versions(self, family_id: str | uuid.UUID) -> list[IngestionJob]:
        """List all non-deleted ingestion jobs belonging to a family, newest first."""
        try:
            family_uuid = family_id if isinstance(family_id, uuid.UUID) else uuid.UUID(family_id)
        except ValueError:
            return []

        query = (
            select(IngestionJob)
            .where(
                IngestionJob.family_id == family_uuid,
                IngestionJob.status != "deleted",
            )
            .order_by(IngestionJob.started_at.desc())
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def has_version(self, family_id: str | uuid.UUID, doc_version: str) -> bool:
        """Return True if a non-deleted job with this version already exists."""
        try:
            family_uuid = family_id if isinstance(family_id, uuid.UUID) else uuid.UUID(family_id)
        except ValueError:
            return False

        query = select(IngestionJob.id).where(
            IngestionJob.family_id == family_uuid,
            IngestionJob.doc_version == doc_version,
            IngestionJob.status != "deleted",
        )
        result = await self.session.execute(query)
        return result.first() is not None

    async def has_hash(self, family_id: str | uuid.UUID, file_hash: str) -> bool:
        """Return True if a non-deleted job with this file hash already exists."""
        try:
            family_uuid = family_id if isinstance(family_id, uuid.UUID) else uuid.UUID(family_id)
        except ValueError:
            return False

        query = select(IngestionJob.id).where(
            IngestionJob.family_id == family_uuid,
            IngestionJob.file_hash == file_hash,
            IngestionJob.status != "deleted",
        )
        result = await self.session.execute(query)
        return result.first() is not None
