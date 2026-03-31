# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for smart register() with shape detection, unwrap, append, and tier hints.

Covers all 9 SHAPE requirements (SHAPE-01 through SHAPE-09) plus append
and tier_hint edge cases.

TDD RED phase: All tests should FAIL initially because the current
register() wraps dicts as [data] and does not create companion tables.
"""

import pytest

from meho_app.jsonflux.query.engine import QueryEngine


@pytest.fixture
def engine():
    """Create a fresh QueryEngine for each test."""
    eng = QueryEngine()
    yield eng
    eng.close()


# ---------------------------------------------------------------
# SHAPE-01: Flat array passes through unchanged
# ---------------------------------------------------------------


class TestFlatArray:
    def test_flat_array(self, engine: QueryEngine):
        """Flat array [dict, dict, ...] registers as multi-row table (unchanged)."""
        engine.register("t", [{"a": 1}, {"a": 2}])
        rows = engine.query("SELECT * FROM t ORDER BY a")
        assert len(rows) == 2
        assert rows[0]["a"] == 1
        assert rows[1]["a"] == 2

    def test_flat_array_unchanged(self, engine: QueryEngine):
        """register('t', [{...}], unwrap='auto') behaves same as before."""
        engine.register("t", [{"a": 1}], unwrap="auto")
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        assert rows[0]["a"] == 1

    def test_existing_behavior_preserved(self, engine: QueryEngine):
        """Regression: register('t', [{...},{...},{...}]) creates 3 rows."""
        engine.register("t", [{"x": 1}, {"x": 2}, {"x": 3}])
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 3


# ---------------------------------------------------------------
# SHAPE-02: Wrapped collection unwrapped to main + meta
# ---------------------------------------------------------------


class TestWrappedCollection:
    def test_wrapped_collection(self, engine: QueryEngine):
        """Wrapped collection creates main table + _meta companion."""
        engine.register(
            "t", {"results": [{"id": 1}, {"id": 2}], "total": 2}
        )
        # Main table should have 2 rows (unwrapped)
        rows = engine.query("SELECT * FROM t ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2

        # Companion metadata table should exist
        assert "t_meta" in engine.tables
        meta_rows = engine.query("SELECT * FROM t_meta")
        assert len(meta_rows) == 1
        assert meta_rows[0]["total"] == 2


# ---------------------------------------------------------------
# SHAPE-03: Single flat object becomes 1-row table
# ---------------------------------------------------------------


class TestSingleObject:
    def test_single_object(self, engine: QueryEngine):
        """Single flat object registers as 1-row table."""
        engine.register("t", {"name": "foo", "status": "ok"})
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        assert rows[0]["name"] == "foo"
        assert rows[0]["status"] == "ok"

    def test_single_object_tier_hint(self, engine: QueryEngine):
        """Single flat object sets tier_hint='inline' in engine.tables."""
        engine.register("t", {"name": "foo"})
        assert engine.tables["t"]["tier_hint"] == "inline"


# ---------------------------------------------------------------
# SHAPE-04: Multi-collection split into multiple tables
# ---------------------------------------------------------------


class TestMultiCollection:
    def test_multi_collection(self, engine: QueryEngine):
        """Multi-collection dict splits into separate tables per key."""
        engine.register(
            "t",
            {
                "pods": [{"n": "a"}],
                "services": [{"n": "b"}],
            },
        )
        assert "t_pods" in engine.tables
        assert "t_services" in engine.tables

        pods = engine.query("SELECT * FROM t_pods")
        assert len(pods) == 1
        assert pods[0]["n"] == "a"

        svcs = engine.query("SELECT * FROM t_services")
        assert len(svcs) == 1
        assert svcs[0]["n"] == "b"

    def test_multi_collection_with_metadata(self, engine: QueryEngine):
        """Multi-collection with scalar metadata creates _meta table too."""
        engine.register(
            "t",
            {
                "pods": [{"n": "a"}],
                "svcs": [{"n": "b"}],
                "total": 5,
            },
        )
        assert "t_pods" in engine.tables
        assert "t_svcs" in engine.tables
        assert "t_meta" in engine.tables

        meta = engine.query("SELECT * FROM t_meta")
        assert len(meta) == 1
        assert meta[0]["total"] == 5


# ---------------------------------------------------------------
# SHAPE-05: Nested metadata flattened to dot-notation
# ---------------------------------------------------------------


class TestNestedMetadata:
    def test_nested_metadata(self, engine: QueryEngine):
        """Nested metadata objects are flattened to dot-notation columns."""
        engine.register(
            "t",
            {
                "items": [{"id": 1}],
                "links": {"next": "/p2", "prev": None},
            },
        )
        # Main table
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        assert rows[0]["id"] == 1

        # Metadata with flattened nested keys
        assert "t_meta" in engine.tables
        meta = engine.query("SELECT * FROM t_meta")
        assert len(meta) == 1
        assert meta[0]["links.next"] == "/p2"
        assert meta[0]["links.prev"] is None


# ---------------------------------------------------------------
# SHAPE-06: Scalar lists become single-column tables
# ---------------------------------------------------------------


class TestScalarLists:
    def test_scalar_lists(self, engine: QueryEngine):
        """Scalar lists become their own single-column tables."""
        engine.register(
            "t",
            {
                "items": [{"id": 1}],
                "tags": ["prod", "critical"],
            },
        )
        # Main table from items
        assert "t" in engine.tables
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1

        # Scalar list table
        assert "t_tags" in engine.tables
        tags = engine.query("SELECT * FROM t_tags ORDER BY tags")
        assert len(tags) == 2
        assert tags[0]["tags"] == "critical"
        assert tags[1]["tags"] == "prod"


# ---------------------------------------------------------------
# SHAPE-07: Empty data registers empty table with schema
# ---------------------------------------------------------------


class TestEmptyData:
    def test_empty_data_dict(self, engine: QueryEngine):
        """Empty wrapped collection creates 0-row table + meta."""
        engine.register("t", {"results": [], "total": 0})
        assert "t" in engine.tables
        count = engine.conn.execute("SELECT count(*) FROM t").fetchone()[0]
        assert count == 0

        assert "t_meta" in engine.tables
        meta = engine.query("SELECT * FROM t_meta")
        assert meta[0]["total"] == 0

    def test_empty_data_list(self, engine: QueryEngine):
        """Empty list registers empty table."""
        engine.register("t", [])
        assert "t" in engine.tables
        count = engine.conn.execute("SELECT count(*) FROM t").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------
# SHAPE-08: unwrap=True forces unwrap
# ---------------------------------------------------------------


class TestUnwrapTrue:
    def test_unwrap_true(self, engine: QueryEngine):
        """unwrap=True forces unwrap even when heuristic would not."""
        engine.register(
            "t",
            {"items": [{"id": 1}], "total": 1},
            unwrap=True,
        )
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        # No companion metadata table when forced unwrap
        assert "t_meta" not in engine.tables


# ---------------------------------------------------------------
# SHAPE-09: unwrap=False forces 1-row table
# ---------------------------------------------------------------


class TestUnwrapFalse:
    def test_unwrap_false(self, engine: QueryEngine):
        """unwrap=False forces 1-row table even when heuristic would unwrap."""
        engine.register(
            "t",
            {"items": [{"id": 1}], "total": 1},
            unwrap=False,
        )
        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        # The entire dict is a single row with items and total as columns
        assert "total" in rows[0]


# ---------------------------------------------------------------
# Append behavior
# ---------------------------------------------------------------


class TestAppend:
    def test_append_true(self, engine: QueryEngine):
        """append=True concatenates new data with existing table."""
        engine.register("t", [{"a": 1}])
        engine.register("t", [{"a": 2}], append=True)

        rows = engine.query("SELECT * FROM t ORDER BY a")
        assert len(rows) == 2
        assert rows[0]["a"] == 1
        assert rows[1]["a"] == 2

    def test_append_false_replaces(self, engine: QueryEngine):
        """append=False (default) replaces existing table."""
        engine.register("t", [{"a": 1}])
        engine.register("t", [{"a": 2}], append=False)

        rows = engine.query("SELECT * FROM t")
        assert len(rows) == 1
        assert rows[0]["a"] == 2

    def test_append_wrapped_collection(self, engine: QueryEngine):
        """Append with wrapped collection: rows accumulate, meta replaced."""
        engine.register(
            "t", {"items": [{"id": 1}], "cursor": "abc"}
        )
        engine.register(
            "t",
            {"items": [{"id": 2}], "cursor": "def"},
            append=True,
        )

        # Main table should have 2 rows (accumulated)
        rows = engine.query("SELECT * FROM t ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2

        # Metadata should be REPLACED (latest page)
        meta = engine.query("SELECT * FROM t_meta")
        assert len(meta) == 1
        assert meta[0]["cursor"] == "def"
