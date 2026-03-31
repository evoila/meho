"""Tests for data query CLI command."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from meho_claude.cli import app

runner = CliRunner()


def _mock_settings(tmp_state_dir: Path):
    settings = MagicMock()
    settings.state_dir = tmp_state_dir
    settings.debug = False
    return settings


def _setup_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    for subdir in ["connectors", "credentials", "skills", "workflows", "logs", "db"]:
        (state_dir / subdir).mkdir()
    return state_dir


class TestDataQuery:
    def test_executes_sql_on_cached_data(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        # Pre-populate DuckDB cache
        from meho_claude.core.data.cache import ResponseCache

        cache = ResponseCache(state_dir / "cache.duckdb")
        cache.cache_response("k8s", "pods", [
            {"id": 1, "name": "pod-1", "status": "Running"},
            {"id": 2, "name": "pod-2", "status": "Pending"},
        ])
        cache.close()

        with patch("meho_claude.cli.data._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["data", "query", "SELECT * FROM k8s_pods"])

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["row_count"] == 2

    def test_respects_limit_option(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        from meho_claude.core.data.cache import ResponseCache

        cache = ResponseCache(state_dir / "cache.duckdb")
        cache.cache_response("test", "items", [{"id": i} for i in range(50)])
        cache.close()

        with patch("meho_claude.cli.data._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(
                app, ["data", "query", "--limit", "5", "SELECT * FROM test_items"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["row_count"] <= 5

    def test_respects_offset_option(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        from meho_claude.core.data.cache import ResponseCache

        cache = ResponseCache(state_dir / "cache.duckdb")
        cache.cache_response("test", "items", [{"id": i, "name": f"item-{i}"} for i in range(20)])
        cache.close()

        with patch("meho_claude.cli.data._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(
                app, ["data", "query", "--limit", "5", "--offset", "3", "SELECT * FROM test_items"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        if data["rows"]:
            assert data["rows"][0]["id"] == 3

    def test_invalid_sql_returns_error(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with patch("meho_claude.cli.data._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["data", "query", "INVALID SQL STATEMENT"])

        # Should return error
        assert result.exit_code != 0 or "error" in result.output.lower()

    def test_query_on_empty_cache_returns_error(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with patch("meho_claude.cli.data._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(
                app, ["data", "query", "SELECT * FROM nonexistent_table"]
            )

        assert result.exit_code != 0 or "error" in result.output.lower()
