"""Tests for memory CLI commands: store, search, list, forget."""

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


class TestMemoryStore:
    def test_store_with_connector_returns_id(self, tmp_path):
        """store "text" --connector foo stores memory and returns id."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.store_memory.return_value = {
            "id": "mem-123",
            "content": "OOM issue resolved",
            "connector_name": "k8s",
            "tags": "",
            "created_at": "2026-03-03 12:00:00",
        }

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "store", "OOM issue resolved", "--connector", "k8s"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["memory"]["id"] == "mem-123"

    def test_store_without_connector_stores_global(self, tmp_path):
        """store "text" without --connector stores as global."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.store_memory.return_value = {
            "id": "mem-456",
            "content": "Global memory",
            "connector_name": None,
            "tags": "",
            "created_at": "2026-03-03 12:00:00",
        }

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "store", "Global memory"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        # store_memory called with connector_name=None
        mock_store.store_memory.assert_called_once_with("Global memory", connector_name=None, tags="")

    def test_store_with_tags(self, tmp_path):
        """store "text" --tags "pattern,resolution" stores with tags."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.store_memory.return_value = {
            "id": "mem-789",
            "content": "Some issue",
            "connector_name": None,
            "tags": "pattern,resolution",
            "created_at": "2026-03-03 12:00:00",
        }

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "store", "Some issue", "--tags", "pattern,resolution"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_store.store_memory.assert_called_once_with("Some issue", connector_name=None, tags="pattern,resolution")


class TestMemorySearch:
    def test_search_returns_results(self, tmp_path):
        """search "query" returns search results."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [
            {"id": "mem-1", "content": "OOM kill pattern", "relevance_score": 0.5},
        ]

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._memory_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["memory", "search", "OOM kill"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["results"]) == 1

    def test_search_with_connector_filter(self, tmp_path):
        """search with --connector filters by connector."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [{"id": "mem-1", "content": "K8s issue", "relevance_score": 0.5}]

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._memory_search", return_value=mock_results) as mock_search,
        ):
            result = runner.invoke(app, ["memory", "search", "kubernetes", "--connector", "k8s"])

        assert result.exit_code == 0
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert "k8s" in str(call_kwargs)


class TestMemoryList:
    def test_list_returns_all_memories(self, tmp_path):
        """list returns all memories."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.list_memories.return_value = [
            {"id": "mem-1", "content": "Memory one", "connector_name": "k8s",
             "tags": "", "created_at": "2026-03-03 12:00:00"},
            {"id": "mem-2", "content": "Memory two", "connector_name": None,
             "tags": "tag1", "created_at": "2026-03-03 11:00:00"},
        ]

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["memories"]) == 2

    def test_list_with_connector_filter(self, tmp_path):
        """list --connector foo filters by connector."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.list_memories.return_value = [
            {"id": "mem-1", "content": "K8s only", "connector_name": "k8s",
             "tags": "", "created_at": "2026-03-03 12:00:00"},
        ]

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "list", "--connector", "k8s"])

        assert result.exit_code == 0
        mock_store.list_memories.assert_called_once_with(connector_name="k8s")


class TestMemoryForget:
    def test_forget_existing_returns_ok(self, tmp_path):
        """forget <id> removes memory and returns confirmation."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.forget_memory.return_value = True

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "forget", "mem-123"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["forgotten"] is True

    def test_forget_nonexistent_returns_error(self, tmp_path):
        """forget nonexistent-id returns error."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_store = MagicMock()
        mock_store.forget_memory.return_value = False

        with (
            patch("meho_claude.cli.memory._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.memory._get_memory_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["memory", "forget", "nonexistent-id"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "NOT_FOUND"
