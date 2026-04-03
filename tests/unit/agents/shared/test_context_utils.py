# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for shared context utilities.

Tests the build_tables_context function used for multi-turn context awareness.
"""

from __future__ import annotations

from meho_app.modules.agents.shared.context_utils import build_tables_context


class TestBuildTablesContext:
    """Tests for build_tables_context function."""

    def test_empty_tables_returns_empty_string(self) -> None:
        """Empty cached_tables should return empty string."""
        result = build_tables_context({})
        assert result == ""

    def test_none_like_values_return_empty_string(self) -> None:
        """Falsy values should return empty string."""
        result = build_tables_context({})
        assert result == ""

    def test_single_table_with_full_info(self) -> None:
        """Single table with complete info should format correctly."""
        tables = {
            "namespaces": {
                "row_count": 30,
                "columns": ["name", "status", "created_at"],
            }
        }
        result = build_tables_context(tables)

        assert "## Cached Data Tables" in result
        assert "namespaces" in result
        assert "30 rows" in result
        assert "name, status, created_at" in result

    def test_multiple_tables(self) -> None:
        """Multiple tables should all be listed."""
        tables = {
            "namespaces": {
                "row_count": 30,
                "columns": ["name", "status"],
            },
            "pods": {
                "row_count": 150,
                "columns": ["name", "namespace", "status", "node"],
            },
        }
        result = build_tables_context(tables)

        assert "namespaces" in result
        assert "30 rows" in result
        assert "pods" in result
        assert "150 rows" in result

    def test_many_columns_truncated(self) -> None:
        """More than 6 columns should be truncated with ellipsis."""
        tables = {
            "resources": {
                "row_count": 100,
                "columns": ["col1", "col2", "col3", "col4", "col5", "col6", "col7", "col8"],
            }
        }
        result = build_tables_context(tables)

        assert "..." in result
        # First 6 columns should be present
        assert "col1" in result
        assert "col6" in result

    def test_missing_row_count_shows_question_mark(self) -> None:
        """Missing row_count should show '?' placeholder."""
        tables = {
            "items": {
                "columns": ["id", "name"],
            }
        }
        result = build_tables_context(tables)

        assert "? rows" in result

    def test_missing_columns_shows_no_columns(self) -> None:
        """Missing columns should show 'no columns' placeholder."""
        tables = {
            "items": {
                "row_count": 50,
            }
        }
        result = build_tables_context(tables)

        assert "50 rows" in result
        assert "no columns" in result

    def test_header_includes_instructions(self) -> None:
        """Output should include SQL query instructions."""
        tables = {"test": {"row_count": 1, "columns": ["a"]}}
        result = build_tables_context(tables)

        assert "reduce_data SQL" in result
        assert "instead of calling new operations" in result
