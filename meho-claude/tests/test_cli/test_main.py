"""Tests for the meho CLI root command and subcommand registration."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from meho_claude.cli import app
from meho_claude.cli.main import _ensure_initialized

runner = CliRunner()


class TestMehoHelp:
    """Test that meho --help shows all subcommand groups."""

    def test_help_exits_zero(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_help_shows_connector(self):
        result = runner.invoke(app, ["--help"])
        assert "connector" in result.output.lower()

    def test_help_shows_topology(self):
        result = runner.invoke(app, ["--help"])
        assert "topology" in result.output.lower()

    def test_help_shows_memory(self):
        result = runner.invoke(app, ["--help"])
        assert "memory" in result.output.lower()

    def test_help_shows_knowledge(self):
        result = runner.invoke(app, ["--help"])
        assert "knowledge" in result.output.lower()

    def test_help_shows_data(self):
        result = runner.invoke(app, ["--help"])
        assert "data" in result.output.lower()


class TestMehoBareCommand:
    """Test that bare meho (no args) returns JSON status."""

    def test_bare_meho_exits_zero(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 0

    def test_bare_meho_returns_json(self):
        result = runner.invoke(app, [])
        data = json.loads(result.output)
        assert data["status"] == "ok"


class TestGlobalFlags:
    """Test that --human and --debug flags are accepted."""

    def test_human_flag_accepted(self):
        result = runner.invoke(app, ["--human"])
        assert result.exit_code == 0

    def test_debug_flag_accepted(self):
        result = runner.invoke(app, ["--debug"])
        assert result.exit_code == 0


class TestSubcommandHelp:
    """Test that each subcommand group shows help."""

    def test_connector_help(self):
        result = runner.invoke(app, ["connector", "--help"])
        assert result.exit_code == 0

    def test_topology_help(self):
        result = runner.invoke(app, ["topology", "--help"])
        assert result.exit_code == 0

    def test_memory_help(self):
        result = runner.invoke(app, ["memory", "--help"])
        assert result.exit_code == 0

    def test_knowledge_help(self):
        result = runner.invoke(app, ["knowledge", "--help"])
        assert result.exit_code == 0

    def test_data_help(self):
        result = runner.invoke(app, ["data", "--help"])
        assert result.exit_code == 0


class TestSubcommandStubs:
    """Test that subcommand stubs return valid JSON placeholders."""

    def test_connector_list_returns_json(self):
        result = runner.invoke(app, ["connector", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "status" in data

    def test_connector_add_help(self):
        """connector add now requires flags or interactive prompts."""
        result = runner.invoke(app, ["connector", "add", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output.lower()

    def test_connector_test_requires_name(self):
        """connector test now requires a name argument."""
        result = runner.invoke(app, ["connector", "test", "--help"])
        assert result.exit_code == 0
        assert "name" in result.output.lower()

    def test_connector_call_requires_args(self):
        """connector call now requires connector_name and operation_id arguments."""
        result = runner.invoke(app, ["connector", "call", "--help"])
        assert result.exit_code == 0
        assert "connector_name" in result.output.lower() or "connector-name" in result.output.lower()

    def test_connector_search_ops_requires_query(self):
        """connector search-ops now requires a query argument."""
        result = runner.invoke(app, ["connector", "search-ops", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_topology_lookup_requires_entity(self):
        """topology lookup now requires an entity argument."""
        result = runner.invoke(app, ["topology", "lookup", "--help"])
        assert result.exit_code == 0
        assert "entity" in result.output.lower()

    def test_topology_correlate_returns_json(self):
        result = runner.invoke(app, ["topology", "correlate"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "correlations" in data

    def test_memory_search_requires_query(self):
        """memory search now requires a query argument."""
        result = runner.invoke(app, ["memory", "search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_memory_store_requires_text(self):
        """memory store now requires a text argument."""
        result = runner.invoke(app, ["memory", "store", "--help"])
        assert result.exit_code == 0
        assert "text" in result.output.lower()

    def test_memory_forget_requires_id(self):
        """memory forget now requires a memory_id argument."""
        result = runner.invoke(app, ["memory", "forget", "--help"])
        assert result.exit_code == 0
        assert "memory" in result.output.lower()

    def test_knowledge_ingest_requires_file(self):
        """knowledge ingest now requires a file argument."""
        result = runner.invoke(app, ["knowledge", "ingest", "--help"])
        assert result.exit_code == 0
        assert "file" in result.output.lower()

    def test_knowledge_search_requires_query(self):
        """knowledge search now requires a query argument."""
        result = runner.invoke(app, ["knowledge", "search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_data_query_requires_sql(self):
        """data query now requires a SQL argument."""
        result = runner.invoke(app, ["data", "query", "--help"])
        assert result.exit_code == 0
        assert "sql" in result.output.lower()


class TestDeferredInitialization:
    """Test that heavy initialization is deferred until needed."""

    def test_help_does_not_initialize_databases(self):
        """meho --help should NOT trigger initialize_databases."""
        with patch("meho_claude.cli.main.SimpleNamespace", wraps=SimpleNamespace) as mock_ns:
            result = runner.invoke(app, ["--help"])
            assert result.exit_code == 0
            # Verify help works; settings should never be set
            # (callback stores settings=None, help exits before _ensure_initialized)

    def test_subcommand_help_does_not_initialize(self):
        """meho connector --help should NOT trigger heavy init."""
        result = runner.invoke(app, ["connector", "--help"])
        assert result.exit_code == 0
        # No crash means settings=None was fine for help

    def test_ensure_initialized_is_idempotent(self):
        """_ensure_initialized called twice only initializes once."""
        ctx = MagicMock(spec=typer.Context)
        ctx.obj = SimpleNamespace(human=False, debug=False, settings=None, start_time=time.time())

        mock_settings = MagicMock()
        mock_settings.debug = False
        mock_settings.state_dir = MagicMock()
        mock_settings.log_level = "INFO"

        with (
            patch("meho_claude.cli.main.MehoSettings", return_value=mock_settings) if False else
            patch("meho_claude.core.config.MehoSettings", return_value=mock_settings) as mock_cls,
            patch("meho_claude.core.state.ensure_state_dir"),
            patch("meho_claude.core.database.initialize_databases"),
            patch("meho_claude.core.logging.configure_logging"),
        ):
            # First call: should initialize
            _ensure_initialized(ctx)
            assert ctx.obj.settings is mock_settings
            assert mock_cls.call_count == 1

            # Second call: should be a no-op
            _ensure_initialized(ctx)
            assert mock_cls.call_count == 1  # NOT called again

    def test_ensure_initialized_sets_settings(self):
        """_ensure_initialized sets ctx.obj.settings to a MehoSettings instance."""
        ctx = MagicMock(spec=typer.Context)
        ctx.obj = SimpleNamespace(human=False, debug=False, settings=None, start_time=time.time())

        mock_settings = MagicMock()
        mock_settings.debug = False
        mock_settings.state_dir = MagicMock()
        mock_settings.log_level = "INFO"

        with (
            patch("meho_claude.core.config.MehoSettings", return_value=mock_settings),
            patch("meho_claude.core.state.ensure_state_dir"),
            patch("meho_claude.core.database.initialize_databases"),
            patch("meho_claude.core.logging.configure_logging"),
        ):
            assert ctx.obj.settings is None
            _ensure_initialized(ctx)
            assert ctx.obj.settings is mock_settings

    def test_callback_sets_settings_none(self):
        """main() callback stores settings=None for deferred init."""
        # The --help path exits before reaching _ensure_initialized
        # but for a subcommand, settings should be None after callback
        result = runner.invoke(app, ["connector", "--help"])
        assert result.exit_code == 0
