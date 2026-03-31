"""Tests for ChromaDB semantic search over operations."""

import pytest

from meho_claude.core.search.semantic import (
    get_chroma_client,
    get_operations_collection,
    index_operations,
    search_semantic,
)


@pytest.fixture
def chroma_collection():
    """Create an ephemeral ChromaDB collection for testing."""
    import chromadb

    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection(
        name="operations",
        metadata={"hnsw:space": "cosine"},
    )
    yield collection
    # Cleanup: delete collection
    client.delete_collection("operations")


@pytest.fixture
def indexed_collection(chroma_collection):
    """Collection pre-populated with test operations."""
    ops = [
        {
            "connector_name": "k8s-prod",
            "operation_id": "listPods",
            "display_name": "List Pods",
            "description": "List all running pods in the cluster",
            "trust_tier": "READ",
            "tags": ["kubernetes", "pods"],
            "db_id": 1,
        },
        {
            "connector_name": "k8s-prod",
            "operation_id": "deletePod",
            "display_name": "Delete Pod",
            "description": "Delete a specific pod by name",
            "trust_tier": "DESTRUCTIVE",
            "tags": ["kubernetes", "pods"],
            "db_id": 2,
        },
        {
            "connector_name": "k8s-prod",
            "operation_id": "createDeployment",
            "display_name": "Create Deployment",
            "description": "Create a new deployment in the cluster",
            "trust_tier": "WRITE",
            "tags": ["kubernetes", "deployments"],
            "db_id": 3,
        },
        {
            "connector_name": "vmware-dc",
            "operation_id": "listVMs",
            "display_name": "List Virtual Machines",
            "description": "List all VMs in vCenter",
            "trust_tier": "READ",
            "tags": ["vmware", "vms"],
            "db_id": 4,
        },
    ]
    index_operations(chroma_collection, ops, "k8s-prod")
    # Index vmware ops separately
    vmware_ops = [o for o in ops if o["connector_name"] == "vmware-dc"]
    index_operations(chroma_collection, vmware_ops, "vmware-dc")

    return chroma_collection


class TestIndexOperations:
    def test_indexes_operations(self, chroma_collection):
        ops = [
            {
                "connector_name": "test-conn",
                "operation_id": "op1",
                "display_name": "Op One",
                "description": "First operation",
                "trust_tier": "READ",
                "tags": ["test"],
                "db_id": 1,
            },
        ]
        index_operations(chroma_collection, ops, "test-conn")
        assert chroma_collection.count() == 1

    def test_re_indexing_replaces_old_docs(self, chroma_collection):
        ops_v1 = [
            {
                "connector_name": "test-conn",
                "operation_id": "op1",
                "display_name": "Old Name",
                "description": "Old description",
                "trust_tier": "READ",
                "tags": [],
                "db_id": 1,
            },
            {
                "connector_name": "test-conn",
                "operation_id": "op2",
                "display_name": "Old Two",
                "description": "Second old",
                "trust_tier": "WRITE",
                "tags": [],
                "db_id": 2,
            },
        ]
        index_operations(chroma_collection, ops_v1, "test-conn")
        assert chroma_collection.count() == 2

        # Re-index with only 1 operation
        ops_v2 = [
            {
                "connector_name": "test-conn",
                "operation_id": "op1",
                "display_name": "New Name",
                "description": "New description",
                "trust_tier": "READ",
                "tags": [],
                "db_id": 1,
            },
        ]
        index_operations(chroma_collection, ops_v2, "test-conn")
        assert chroma_collection.count() == 1

    def test_indexes_with_correct_ids(self, chroma_collection):
        ops = [
            {
                "connector_name": "myconn",
                "operation_id": "getItem",
                "display_name": "Get Item",
                "description": "Get a single item",
                "trust_tier": "READ",
                "tags": ["items"],
                "db_id": 10,
            },
        ]
        index_operations(chroma_collection, ops, "myconn")
        result = chroma_collection.get(ids=["myconn:getItem"])
        assert len(result["ids"]) == 1


class TestSearchSemantic:
    def test_returns_results_for_matching_query(self, indexed_collection):
        results = search_semantic(indexed_collection, "list pods", limit=5)
        assert len(results) > 0

    def test_results_have_required_fields(self, indexed_collection):
        results = search_semantic(indexed_collection, "pods", limit=5)
        for r in results:
            assert "id" in r
            assert "connector_name" in r
            assert "operation_id" in r
            assert "trust_tier" in r
            assert "distance" in r

    def test_returns_relevant_results(self, indexed_collection):
        results = search_semantic(indexed_collection, "virtual machines", limit=5)
        # listVMs should be among the top results
        op_ids = [r["operation_id"] for r in results]
        assert "listVMs" in op_ids

    def test_empty_collection_returns_empty(self, chroma_collection):
        results = search_semantic(chroma_collection, "anything", limit=5)
        assert results == []
