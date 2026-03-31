# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
FastAPI dependencies for Ingestion module.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.database import get_db_session
from meho_app.modules.ingestion.processor import GenericWebhookProcessor
from meho_app.modules.ingestion.repository import EventTemplateRepository


async def get_template_repository(
    session: AsyncSession = Depends(get_db_session),
) -> EventTemplateRepository:
    """Get event template repository."""
    return EventTemplateRepository(session)


async def get_webhook_processor(
    session: AsyncSession = Depends(get_db_session),
) -> GenericWebhookProcessor:
    """Get webhook processor."""
    return GenericWebhookProcessor(session)
