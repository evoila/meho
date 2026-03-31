"""Tests for knowledge CLI commands: ingest, search, remove, rebuild, stats."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from meho_claude.cli import app

runner = CliRunner()


def _mock_settings(tmp_state_dir: Path):
    """Create a mock MehoSettings pointing to tmp state dir."""
    settings = MagicMock()
    settings.state_dir = tmp_state_dir
    settings.debug = False
    return settings


class TestKnowledgeIngest:
    def test_ingest_file_returns_chunk_count(self, tmp_path):
        """ingest <file> ingests file and returns chunk count."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        test_file = tmp_path / "doc.md"
        test_file.write_text("# Test\nSome content")

        mock_chunks = [MagicMock() for _ in range(3)]
        mock_store = MagicMock()
        mock_store.store_chunks.return_value = 3

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._ingest_file", return_value=mock_chunks),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "ingest", str(test_file), "--connector", "foo"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["chunk_count"] == 3
        assert data["connector"] == "foo"

    def test_ingest_without_connector_stores_global(self, tmp_path):
        """ingest <file> without --connector stores as global."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        test_file = tmp_path / "doc.md"
        test_file.write_text("# Test\nSome content")

        mock_chunks = [MagicMock() for _ in range(2)]
        mock_store = MagicMock()
        mock_store.store_chunks.return_value = 2

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._ingest_file", return_value=mock_chunks),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "ingest", str(test_file)])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        # store_chunks called with connector=None
        mock_store.store_chunks.assert_called_once()
        call_args = mock_store.store_chunks.call_args
        assert call_args[0][1] is None  # connector_name

    def test_ingest_nonexistent_file_returns_error(self, tmp_path):
        """ingest nonexistent file returns error."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        with patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["knowledge", "ingest", "/nonexistent/file.pdf"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "FILE_NOT_FOUND"


class TestKnowledgeSearch:
    def test_search_returns_results(self, tmp_path):
        """search "query" returns search results with relevance_score."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [
            {"id": "chunk-1", "content": "K8s troubleshooting", "relevance_score": 0.5},
            {"id": "chunk-2", "content": "VMware memory", "relevance_score": 0.3},
        ]

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._knowledge_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["knowledge", "search", "kubernetes pod"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["results"]) == 2

    def test_search_with_connector_filter(self, tmp_path):
        """search with --connector filters results."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [{"id": "chunk-1", "content": "K8s data", "relevance_score": 0.5}]

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._knowledge_search", return_value=mock_results) as mock_search,
        ):
            result = runner.invoke(app, ["knowledge", "search", "kubernetes", "--connector", "k8s"])

        assert result.exit_code == 0
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        # Verify connector was passed
        assert "k8s" in str(call_kwargs)


class TestKnowledgeRemove:
    def test_remove_existing_returns_ok(self, tmp_path):
        """remove <filename> removes source and returns confirmation."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.remove_source.return_value = True

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "remove", "doc.md"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "removed"

    def test_remove_nonexistent_returns_not_found(self, tmp_path):
        """remove nonexistent filename returns error with NOT_FOUND code."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.remove_source.return_value = False

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "remove", "nonexistent.md"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "NOT_FOUND"
        assert "suggestion" in data


class TestKnowledgeRebuild:
    def test_rebuild_returns_count(self, tmp_path):
        """rebuild re-embeds and returns count."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.rebuild.return_value = 15

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "rebuild"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["chunks_reindexed"] == 15


class TestKnowledgeStats:
    def test_stats_returns_counts(self, tmp_path):
        """stats returns source/chunk counts."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.get_stats.return_value = {
            "total_sources": 3,
            "total_chunks": 15,
            "by_connector": [
                {"connector": "k8s", "sources": 2, "chunks": 10},
                {"connector": "__global__", "sources": 1, "chunks": 5},
            ],
        }

        with (
            patch("meho_claude.cli.knowledge._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.knowledge._get_knowledge_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["knowledge", "stats"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["total_sources"] == 3
        assert data["total_chunks"] == 15
