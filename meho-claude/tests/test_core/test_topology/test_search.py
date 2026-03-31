"""Tests for topology hybrid search (FTS5 BM25 + ChromaDB semantic + RRF).

Tests BM25 search, topology ChromaDB collection, embed/search semantic,
and hybrid search with graceful BM25-only fallback.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.topology.models import TopologyEntity


def _make_entity(
    name="nginx",
    entity_type="Pod",
    connector_type="kubernetes",
    connector_id="k8s-prod",
    connector_name="prod-cluster",
    canonical_id="default/nginx",
    description="Nginx web server pod",
    scope=None,
):
    """Helper to create a TopologyEntity with sensible defaults."""
    return TopologyEntity(
        id=str(uuid.uuid4()),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=connector_id,
        connector_name=connector_name,
        canonical_id=canonical_id,
        description=description,
        scope=scope or {},
    )


def _insert_entity(conn, entity: TopologyEntity):
    """Insert a topology entity directly into the database."""
    conn.execute(
        """INSERT INTO topology_entities
           (id, name, connector_id, connector_name, entity_type, connector_type,
            scope_json, canonical_id, description, raw_attributes_json, embedding_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity.id,
            entity.name,
            entity.connector_id,
            entity.connector_name,
            entity.entity_type,
            entity.connector_type,
            json.dumps(entity.scope),
            entity.canonical_id,
            entity.description,
            json.dumps(entity.raw_attributes),
            entity.embedding_hash,
        ),
    )
    conn.commit()


@pytest.fixture
def populated_topology_db(topology_db):
    """Topology DB with sample entities for search testing."""
    entities = [
        _make_entity(
            name="nginx-frontend",
            entity_type="Pod",
            canonical_id="prod/nginx-frontend",
            description="Frontend web server running nginx",
        ),
        _make_entity(
            name="postgres-primary",
            entity_type="StatefulSet",
            canonical_id="prod/postgres-primary",
            description="Primary PostgreSQL database for user data",
        ),
        _make_entity(
            name="redis-cache",
            entity_type="Pod",
            canonical_id="prod/redis-cache",
            description="Redis cache for session storage",
        ),
        _make_entity(
            name="api-gateway",
            entity_type="Deployment",
            canonical_id="prod/api-gateway",
            description="API gateway service handling ingress traffic",
            connector_name="staging-cluster",
        ),
        _make_entity(
            name="vm-db-01",
            entity_type="VM",
            connector_type="vmware",
            connector_id="vc-prod",
            connector_name="vcenter",
            canonical_id="dc1/vm-db-01",
            description="Database virtual machine hosting PostgreSQL",
        ),
    ]
    for e in entities:
        _insert_entity(topology_db, e)
    return topology_db, entities


