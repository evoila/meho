"""Tests for DuckDB response cache."""

import json

import pytest

from meho_claude.core.data.cache import ResponseCache


@pytest.fixture
def cache(tmp_path):
    """Create a ResponseCache with a temporary DuckDB file."""
    db_path = tmp_path / "cache.duckdb"
    c = ResponseCache(db_path, size_threshold=100)
    yield c
    c.close()


class TestShouldCache:
    def test_returns_true_when_above_threshold(self, cache):
        large_data = [{"key": "x" * 200}]
        assert cache.should_cache(large_data) is True

    def test_returns_false_when_below_threshold(self, cache):
        small_data = [{"k": "v"}]
        assert cache.should_cache(small_data) is False

    def test_returns_false_for_empty_data(self, cache):
        assert cache.should_cache([]) is False


class TestCacheResponse:
    def test_creates_table_and_returns_summary(self, cache):
        data = [
            {"id": 1, "name": "pod-1", "status": "Running"},
            {"id": 2, "name": "pod-2", "status": "Pending"},
            {"id": 3, "name": "pod-3", "status": "Running"},
        ]
        result = cache.cache_response("k8s-prod", "listPods", data)

        assert result["status"] == "cached"
        assert result["row_count"] == 3
        assert "columns" in result
        assert "name" in result["columns"]
        assert "table" in result
        assert "sample" in result
        assert len(result["sample"]) == 3
        assert "query_hint" in result

    def test_sanitizes_table_name_hyphens(self, cache):
        data = [{"val": 1}]
        result = cache.cache_response("my-connector", "get-items", data)
        # Table name should not contain hyphens
        assert "-" not in result["table"]
        assert "_" in result["table"]

    def test_sanitizes_table_name_dots(self, cache):
        data = [{"val": 1}]
        result = cache.cache_response("api.v2", "list.things", data)
        assert "." not in result["table"]

    def test_sanitizes_table_name_spaces(self, cache):
        data = [{"val": 1}]
        result = cache.cache_response("my conn", "get items", data)
        assert " " not in result["table"]

    def test_replaces_existing_table(self, cache):
        data_v1 = [{"id": 1, "value": "old"}]
        cache.cache_response("test", "op1", data_v1)

        data_v2 = [{"id": 1, "value": "new"}, {"id": 2, "value": "added"}]
        result = cache.cache_response("test", "op1", data_v2)

        assert result["row_count"] == 2


class TestQuery:
    def test_executes_sql_query(self, cache):
        data = [
            {"id": 1, "name": "pod-1", "status": "Running"},
            {"id": 2, "name": "pod-2", "status": "Pending"},
        ]
        cache.cache_response("k8s", "listPods", data)

        result = cache.query("SELECT * FROM k8s_listPods")
        assert result["row_count"] == 2
        assert "columns" in result
        assert "rows" in result

    def test_injects_limit_when_not_present(self, cache):
        data = [{"id": i} for i in range(200)]
        cache.cache_response("test", "bigop", data)

        result = cache.query("SELECT * FROM test_bigop", limit=10)
        assert result["row_count"] <= 10

    def test_injects_offset(self, cache):
        data = [{"id": i, "name": f"item-{i}"} for i in range(50)]
        cache.cache_response("test", "items", data)

        result_all = cache.query("SELECT * FROM test_items", limit=50)
        result_offset = cache.query("SELECT * FROM test_items", limit=10, offset=5)

        assert result_offset["row_count"] <= 10
        # First row should be offset
        if result_offset["rows"]:
            assert result_offset["rows"][0]["id"] == 5

    def test_respects_existing_limit(self, cache):
        data = [{"id": i} for i in range(50)]
        cache.cache_response("test", "op", data)

        result = cache.query("SELECT * FROM test_op LIMIT 5")
        assert result["row_count"] == 5


class TestListTables:
    def test_lists_cached_tables(self, cache):
        cache.cache_response("k8s", "pods", [{"id": 1}])
        cache.cache_response("vmware", "vms", [{"id": 1}])

        tables = cache.list_tables()
        assert "k8s_pods" in tables
        assert "vmware_vms" in tables

    def test_empty_cache_returns_empty_list(self, cache):
        tables = cache.list_tables()
        assert tables == []
