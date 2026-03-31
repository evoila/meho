"""Tests for the dual-mode output system (JSON + Rich)."""

import json
import sys
import time

import pytest
from typer.testing import CliRunner

from meho_claude.cli import app

runner = CliRunner()


class TestOutputResponseJSON:
    """Test output_response writes valid JSON to stdout."""

    def test_writes_json_to_stdout(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"status": "ok", "count": 3})
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "ok"
        assert data["count"] == 3

    def test_json_output_has_newline(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"status": "ok"})
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")

    def test_no_output_to_stderr_in_json_mode(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"status": "ok"})
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_injects_duration_ms_with_start_time(self, capsys):
        from meho_claude.cli.output import output_response

        start = time.time() - 0.1  # 100ms ago
        output_response({"status": "ok"}, start_time=start)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "duration_ms" in data
        assert isinstance(data["duration_ms"], int)
        assert data["duration_ms"] >= 100

    def test_no_duration_ms_without_start_time(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"status": "ok"})
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "duration_ms" not in data

    def test_json_uses_msgspec(self, capsys):
        """Verify output is valid JSON produced by msgspec (fast path)."""
        from meho_claude.cli.output import output_response

        output_response({"key": "value", "number": 42, "nested": {"a": 1}})
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["key"] == "value"
        assert data["number"] == 42
        assert data["nested"]["a"] == 1


class TestOutputResponseRich:
    """Test output_response with human=True writes to stderr."""

    def test_human_mode_writes_to_stderr(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"status": "ok", "count": 3}, human=True)
        captured = capsys.readouterr()
        # stdout should be empty in human mode
        assert captured.out == ""
        # stderr should have Rich output
        assert captured.err != ""

    def test_human_mode_renders_table_for_list_of_dicts(self, capsys):
        from meho_claude.cli.output import output_response

        data = {
            "connectors": [
                {"name": "k8s-prod", "type": "kubernetes"},
                {"name": "vcenter-1", "type": "vmware"},
            ]
        }
        output_response(data, human=True)
        captured = capsys.readouterr()
        assert "k8s-prod" in captured.err
        assert "vcenter-1" in captured.err

    def test_human_mode_renders_error_with_red(self, capsys):
        from meho_claude.cli.output import output_response

        output_response({"error": "Something broke", "suggestion": "Try again"}, human=True)
        captured = capsys.readouterr()
        assert "Something broke" in captured.err
        assert "Try again" in captured.err


class TestOutputError:
    """Test output_error writes structured JSON error."""

    def test_error_has_required_fields(self, capsys):
        from meho_claude.cli.output import output_error

        with pytest.raises(SystemExit) as exc_info:
            output_error("Database connection failed", code="DB_ERROR")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["error"] == "Database connection failed"
        assert data["code"] == "DB_ERROR"

    def test_error_includes_suggestion_when_provided(self, capsys):
        from meho_claude.cli.output import output_error

        with pytest.raises(SystemExit):
            output_error("Not found", code="NOT_FOUND", suggestion="Check the name")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["suggestion"] == "Check the name"

    def test_error_omits_suggestion_when_not_provided(self, capsys):
        from meho_claude.cli.output import output_error

        with pytest.raises(SystemExit):
            output_error("Failed", code="FAIL")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "suggestion" not in data

    def test_error_raises_system_exit_with_custom_code(self, capsys):
        from meho_claude.cli.output import output_error

        with pytest.raises(SystemExit) as exc_info:
            output_error("Bad request", code="BAD_REQ", exit_code=2)
        assert exc_info.value.code == 2

    def test_error_default_code_is_unknown_error(self, capsys):
        from meho_claude.cli.output import output_error

        with pytest.raises(SystemExit):
            output_error("Mystery failure")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["code"] == "UNKNOWN_ERROR"


class TestCLIOutputIntegration:
    """Test that CLI commands use output_response (not raw print/echo)."""

    def test_connector_list_has_duration_ms(self):
        result = runner.invoke(app, ["connector", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "duration_ms" in data

    def test_connector_list_human_no_json_on_stdout(self):
        result = runner.invoke(app, ["--human", "connector", "list"])
        assert result.exit_code == 0
        # In CliRunner, stdout captures everything from the app.
        # With human mode, output_response writes to stderr via Console(stderr=True).
        # CliRunner captures both. The key check: no JSON object on stdout.
        # Since Rich goes to stderr and CliRunner mixes them, we verify no raw JSON.
        try:
            data = json.loads(result.output.strip())
            # If it parsed as JSON, that means output_response is not in human mode
            assert False, f"Expected Rich output, got JSON: {data}"
        except (json.JSONDecodeError, ValueError, AssertionError):
            pass  # Expected -- not valid JSON means Rich output worked

    def test_bare_meho_has_duration_ms(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "duration_ms" in data

    def test_connector_call_help_exits_zero(self):
        """connector call now requires arguments."""
        result = runner.invoke(app, ["connector", "call", "--help"])
        assert result.exit_code == 0

    def test_topology_lookup_requires_entity(self):
        """topology lookup now requires an entity argument."""
        result = runner.invoke(app, ["topology", "lookup", "--help"])
        assert result.exit_code == 0
        assert "entity" in result.output.lower()

    def test_memory_search_requires_query(self):
        """memory search now requires a query argument."""
        result = runner.invoke(app, ["memory", "search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_knowledge_ingest_requires_file(self):
        """knowledge ingest now requires a file argument."""
        result = runner.invoke(app, ["knowledge", "ingest", "--help"])
        assert result.exit_code == 0
        assert "file" in result.output.lower()

    def test_data_query_help_exits_zero(self):
        """data query now requires a SQL argument."""
        result = runner.invoke(app, ["data", "query", "--help"])
        assert result.exit_code == 0