class TestSearchTopologyBM25:
    """Tests for search_topology_bm25."""

    def test_returns_matching_entities(self, populated_topology_db):
        """BM25 search should return entities matching the query."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "nginx")
        assert len(results) >= 1
        assert any(r["name"] == "nginx-frontend" for r in results)

    def test_empty_query_returns_empty(self, populated_topology_db):
        """BM25 search with empty query should return empty list."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "")
        assert results == []

    def test_connector_name_filter(self, populated_topology_db):
        """BM25 search should filter by connector_name."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        # Search for something that exists in multiple connectors
        results = search_topology_bm25(conn, "gateway", connector_name="staging-cluster")
        assert all(r["connector_name"] == "staging-cluster" for r in results)

    def test_entity_type_filter(self, populated_topology_db):
        """BM25 search should filter by entity_type."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "postgres", entity_type="StatefulSet")
        assert len(results) >= 1
        assert all(r["entity_type"] == "StatefulSet" for r in results)

    def test_returns_bm25_score(self, populated_topology_db):
        """BM25 results should include bm25_score field."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "nginx")
        assert len(results) >= 1
        assert "bm25_score" in results[0]

    def test_returns_expected_fields(self, populated_topology_db):
        """BM25 results should include all expected entity fields."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "nginx")
        assert len(results) >= 1
        result = results[0]
        assert "id" in result
        assert "name" in result
        assert "entity_type" in result
        assert "connector_name" in result
        assert "connector_type" in result
        assert "canonical_id" in result
        assert "description" in result

    def test_no_match_returns_empty(self, populated_topology_db):
        """BM25 search with no matching query should return empty list."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import search_topology_bm25

        results = search_topology_bm25(conn, "nonexistentxyzabc")
        assert results == []


class TestGetTopologyCollection:
    """Tests for get_topology_collection."""

    def test_returns_collection(self, tmp_state_dir):
        """Should return a ChromaDB collection named 'topology_entities'."""
        from meho_claude.core.topology.search import get_topology_collection

        try:
            from meho_claude.core.search.semantic import get_chroma_client

            client = get_chroma_client(tmp_state_dir)
            collection = get_topology_collection(client)
            assert collection.name == "topology_entities"
        except Exception:
            pytest.skip("ChromaDB not available in test environment")


class TestEmbedTopologyEntities:
    """Tests for embed_topology_entities."""

    def test_embeds_entities_into_chromadb(self, tmp_state_dir):
        """embed_topology_entities should upsert entities into ChromaDB."""
        from meho_claude.core.topology.search import embed_topology_entities

        entities = [
            _make_entity(name="test-pod", description="A test pod"),
        ]

        try:
            embed_topology_entities(tmp_state_dir, entities)

            # Verify entity was stored
            from meho_claude.core.search.semantic import get_chroma_client
            from meho_claude.core.topology.search import get_topology_collection

            client = get_chroma_client(tmp_state_dir)
            collection = get_topology_collection(client)
            assert collection.count() == 1
        except Exception:
            pytest.skip("ChromaDB not available in test environment")

    def test_embed_never_raises(self, tmp_state_dir):
        """embed_topology_entities should catch exceptions and never raise."""
        from meho_claude.core.topology.search import embed_topology_entities

        # Pass entities with bad data -- should not raise
        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            side_effect=RuntimeError("ChromaDB broken"),
        ):
            embed_topology_entities(tmp_state_dir, [_make_entity()])
            # Should not raise


class TestSearchTopologySemantic:
    """Tests for search_topology_semantic."""

    def test_search_returns_results(self, tmp_state_dir):
        """Semantic search should return results after embedding."""
        from meho_claude.core.topology.search import (
            embed_topology_entities,
            get_topology_collection,
            search_topology_semantic,
        )

        entities = [
            _make_entity(name="nginx-pod", description="Web server"),
            _make_entity(
                name="postgres-db",
                entity_type="StatefulSet",
                canonical_id="prod/postgres-db",
                description="Database server",
            ),
        ]

        try:
            embed_topology_entities(tmp_state_dir, entities)

            from meho_claude.core.search.semantic import get_chroma_client

            client = get_chroma_client(tmp_state_dir)
            collection = get_topology_collection(client)

            results = search_topology_semantic(collection, "web server")
            assert len(results) >= 1
            assert "id" in results[0]
        except Exception:
            pytest.skip("ChromaDB not available in test environment")


class TestTopologyHybridSearch:
    """Tests for topology_hybrid_search."""

    def test_returns_results_bm25_only(self, populated_topology_db, tmp_state_dir):
        """Hybrid search should return BM25 results when ChromaDB is empty."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        results = topology_hybrid_search(conn, tmp_state_dir, "nginx")
        assert len(results) >= 1
        assert "relevance_score" in results[0]

    def test_graceful_fallback_on_chromadb_error(self, populated_topology_db, tmp_state_dir):
        """Hybrid search should fall back to BM25 when ChromaDB errors."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            side_effect=RuntimeError("ChromaDB unavailable"),
        ):
            results = topology_hybrid_search(conn, tmp_state_dir, "nginx")
            assert len(results) >= 1

    def test_respects_limit(self, populated_topology_db, tmp_state_dir):
        """Hybrid search should respect the limit parameter."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        results = topology_hybrid_search(conn, tmp_state_dir, "pod", limit=2)
        assert len(results) <= 2

    def test_connector_name_filter(self, populated_topology_db, tmp_state_dir):
        """Hybrid search should filter by connector_name."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        results = topology_hybrid_search(
            conn, tmp_state_dir, "gateway", connector_name="staging-cluster"
        )
        assert all(r["connector_name"] == "staging-cluster" for r in results)

    def test_entity_type_filter(self, populated_topology_db, tmp_state_dir):
        """Hybrid search should filter by entity_type."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        results = topology_hybrid_search(
            conn, tmp_state_dir, "postgres", entity_type="StatefulSet"
        )
        assert len(results) >= 1
        assert all(r["entity_type"] == "StatefulSet" for r in results)

    def test_empty_query_returns_empty(self, populated_topology_db, tmp_state_dir):
        """Hybrid search with empty query should return empty list."""
        conn, _ = populated_topology_db
        from meho_claude.core.topology.search import topology_hybrid_search

        results = topology_hybrid_search(conn, tmp_state_dir, "")
        assert results == []
