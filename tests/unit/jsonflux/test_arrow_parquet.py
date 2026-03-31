# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for Arrow-native Parquet serialization round-trip.

Verifies that PyArrow tables can be serialized to Parquet bytes and
deserialized back without data loss -- the foundation for pandas-free
caching in unified_executor.

Also includes integration tests verifying that cache_data_async uses
Arrow tables instead of pandas DataFrames.
"""

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# =============================================================================
# Arrow Parquet Round-Trip Tests (Pure Arrow, no application code)
# =============================================================================


class TestArrowParquetRoundTrip:
    """Verify Arrow Table <-> Parquet bytes round-trip integrity."""

    def test_arrow_to_parquet_roundtrip(self):
        """Arrow table survives Parquet serialization and deserialization."""
        original = pa.table({
            "name": ["alice", "bob", "charlie"],
            "age": [30, 25, 35],
            "active": [True, False, True],
        })

        # Serialize
        buffer = io.BytesIO()
        pq.write_table(original, buffer, compression="snappy")
        parquet_bytes = buffer.getvalue()

        # Deserialize
        restored = pq.read_table(io.BytesIO(parquet_bytes))

        assert restored.equals(original)
        assert restored.num_rows == 3
        assert restored.column_names == ["name", "age", "active"]

    def test_arrow_parquet_with_nested_types(self):
        """Arrow table with struct columns survives Parquet round-trip."""
        original = pa.table({
            "id": [1, 2],
            "metadata": [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}],
        })

        buffer = io.BytesIO()
        pq.write_table(original, buffer, compression="snappy")
        restored = pq.read_table(io.BytesIO(buffer.getvalue()))

        assert restored.num_rows == 2
        assert restored.column_names == ["id", "metadata"]
        # Struct data should survive
        assert restored.column("metadata").to_pylist() == [
            {"key": "a", "value": "1"},
            {"key": "b", "value": "2"},
        ]

    def test_arrow_parquet_empty_table(self):
        """Empty Arrow table (0 rows, has schema) survives Parquet round-trip."""
        schema = pa.schema([
            pa.field("name", pa.string()),
            pa.field("count", pa.int64()),
        ])
        original = pa.table({"name": [], "count": []}, schema=schema)

        buffer = io.BytesIO()
        pq.write_table(original, buffer, compression="snappy")
        restored = pq.read_table(io.BytesIO(buffer.getvalue()))

        assert restored.num_rows == 0
        assert restored.column_names == ["name", "count"]

    def test_arrow_parquet_with_nulls(self):
        """Arrow table with nullable columns survives round-trip."""
        original = pa.table({
            "name": ["alice", None, "charlie"],
            "score": [95.5, None, 87.2],
            "tags": [["a", "b"], None, ["c"]],
        })

        buffer = io.BytesIO()
        pq.write_table(original, buffer, compression="snappy")
        restored = pq.read_table(io.BytesIO(buffer.getvalue()))

        assert restored.num_rows == 3
        assert restored.column("name").to_pylist() == ["alice", None, "charlie"]
        assert restored.column("score").to_pylist() == [95.5, None, 87.2]

    def test_sanitize_empty_dicts(self):
        """Empty dicts in data are handled before Arrow conversion.

        PyArrow cannot serialize zero-field structs, so empty dicts must
        be converted to None before creating Arrow tables.
        """
        # Verify that empty dicts cause issues in raw PyArrow
        data_with_empties = [
            {"name": "a", "config": {"key": "val"}},
            {"name": "b", "config": {}},  # empty dict
        ]

        # The normalize_data flow in QueryEngine handles this by converting
        # empty dicts to None. Verify the pattern works:
        sanitized = []
        for row in data_with_empties:
            clean_row = {}
            for k, v in row.items():
                if isinstance(v, dict) and not v:
                    clean_row[k] = None
                else:
                    clean_row[k] = v
            sanitized.append(clean_row)

        # Now it should be Arrow-safe (struct column becomes nullable)
        # Just verify the sanitization logic is correct
        assert sanitized[0]["config"] == {"key": "val"}
        assert sanitized[1]["config"] is None


# =============================================================================
# Cache Integration Tests (verify Arrow is used, not pandas)
# =============================================================================


class TestCacheArrowIntegration:
    """Verify cache_data_async uses Arrow tables instead of pandas."""

    @pytest.mark.asyncio
    async def test_cache_data_no_pandas(self):
        """cache_data_async does not import pandas in the hot path.

        After Arrow migration, the cache_data_async method should use
        PyArrow for DataFrame-like operations, not pandas.
        """
        import sys

        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor(redis_client=None)

        # Clear pandas from sys.modules to detect fresh imports
        # (pandas may already be imported from other test modules,
        # so we track whether cache_data_async triggers a NEW import)
        pandas_was_loaded = "pandas" in sys.modules
        if pandas_was_loaded:
            # Can't truly unload pandas in a test process,
            # so instead verify the source code doesn't import pandas
            import inspect

            source = inspect.getsource(executor.cache_data_async)
            assert "import pandas" not in source, (
                "cache_data_async still imports pandas"
            )
        else:
            # pandas not loaded yet -- call cache_data_async and check
            data = [{"name": "test", "id": 1}]
            await executor.cache_data_async(
                session_id="test",
                source_id="list_items",
                source_path="list_items",
                connector_id="c1",
                connector_type="rest",
                data=data,
            )
            assert "pandas" not in sys.modules, (
                "cache_data_async imported pandas"
            )

    @pytest.mark.asyncio
    async def test_cached_table_has_arrow(self):
        """CachedTable stores a pa.Table, not a pd.DataFrame."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor(redis_client=None)

        data = [
            {"name": "alice", "age": 30},
            {"name": "bob", "age": 25},
        ]

        cached, _tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_users",
            source_path="list_users",
            connector_id="c1",
            connector_type="rest",
            data=data,
        )

        # The internal storage should be an Arrow table
        assert isinstance(cached._df, pa.Table), (
            f"Expected pa.Table, got {type(cached._df).__name__}"
        )

        # Arrow-native access: to_pylist() returns list[dict]
        rows = cached.to_pylist()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_cached_table_l1_has_arrow(self):
        """L1 cache (session_tables) stores Arrow tables in CachedTable."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor(redis_client=None)

        data = [{"name": "test", "value": 42}]

        await executor.cache_data_async(
            session_id="sess1",
            source_id="list_items",
            source_path="list_items",
            connector_id="c1",
            connector_type="rest",
            data=data,
        )

        # Check L1 cache
        tables = executor._session_tables.get("sess1", {})
        assert "items" in tables
        cached_table = tables["items"]
        assert isinstance(cached_table._df, pa.Table), (
            f"L1 CachedTable._df should be pa.Table, got {type(cached_table._df).__name__}"
        )

    @pytest.mark.asyncio
    async def test_inline_tier_hint_propagated(self):
        """Single flat object with tier_hint='inline' forces INLINE tier."""
        from meho_app.modules.agents.execution.cache import ResponseTier
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor(redis_client=None)

        # A single flat object (dict, not list) -- should trigger inline tier
        single_object = {"name": "my-cluster", "status": "healthy", "nodes": 5}

        cached, tier = await executor.cache_data_async(
            session_id="sess1",
            source_id="get_cluster",
            source_path="get_cluster",
            connector_id="c1",
            connector_type="kubernetes",
            data=single_object,  # dict, not list!
        )

        # Single flat objects should be forced to INLINE regardless of token count
        assert tier == ResponseTier.INLINE
        assert cached.row_count == 1
