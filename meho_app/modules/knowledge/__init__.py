# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge module public exports.

Usage:
    from meho_app.modules.knowledge import KnowledgeService, get_knowledge_service
"""

from .routes import router
from .schemas import (
    KnowledgeChunk,
    KnowledgeChunkCreate,
    KnowledgeChunkFilter,
    KnowledgeType,
)
from .service import KnowledgeService, get_knowledge_service

__all__ = [
    "KnowledgeChunk",
    "KnowledgeChunkCreate",
    "KnowledgeChunkFilter",
    "KnowledgeService",
    "KnowledgeType",
    "get_knowledge_service",
    "router",
]
