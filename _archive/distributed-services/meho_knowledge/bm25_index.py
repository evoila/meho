"""
BM25 Index Manager for keyword-based search.

Manages BM25 inverted indexes per tenant for ACL-compliant keyword search.
"""

from typing import List, Dict, Any, Tuple
from uuid import UUID
from rank_bm25 import BM25Okapi
import pickle
from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
import re
import structlog

logger = structlog.get_logger(__name__)


class BM25IndexManager:
    """
    Manages BM25 inverted indexes for keyword-based search.
    
    Maintains separate indexes per tenant for ACL compliance.
    """
    
    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache of loaded indexes
        self._indexes: Dict[str, Tuple[BM25Okapi, List[Dict]]] = {}
        
        # Thread pool for CPU-intensive BM25 operations
        self._executor = ThreadPoolExecutor(max_workers=4)
        
        logger.info("bm25_index_manager_initialized", index_dir=str(index_dir))
    
    async def build_index(
        self,
        tenant_id: UUID,
        documents: List[Dict[str, Any]]
    ) -> None:
        """
        Build or rebuild BM25 index for a tenant.
        
        Args:
            tenant_id: Tenant ID
            documents: List of documents with 'id', 'text', and 'metadata'
        """
        tenant_key = str(tenant_id)
        
        logger.info(
            "building_bm25_index",
            tenant_id=tenant_key,
            num_documents=len(documents)
        )
        
        # Tokenize documents
        tokenized_docs = []
        doc_metadata = []
        
        for doc in documents:
            # Simple tokenization (can be improved)
            tokens = self._tokenize(doc["text"])
            tokenized_docs.append(tokens)
            
            # Store metadata for retrieval
            doc_metadata.append({
                "id": doc["id"],
                "text": doc["text"],
                "metadata": doc.get("metadata", {})
            })
        
        # Build BM25 index (CPU-intensive, run in thread pool)
        bm25 = await asyncio.get_event_loop().run_in_executor(
            self._executor,
            BM25Okapi,
            tokenized_docs
        )
        
        # Cache in memory
        self._indexes[tenant_key] = (bm25, doc_metadata)
        
        # Persist to disk
        await self._save_index(tenant_id, bm25, doc_metadata)
        
        logger.info(
            "bm25_index_built",
            tenant_id=tenant_key,
            num_documents=len(documents),
            avg_doc_length=bm25.avgdl
        )
    
    async def search(
        self,
        tenant_id: UUID,
        query: str,
        top_k: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search using BM25.
        
        Args:
            tenant_id: Tenant ID
            query: Search query
            top_k: Number of results to return
        
        Returns:
            List of documents with BM25 scores
        """
        tenant_key = str(tenant_id)
        
        # Load index if not cached
        if tenant_key not in self._indexes:
            await self._load_index(tenant_id)
        
        if tenant_key not in self._indexes:
            logger.warning("bm25_index_not_found", tenant_id=tenant_key)
            return []  # No index exists
        
        bm25, doc_metadata = self._indexes[tenant_key]
        
        # Tokenize query
        query_tokens = self._tokenize(query)
        
        logger.debug(
            "bm25_search_started",
            tenant_id=tenant_key,
            query=query,
            query_tokens=query_tokens[:10],  # First 10 tokens
            top_k=top_k
        )
        
        # Search (CPU-intensive, run in thread pool)
        scores = await asyncio.get_event_loop().run_in_executor(
            self._executor,
            bm25.get_scores,
            query_tokens
        )
        
        # Get top-k results
        top_indices = sorted(
            range(len(scores)),
            key=lambda i: scores[i],
            reverse=True
        )[:top_k]
        
        results = []
        for idx in top_indices:
            # Note: BM25 scores can be negative for common terms (high IDF).
            # We include all results and rely on sorting by score.
            # Only exclude if score is NaN or extremely negative (likely error).
            if not (scores[idx] < -10 or scores[idx] != scores[idx]):  # Not error or NaN
                results.append({
                    **doc_metadata[idx],
                    "bm25_score": float(scores[idx])
                })
        
        logger.debug(
            "bm25_search_completed",
            tenant_id=tenant_key,
            query=query,
            num_results=len(results),
            top_score=results[0]["bm25_score"] if results else 0
        )
        
        return results
    
    def _tokenize(self, text: str) -> List[str]:
        """
        Tokenize text for BM25.
        
        Improved tokenization that preserves:
        - Endpoint paths (/v1/roles)
        - HTTP methods (GET, POST)
        - Technical terms (camelCase, snake_case)
        - Quoted strings
        """
        tokens = []
        
        # Extract quoted strings first (preserve as single tokens)
        quoted_pattern = r'"([^"]+)"'
        quoted_matches = re.findall(quoted_pattern, text)
        tokens.extend(quoted_matches)
        
        # Remove quoted strings from text temporarily
        text_without_quotes = re.sub(quoted_pattern, '', text)
        
        # Extract endpoint paths (/v1/roles, /api/users)
        endpoint_pattern = r'/[\w/\-]+'
        endpoint_matches = re.findall(endpoint_pattern, text_without_quotes)
        tokens.extend(endpoint_matches)
        
        # Remove endpoints
        text_without_endpoints = re.sub(endpoint_pattern, '', text_without_quotes)
        
        # Extract technical terms (preserve casing and separators)
        # camelCase, PascalCase, snake_case, kebab-case
        technical_pattern = r'\b[a-zA-Z_\-][a-zA-Z0-9_\-]*\b'
        technical_matches = re.findall(technical_pattern, text_without_endpoints)
        tokens.extend(technical_matches)
        
        # Normalize tokens (lowercase, but keep originals too for exact matches)
        normalized = []
        for token in tokens:
            if len(token) > 1:  # Skip single characters
                normalized.append(token.lower())
                if token.lower() != token:
                    normalized.append(token)  # Keep original case too
        
        return normalized
    
    async def add_documents(
        self,
        tenant_id: UUID,
        documents: List[Dict[str, Any]]
    ) -> None:
        """
        Add documents to existing index.
        
        For simplicity, rebuilds the entire index.
        For production, consider incremental updates.
        """
        tenant_key = str(tenant_id)
        
        logger.info(
            "adding_documents_to_bm25_index",
            tenant_id=tenant_key,
            num_new_documents=len(documents)
        )
        
        # Load existing index
        if tenant_key not in self._indexes:
            await self._load_index(tenant_id)
        
        # Get existing documents
        if tenant_key in self._indexes:
            _, existing_docs = self._indexes[tenant_key]
            all_docs = existing_docs + documents
        else:
            all_docs = documents
        
        # Rebuild index
        await self.build_index(tenant_id, all_docs)
    
    async def remove_document(
        self,
        tenant_id: UUID,
        document_id: str
    ) -> None:
        """
        Remove document from index.
        
        Rebuilds index without the specified document.
        """
        tenant_key = str(tenant_id)
        
        logger.info(
            "removing_document_from_bm25_index",
            tenant_id=tenant_key,
            document_id=document_id
        )
        
        if tenant_key not in self._indexes:
            await self._load_index(tenant_id)
        
        if tenant_key in self._indexes:
            _, docs = self._indexes[tenant_key]
            # Filter out document
            remaining_docs = [d for d in docs if d["id"] != document_id]
            # Rebuild
            await self.build_index(tenant_id, remaining_docs)
    
    async def _save_index(
        self,
        tenant_id: UUID,
        bm25: BM25Okapi,
        doc_metadata: List[Dict]
    ) -> None:
        """Persist index to disk"""
        tenant_key = str(tenant_id)
        index_path = self.index_dir / f"{tenant_key}.bm25.pkl"
        
        # Save in thread pool (I/O operation)
        await asyncio.get_event_loop().run_in_executor(
            self._executor,
            self._save_pickle,
            index_path,
            (bm25, doc_metadata)
        )
        
        logger.debug("bm25_index_saved", tenant_id=tenant_key, path=str(index_path))
    
    @staticmethod
    def _save_pickle(path: Path, data: Any) -> None:
        """Synchronous pickle save"""
        with open(path, 'wb') as f:
            pickle.dump(data, f)
    
    async def _load_index(self, tenant_id: UUID) -> None:
        """Load index from disk"""
        tenant_key = str(tenant_id)
        index_path = self.index_dir / f"{tenant_key}.bm25.pkl"
        
        if not index_path.exists():
            logger.debug("bm25_index_file_not_found", tenant_id=tenant_key)
            return
        
        logger.debug("loading_bm25_index", tenant_id=tenant_key, path=str(index_path))
        
        # Load in thread pool
        data = await asyncio.get_event_loop().run_in_executor(
            self._executor,
            self._load_pickle,
            index_path
        )
        
        self._indexes[tenant_key] = data
        
        logger.info("bm25_index_loaded", tenant_id=tenant_key)
    
    @staticmethod
    def _load_pickle(path: Path) -> Any:
        """Synchronous pickle load"""
        with open(path, 'rb') as f:
            return pickle.load(f)
    
    async def get_index_stats(self, tenant_id: UUID) -> Dict[str, Any]:
        """Get statistics about the index"""
        tenant_key = str(tenant_id)
        
        if tenant_key not in self._indexes:
            await self._load_index(tenant_id)
        
        if tenant_key not in self._indexes:
            return {"exists": False}
        
        bm25, doc_metadata = self._indexes[tenant_key]
        
        return {
            "exists": True,
            "num_documents": len(doc_metadata),
            "avg_doc_length": bm25.avgdl,
        }
    
    def __del__(self) -> None:
        """Cleanup executor on deletion"""
        if hasattr(self, '_executor'):
            self._executor.shutdown(wait=False)

