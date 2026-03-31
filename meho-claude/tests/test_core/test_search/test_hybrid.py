"""Tests for hybrid search (BM25 + semantic via RRF)."""

import pytest

from meho_claude.core.search.hybrid import hybrid_search, reciprocal_rank_fusion


class TestReciprocalRankFusion:
    def test_merges_two_ranked_lists(self):
        bm25 = [
            {"id": 1, "operation_id": "op1", "connector_name": "c1"},
            {"id": 2, "operation_id": "op2", "connector_name": "c1"},
            {"id": 3, "operation_id": "op3", "connector_name": "c1"},
        ]
        semantic = [
            {"id": 2, "operation_id": "op2", "connector_name": "c1"},
            {"id": 3, "operation_id": "op3", "connector_name": "c1"},
            {"id": 4, "operation_id": "op4", "connector_name": "c1"},
        ]
        merged = reciprocal_rank_fusion(bm25, semantic, k=60)
        assert len(merged) == 4  # op1, op2, op3, op4
        # op2 and op3 appear in both lists, should be ranked higher
        op_ids = [r["operation_id"] for r in merged]
        # op2 is rank 1 in semantic, rank 2 in bm25 -> highest combined RRF
        # op3 is rank 2 in semantic, rank 3 in bm25
        assert op_ids.index("op2") < op_ids.index("op4")
        assert op_ids.index("op3") < op_ids.index("op4")

    def test_results_have_relevance_score(self):
        bm25 = [{"id": 1, "operation_id": "op1", "connector_name": "c1"}]
        semantic = [{"id": 1, "operation_id": "op1", "connector_name": "c1"}]
        merged = reciprocal_rank_fusion(bm25, semantic, k=60)
        assert len(merged) == 1
        assert "relevance_score" in merged[0]
        assert merged[0]["relevance_score"] > 0

    def test_empty_bm25_returns_semantic_only(self):
        semantic = [
            {"id": 1, "operation_id": "op1", "connector_name": "c1"},
        ]
        merged = reciprocal_rank_fusion([], semantic, k=60)
        assert len(merged) == 1
        assert merged[0]["operation_id"] == "op1"

    def test_empty_semantic_returns_bm25_only(self):
        bm25 = [
            {"id": 1, "operation_id": "op1", "connector_name": "c1"},
        ]
        merged = reciprocal_rank_fusion(bm25, [], k=60)
        assert len(merged) == 1
        assert merged[0]["operation_id"] == "op1"

    def test_both_empty_returns_empty(self):
        merged = reciprocal_rank_fusion([], [], k=60)
        assert merged == []


class TestHybridSearch:
    def test_degrades_gracefully_when_chroma_empty(self, tmp_path):
        """When ChromaDB has no data, hybrid_search should fall back to BM25-only."""
        import sqlite3

        db_path = tmp_path / "meho.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                trust_tier TEXT NOT NULL DEFAULT 'READ',
                http_method TEXT,
                url_template TEXT,
                tags TEXT DEFAULT '',
                UNIQUE(connector_name, operation_id)
            );

            CREATE VIRTUAL TABLE operations_fts USING fts5(
                operation_id, display_name, description, tags,
                content='operations', content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER operations_ai AFTER INSERT ON operations BEGIN
                INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
                VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
            END;
        """)
        conn.execute(
            "INSERT INTO operations (connector_name, operation_id, display_name, description, tags) VALUES (?, ?, ?, ?, ?)",
            ("k8s", "listPods", "List Pods", "List all pods", "kubernetes,pods"),
        )
        conn.commit()

        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        results = hybrid_search(conn, state_dir, "list pods", limit=5)
        assert len(results) > 0
        assert results[0]["operation_id"] == "listPods"
        assert "relevance_score" in results[0]

        conn.close()

    def test_results_include_relevance_score(self, tmp_path):
        """Each result from hybrid_search has a relevance_score field."""
        import sqlite3

        db_path = tmp_path / "meho.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                trust_tier TEXT NOT NULL DEFAULT 'READ',
                http_method TEXT,
                url_template TEXT,
                tags TEXT DEFAULT '',
                UNIQUE(connector_name, operation_id)
            );

            CREATE VIRTUAL TABLE operations_fts USING fts5(
                operation_id, display_name, description, tags,
                content='operations', content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER operations_ai AFTER INSERT ON operations BEGIN
                INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
                VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
            END;
        """)
        conn.execute(
            "INSERT INTO operations (connector_name, operation_id, display_name, description, tags) VALUES (?, ?, ?, ?, ?)",
            ("k8s", "listPods", "List Pods", "List all pods", "kubernetes"),
        )
        conn.commit()

        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        results = hybrid_search(conn, state_dir, "pods", limit=5)
        for r in results:
            assert "relevance_score" in r
            assert isinstance(r["relevance_score"], float)

        conn.close()
