"""Tests for meho init CLI command."""

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


# ---- meho init Tests ----


class TestInitCommand:
    def test_init_returns_json_with_ok_status(self, tmp_path):
        """meho init returns JSON with status=ok."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [
            {"name": "meho-diagnose", "status": "created", "path": str(tmp_path / ".claude/skills/meho-diagnose/SKILL.md")},
            {"name": "meho-connect", "status": "created", "path": str(tmp_path / ".claude/skills/meho-connect/SKILL.md")},
            {"name": "meho-topology", "status": "created", "path": str(tmp_path / ".claude/skills/meho-topology/SKILL.md")},
            {"name": "meho-knowledge", "status": "created", "path": str(tmp_path / ".claude/skills/meho-knowledge/SKILL.md")},
            {"name": "meho-memory", "status": "created", "path": str(tmp_path / ".claude/skills/meho-memory/SKILL.md")},
        ]

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=mock_results),
            patch("meho_claude.cli.main._ensure_workflows", return_value=["diagnose.md", "health-check.md"]),
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=tmp_path / ".claude" / "skills"),
        ):
            result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_init_returns_skills_array(self, tmp_path):
        """meho init returns skills array in JSON."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        mock_results = [
            {"name": "meho-diagnose", "status": "created", "path": "/some/path"},
        ]

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=mock_results),
            patch("meho_claude.cli.main._ensure_workflows", return_value=[]),
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=tmp_path / ".claude" / "skills"),
        ):
            result = runner.invoke(app, ["init"])

        data = json.loads(result.output)
        assert "skills" in data
        assert isinstance(data["skills"], list)
        assert len(data["skills"]) == 1
        assert data["skills"][0]["name"] == "meho-diagnose"

    def test_init_returns_skills_dir(self, tmp_path):
        """meho init returns skills_dir path in JSON."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        skills_dir = tmp_path / ".claude" / "skills"

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=[]),
            patch("meho_claude.cli.main._ensure_workflows", return_value=[]),
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=skills_dir),
        ):
            result = runner.invoke(app, ["init"])

        data = json.loads(result.output)
        assert "skills_dir" in data
        assert str(skills_dir) in data["skills_dir"]

    def test_init_returns_workflows_installed(self, tmp_path):
        """meho init returns workflows_installed list in JSON."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=[]),
            patch("meho_claude.cli.main._ensure_workflows", return_value=["diagnose.md"]),
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=tmp_path / ".claude" / "skills"),
        ):
            result = runner.invoke(app, ["init"])

        data = json.loads(result.output)
        assert "workflows_installed" in data
        assert data["workflows_installed"] == ["diagnose.md"]

    def test_init_with_force_passes_force_true(self, tmp_path):
        """meho init --force passes force=True to write_meho_skills and ensure_bundled_workflows."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=[]) as mock_write,
            patch("meho_claude.cli.main._ensure_workflows", return_value=[]) as mock_workflows,
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=tmp_path / ".claude" / "skills"),
        ):
            result = runner.invoke(app, ["init", "--force"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Verify force=True was passed to both functions
        mock_write.assert_called_once()
        call_args = mock_write.call_args
        assert call_args[1].get("force") is True or (len(call_args[0]) > 1 and call_args[0][1] is True)

        mock_workflows.assert_called_once()
        wf_call_args = mock_workflows.call_args
        assert wf_call_args[1].get("force") is True or (len(wf_call_args[0]) > 1 and wf_call_args[0][1] is True)

    def test_init_has_duration_ms(self, tmp_path):
        """meho init output includes duration_ms."""
        state_dir = tmp_path / ".meho"
        state_dir.mkdir()

        with (
            patch("meho_claude.cli.main._get_init_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.main._write_skills", return_value=[]),
            patch("meho_claude.cli.main._ensure_workflows", return_value=[]),
            patch("meho_claude.cli.main._get_init_skills_dir", return_value=tmp_path / ".claude" / "skills"),
        ):
            result = runner.invoke(app, ["init"])

        data = json.loads(result.output)
        assert "duration_ms" in data


# ---- Subcommand registration Tests ----


class TestInitSubcommand:
    def test_init_appears_in_help(self):
        """init command appears in meho --help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output.lower()

    def test_init_help_exits_zero(self):
        """meho init --help exits with code 0."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0

    def test_init_help_shows_force_option(self):
        """meho init --help shows --force option."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "--force" in result.output
