"""
BM25 search service for connector operations.

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.bm25_search
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.bm25_search import OperationBM25Service

__all__ = ["OperationBM25Service"]
