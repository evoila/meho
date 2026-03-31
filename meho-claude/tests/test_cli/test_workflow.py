"""Tests for workflow CLI commands: list and run."""

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


# ---- workflow list Tests ----


class TestWorkflowList:
    def test_list_returns_json_with_workflows(self, tmp_path):
        """workflow list returns JSON with status=ok, workflows array, count."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        mock_workflows = [
            {
                "name": "diagnose",
                "description": "Cross-system diagnostic investigation",
                "budget": 15,
                "version": "1.0",
                "source_path": str(state_dir / "workflows" / "diagnose.md"),
                "has_frontmatter": True,
                "tags": [],
            },
            {
                "name": "health-check",
                "description": "Multi-system health overview",
                "budget": 10,
                "version": "1.0",
                "source_path": str(state_dir / "workflows" / "health-check.md"),
                "has_frontmatter": True,
                "tags": [],
            },
        ]

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._list_workflows", return_value=mock_workflows),
        ):
            result = runner.invoke(app, ["workflow", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["workflows"]) == 2
        assert data["count"] == 2
        assert data["workflows"][0]["name"] == "diagnose"

    def test_list_auto_installs_bundled_on_empty(self, tmp_path):
        """workflow list auto-installs bundled templates if workflows dir is empty."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        workflows_dir = state_dir / "workflows"
        workflows_dir.mkdir()

        # Use real _list_workflows which calls ensure_bundled_workflows
        with patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["workflow", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        # Bundled templates should have been auto-installed
        assert data["count"] >= 3  # diagnose, health-check, compare (not _template)

    def test_list_empty_returns_empty_array(self, tmp_path):
        """workflow list returns empty array when no templates."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._list_workflows", return_value=[]),
        ):
            result = runner.invoke(app, ["workflow", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["workflows"] == []
        assert data["count"] == 0

    def test_list_json_has_duration_ms(self, tmp_path):
        """workflow list output includes duration_ms."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._list_workflows", return_value=[]),
        ):
            result = runner.invoke(app, ["workflow", "list"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "duration_ms" in data


# ---- workflow run Tests ----


class TestWorkflowRun:
    def test_run_valid_name_returns_template(self, tmp_path):
        """workflow run diagnose returns JSON with status=ok, workflow, template."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        template_content = "---\nname: diagnose\n---\n\n# Diagnosis Workflow\n\nSteps here."

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._load_workflow", return_value=template_content),
        ):
            result = runner.invoke(app, ["workflow", "run", "diagnose"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["workflow"] == "diagnose"
        assert "# Diagnosis Workflow" in data["template"]

    def test_run_unknown_name_returns_error(self, tmp_path):
        """workflow run unknown-name returns WORKFLOW_NOT_FOUND error."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._load_workflow", return_value=None),
        ):
            result = runner.invoke(app, ["workflow", "run", "nonexistent"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "WORKFLOW_NOT_FOUND"
        assert "suggestion" in data

    def test_run_json_has_duration_ms(self, tmp_path):
        """workflow run output includes duration_ms."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        (state_dir / "workflows").mkdir()

        with (
            patch("meho_claude.cli.workflow._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.workflow._load_workflow", return_value="# Workflow content"),
        ):
            result = runner.invoke(app, ["workflow", "run", "test"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "duration_ms" in data


# ---- Subcommand registration Tests ----


class TestWorkflowSubcommand:
    def test_workflow_appears_in_help(self):
        """workflow subcommand appears in meho --help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "workflow" in result.output.lower()

    def test_workflow_help_exits_zero(self):
        """workflow --help exits with code 0."""
        result = runner.invoke(app, ["workflow", "--help"])
        assert result.exit_code == 0

    def test_workflow_list_appears_in_workflow_help(self):
        """list command appears in workflow --help."""
        result = runner.invoke(app, ["workflow", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output.lower()

    def test_workflow_run_appears_in_workflow_help(self):
        """run command appears in workflow --help."""
        result = runner.invoke(app, ["workflow", "--help"])
        assert result.exit_code == 0
        assert "run" in result.output.lower()
