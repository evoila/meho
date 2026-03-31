# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Ingestion module public exports.

Usage:
    from meho_app.modules.ingestion import IngestionService, get_ingestion_service
"""

from .routes import router
from .schemas import (
    EventTemplate,
)
from .service import IngestionService, get_ingestion_service

__all__ = [
    "EventTemplate",
    "IngestionService",
    "get_ingestion_service",
    "router",
]
