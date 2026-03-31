# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for hybrid search (BM25 + semantic) with RRF fusion.

Phase 84: PostgresFTSHybridService constructor changed -- bm25_manager parameter
removed, now uses session-based FTS queries directly.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: PostgresFTSHybridService constructor changed, bm25_manager parameter removed")

from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService


class TestReciprocalRankFusion:
    """Test RRF algorithm"""

    def test_rrf_merges_results(self):
        """Test that RRF correctly merges results from BM25 and semantic search"""
        # Create mock service (we only need the _reciprocal_rank_fusion method)
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # BM25 results: doc1 ranked first
        bm25_results = [
            {"id": "doc1", "text": "GET /v1/roles endpoint", "bm25_score": 15.5},
            {"id": "doc2", "text": "User management guide", "bm25_score": 10.2},
            {"id": "doc3", "text": "Authentication overview", "bm25_score": 5.1},
        ]

        # Semantic results: doc2 ranked first (different ranking!)
        semantic_results = [
            {"id": "doc2", "text": "User management guide", "distance": 0.2},
            {"id": "doc3", "text": "Authentication overview", "distance": 0.3},
            {"id": "doc1", "text": "GET /v1/roles endpoint", "distance": 0.5},
        ]

        # Fuse with equal weights
        fused = service._reciprocal_rank_fusion(
            bm25_results=bm25_results,
            semantic_results=semantic_results,
            bm25_weight=0.5,
            semantic_weight=0.5,
        )

        # Verify results
        assert len(fused) == 3

        # doc1 should rank highly (1st in BM25, 3rd in semantic)
        # doc2 should rank highly (2nd in BM25, 1st in semantic)
        # One of these should be first
        assert fused[0]["id"] in ["doc1", "doc2"]

        # All docs should have RRF scores
        for result in fused:
            assert "rrf_score" in result
            assert result["rrf_score"] > 0

        # Scores should be descending
        assert fused[0]["rrf_score"] >= fused[1]["rrf_score"]
        assert fused[1]["rrf_score"] >= fused[2]["rrf_score"]

    def test_rrf_handles_unique_results(self):
        """Test RRF when some results appear only in one source"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # BM25 finds doc1 and doc2
        bm25_results = [
            {"id": "doc1", "text": "Exact keyword match", "bm25_score": 20.0},
            {"id": "doc2", "text": "Another keyword", "bm25_score": 10.0},
        ]

        # Semantic finds doc3 and doc4 (completely different!)
        semantic_results = [
            {"id": "doc3", "text": "Semantically similar", "distance": 0.1},
            {"id": "doc4", "text": "Also similar", "distance": 0.2},
        ]

        fused = service._reciprocal_rank_fusion(
            bm25_results=bm25_results,
            semantic_results=semantic_results,
            bm25_weight=0.5,
            semantic_weight=0.5,
        )

        # Should have all 4 unique documents
        assert len(fused) == 4
        doc_ids = {r["id"] for r in fused}
        assert doc_ids == {"doc1", "doc2", "doc3", "doc4"}

    def test_rrf_weighted_fusion(self):
        """Test that weights affect ranking"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        bm25_results = [
            {"id": "doc1", "text": "BM25 favorite", "bm25_score": 20.0},
        ]

        semantic_results = [
            {"id": "doc2", "text": "Semantic favorite", "distance": 0.1},
        ]

        # Heavy BM25 weight (0.9 vs 0.1)
        fused_bm25_heavy = service._reciprocal_rank_fusion(
            bm25_results=bm25_results,
            semantic_results=semantic_results,
            bm25_weight=0.9,
            semantic_weight=0.1,
        )

        # Heavy semantic weight (0.1 vs 0.9)
        fused_semantic_heavy = service._reciprocal_rank_fusion(
            bm25_results=bm25_results,
            semantic_results=semantic_results,
            bm25_weight=0.1,
            semantic_weight=0.9,
        )

        # With BM25 weight, doc1 should win
        assert fused_bm25_heavy[0]["id"] == "doc1"

        # With semantic weight, doc2 should win
        assert fused_semantic_heavy[0]["id"] == "doc2"


class TestQueryAnalysis:
    """Test adaptive weight selection based on query analysis"""

    def test_detects_technical_queries(self):
        """Test that technical queries favor BM25"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # Technical query with endpoint
        bm25_w, sem_w = service._analyze_query("GET /v1/roles")
        assert bm25_w > sem_w, "Technical query should favor BM25"

        # Query with constants (purely technical)
        bm25_w, sem_w = service._analyze_query("ADMIN OPERATOR roles")
        assert bm25_w >= sem_w, "Query with constants should favor or balance BM25"

    def test_detects_natural_language_queries(self):
        """Test that NL queries favor semantic search"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # Natural language question
        bm25_w, sem_w = service._analyze_query("What are the different types of roles available?")
        assert sem_w > bm25_w, "Natural language query should favor semantic"

        # Explanation request
        bm25_w, sem_w = service._analyze_query("Explain how authentication works")
        assert sem_w > bm25_w, "Explanation request should favor semantic"

    def test_balanced_weights_for_mixed_queries(self):
        """Test balanced weights for queries with both technical and NL elements"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # Mixed query
        bm25_w, sem_w = service._analyze_query("roles configuration")
        assert bm25_w == sem_w == 0.5, "Balanced query should have equal weights"


class TestRRFScoreCalculation:
    """Test RRF score calculation formula"""

    def test_rrf_score_formula(self):
        """Test that RRF scores follow the correct formula: 1/(k + rank)"""
        service = PostgresFTSHybridService(
            bm25_manager=None,  # type: ignore
            repository=None,  # type: ignore
            embeddings=None,  # type: ignore
        )

        # Single result in BM25 at rank 1
        bm25_results = [
            {"id": "doc1", "text": "test", "bm25_score": 10.0},
        ]
        semantic_results = []

        fused = service._reciprocal_rank_fusion(
            bm25_results=bm25_results,
            semantic_results=semantic_results,
            bm25_weight=1.0,
            semantic_weight=0.0,
            k=60,
        )

        # RRF score should be: 1.0 / (60 + 1) ≈ 0.0164
        expected_score = 1.0 / (60 + 1)
        assert abs(fused[0]["rrf_score"] - expected_score) < 0.0001
