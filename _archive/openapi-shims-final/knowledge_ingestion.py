"""
OpenAPI to Knowledge Ingestion

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.knowledge_ingestion
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.knowledge_ingestion import (
    ingest_openapi_to_knowledge,
    remove_connector_knowledge,
    _format_endpoint_as_text,
    _generate_search_keywords,
    _get_common_abbreviations,
    _summarize_schema,
    _create_tags,
    _create_search_metadata,
)

__all__ = [
    "ingest_openapi_to_knowledge",
    "remove_connector_knowledge",
    "_format_endpoint_as_text",
    "_generate_search_keywords",
    "_get_common_abbreviations",
    "_summarize_schema",
    "_create_tags",
    "_create_search_metadata",
]
