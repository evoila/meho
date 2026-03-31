# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Protocol definitions for MEHO application.

These protocols define interfaces for dependency injection and testing.
Services should depend on protocols (abstractions), not concrete implementations.

Usage:
    from meho_app.protocols.knowledge import IKnowledgeRepository
    from meho_app.protocols.openapi import IConnectorRepository
    # etc.

Or import all at once (with lazy loading):
    from meho_app.protocols import IKnowledgeRepository, IConnectorRepository

Benefits:
    - Easier unit testing with mock implementations
    - Clear contracts between modules
    - Dependency inversion principle (SOLID)
    - Better IDE support for type checking

Note: Protocols use TYPE_CHECKING guards to avoid circular imports.
Import protocols directly from their submodules when needed in service files.
"""


def __getattr__(name: str):
    """
    Lazy import to avoid circular dependencies.

    This allows `from meho_app.protocols import IKnowledgeRepository` to work
    without eagerly loading all modules at import time.
    """
    # Knowledge protocols
    if name in ("IKnowledgeRepository", "IHybridSearchService", "IKnowledgeStore"):
        from meho_app.protocols.knowledge import (
            IHybridSearchService,
            IKnowledgeRepository,
            IKnowledgeStore,
        )

        _protocols = {
            "IKnowledgeRepository": IKnowledgeRepository,
            "IHybridSearchService": IHybridSearchService,
            "IKnowledgeStore": IKnowledgeStore,
        }
        return _protocols[name]

    # EmbeddingProvider (already a Protocol in embeddings.py)
    if name == "IEmbeddingProvider":
        from meho_app.modules.knowledge.embeddings import EmbeddingProvider

        return EmbeddingProvider

    # OpenAPI protocols
    if name in (
        "IConnectorRepository",
        "IEndpointRepository",
        "IOperationRepository",
        "IHTTPClient",
        "ISessionManager",
    ):
        from meho_app.protocols.openapi import (
            IConnectorRepository,
            IEndpointRepository,
            IHTTPClient,
            IOperationRepository,
            ISessionManager,
        )

        _protocols = {
            "IConnectorRepository": IConnectorRepository,
            "IEndpointRepository": IEndpointRepository,
            "IOperationRepository": IOperationRepository,
            "IHTTPClient": IHTTPClient,
            "ISessionManager": ISessionManager,
        }
        return _protocols[name]

    # Agent protocols
    if name == "IAgentDependencies":
        from meho_app.protocols.agent import IAgentDependencies

        return IAgentDependencies

    # Ingestion protocols
    if name in ("IEventTemplateRepository", "IWebhookProcessor"):
        from meho_app.protocols.ingestion import (
            IEventTemplateRepository,
            IWebhookProcessor,
        )

        _protocols = {
            "IEventTemplateRepository": IEventTemplateRepository,
            "IWebhookProcessor": IWebhookProcessor,
        }
        return _protocols[name]

    raise AttributeError(f"module 'meho_app.protocols' has no attribute '{name}'")


__all__ = [
    # Agent
    "IAgentDependencies",
    # OpenAPI
    "IConnectorRepository",
    "IEmbeddingProvider",
    "IEndpointRepository",
    # Ingestion
    "IEventTemplateRepository",
    "IHTTPClient",
    "IHybridSearchService",
    # Knowledge
    "IKnowledgeRepository",
    "IKnowledgeStore",
    "IOperationRepository",
    "ISessionManager",
    "IWebhookProcessor",
]
