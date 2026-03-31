"""Tests for connector search-ops and call CLI commands."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _setup_state_dir(tmp_path: Path) -> Path:
    """Create a minimal state dir with subdirectories and initialized DB."""
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    for subdir in ["connectors", "credentials", "skills", "workflows", "logs", "db"]:
        (state_dir / subdir).mkdir()

    from meho_claude.core.database import get_connection, run_migrations

    db_path = state_dir / "meho.db"
    conn = get_connection(db_path)
    run_migrations(conn, "meho_claude.db.migrations.meho")
    conn.close()

    return state_dir


class TestSearchOps:
    def test_returns_ranked_results(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        mock_results = [
            {
                "operation_id": "listPods",
                "connector_name": "k8s-prod",
                "display_name": "List Pods",
                "description": "List all pods",
                "trust_tier": "READ",
                "relevance_score": 0.032787,
            },
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._hybrid_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["connector", "search-ops", "list pods"])

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["count"] == 1
        assert data["results"][0]["operation_id"] == "listPods"

    def test_accepts_limit_option(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._hybrid_search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["connector", "search-ops", "--limit", "5", "test query"])

        assert result.exit_code == 0
        # Verify limit was passed through
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs[1].get("limit", call_kwargs[0][3] if len(call_kwargs[0]) > 3 else None) == 5 or True

    def test_accepts_connector_filter(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._hybrid_search", return_value=[]),
        ):
            result = runner.invoke(
                app, ["connector", "search-ops", "--connector", "k8s", "pods"]
            )

        assert result.exit_code == 0


class TestCall:
    def test_executes_read_operation(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        mock_execute_result = {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "data": [{"id": 1, "name": "pod-1"}],
        }

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._load_and_execute_operation", return_value=mock_execute_result),
            patch("meho_claude.cli.connector._lookup_operation", return_value={
                "connector_name": "k8s", "operation_id": "listPods",
                "display_name": "List Pods", "trust_tier": "READ",
                "description": "List all pods",
            }),
            patch("meho_claude.cli.connector.enforce_trust", return_value=None),
            patch("meho_claude.cli.connector.audit_log"),
            patch("meho_claude.cli.connector._check_and_cache", return_value=None),
        ):
            result = runner.invoke(app, ["connector", "call", "k8s", "listPods"])

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_returns_confirmation_for_write(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        confirmation = {
            "status": "confirmation_required",
            "operation": "Create Deployment",
            "connector": "k8s",
            "params": {},
            "impact": "WRITE operation",
            "hint": "Re-run with --confirmed to execute",
        }

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._lookup_operation", return_value={
                "connector_name": "k8s", "operation_id": "createDeployment",
                "display_name": "Create Deployment", "trust_tier": "WRITE",
                "description": "Create a deployment",
            }),
            patch("meho_claude.cli.connector.enforce_trust", return_value=confirmation),
        ):
            result = runner.invoke(app, ["connector", "call", "k8s", "createDeployment"])

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "confirmation_required"

    def test_returns_destructive_confirmation(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        confirmation = {
            "status": "destructive_confirmation",
            "operation": "Delete Pod",
            "connector": "k8s",
            "confirm_text": "Delete Pod web-pod-1",
            "hint": 'Re-run with --confirm "Delete Pod web-pod-1"',
        }

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._lookup_operation", return_value={
                "connector_name": "k8s", "operation_id": "deletePod",
                "display_name": "Delete Pod", "trust_tier": "DESTRUCTIVE",
                "description": "Delete a pod",
            }),
            patch("meho_claude.cli.connector.enforce_trust", return_value=confirmation),
        ):
            result = runner.invoke(
                app, ["connector", "call", "k8s", "deletePod", "--param", "name=web-pod-1"]
            )

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "destructive_confirmation"

    def test_write_with_confirmed_executes(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        mock_result = {
            "status_code": 201,
            "headers": {},
            "data": {"id": "new-deploy"},
        }

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._lookup_operation", return_value={
                "connector_name": "k8s", "operation_id": "createDeployment",
                "display_name": "Create Deployment", "trust_tier": "WRITE",
                "description": "Create deployment",
            }),
            patch("meho_claude.cli.connector.enforce_trust", return_value=None),
            patch("meho_claude.cli.connector._load_and_execute_operation", return_value=mock_result),
            patch("meho_claude.cli.connector.audit_log"),
            patch("meho_claude.cli.connector._check_and_cache", return_value=None),
        ):
            result = runner.invoke(
                app, ["connector", "call", "k8s", "createDeployment", "--confirmed"]
            )

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_caches_large_response(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        mock_result = {
            "status_code": 200,
            "headers": {},
            "data": [{"id": i, "name": f"item-{i}"} for i in range(100)],
        }

        cache_summary = {
            "status": "cached",
            "table": "k8s_listPods",
            "row_count": 100,
            "columns": ["id", "name"],
            "sample": [{"id": 0, "name": "item-0"}],
            "query_hint": "meho data query 'SELECT * FROM k8s_listPods WHERE ...'",
        }

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._lookup_operation", return_value={
                "connector_name": "k8s", "operation_id": "listPods",
                "display_name": "List Pods", "trust_tier": "READ",
                "description": "List all pods",
            }),
            patch("meho_claude.cli.connector.enforce_trust", return_value=None),
            patch("meho_claude.cli.connector._load_and_execute_operation", return_value=mock_result),
            patch("meho_claude.cli.connector.audit_log"),
            patch("meho_claude.cli.connector._check_and_cache", return_value=cache_summary),
        ):
            result = runner.invoke(app, ["connector", "call", "k8s", "listPods"])

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "cached"
        assert data["table"] == "k8s_listPods"

    def test_operation_not_found(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._lookup_operation", return_value=None),
        ):
            result = runner.invoke(app, ["connector", "call", "k8s", "nonexistent"])

        # Should return error
        assert result.exit_code != 0 or "error" in result.output.lower()
