# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke tests for the ``meho-dev`` Typer CLI (Goal #294 / #310).

These tests exercise *dispatch* only -- they verify Typer parses the command
table correctly, surfaces help output for every documented subcommand, and
calls the right helpers with the right docker compose args. They do not run
docker, alembic, uvicorn, or any subprocess; ``subprocess.run``,
``subprocess.Popen``, and ``signal.signal`` are mocked.

Subprocess behaviour parity with the old ``scripts/dev-env.sh`` is verified
manually (the issue lists the verification commands) and by the
``validate-install.sh`` smoke that runs ``meho-dev up`` end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from meho_app.tools.dev import app


@pytest.fixture
def runner() -> CliRunner:
    """Plain CliRunner -- mix_stderr defaults to True, sufficient for our checks."""
    return CliRunner()


@pytest.fixture
def mock_subprocess_run() -> Iterator[MagicMock]:
    """Patch every ``subprocess.run`` call inside ``meho_app.tools.dev``."""
    with patch("meho_app.tools.dev.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0, stdout="")
        yield m


class TestCommandSurface:
    """The CLI must keep parity with the old ``scripts/dev-env.sh`` surface."""

    def test_root_help_lists_every_subcommand(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in (
            "up",
            "down",
            "restart",
            "logs",
            "status",
            "validate",
            "test",
            "test-all",
            "local",
        ):
            assert cmd in result.stdout, f"missing subcommand in --help output: {cmd}"

    @pytest.mark.parametrize(
        "command",
        ["up", "down", "restart", "logs", "status", "validate", "test", "test-all", "local"],
    )
    def test_each_subcommand_has_help(self, runner: CliRunner, command: str) -> None:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, f"`meho-dev {command} --help` failed: {result.stdout}"


class TestDispatch:
    """Verify the right docker compose subcommand reaches subprocess.run."""

    def test_status_calls_docker_compose_ps(
        self,
        runner: CliRunner,
        mock_subprocess_run: MagicMock,
    ) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        ps_calls = [
            c
            for c in mock_subprocess_run.call_args_list
            if "ps" in c.args[0] and "docker" in c.args[0]
        ]
        assert ps_calls, "expected `docker compose ps` to be invoked"

    def test_logs_forwards_service_filters(
        self,
        runner: CliRunner,
        mock_subprocess_run: MagicMock,
    ) -> None:
        result = runner.invoke(app, ["logs", "meho", "postgres"])
        assert result.exit_code == 0
        logs_call = next(c for c in mock_subprocess_run.call_args_list if "logs" in c.args[0])
        argv = logs_call.args[0]
        assert "logs" in argv
        assert "-f" in argv
        assert "meho" in argv
        assert "postgres" in argv

    def test_down_forwards_extra_args_to_compose(
        self,
        runner: CliRunner,
        mock_subprocess_run: MagicMock,
    ) -> None:
        with patch("meho_app.tools.dev.time.sleep"):
            result = runner.invoke(app, ["down", "--volumes"])
        assert result.exit_code == 0
        down_call = next(
            c
            for c in mock_subprocess_run.call_args_list
            if "down" in c.args[0] and "docker" in c.args[0]
        )
        assert "--volumes" in down_call.args[0]


class TestEnvFileGate:
    """``up`` and ``local`` refuse to run without ``.env`` -- mirrors the bash check."""

    def test_up_aborts_when_env_missing(
        self,
        runner: CliRunner,
        mock_subprocess_run: MagicMock,
        tmp_path,
    ) -> None:
        with patch("meho_app.tools.dev.ENV_FILE", tmp_path / "missing.env"):
            result = runner.invoke(app, ["up"])
        assert result.exit_code == 1
        assert "Missing .env" in result.stdout
        assert not mock_subprocess_run.called, "compose must not run when .env is missing"

    def test_local_aborts_when_env_missing(
        self,
        runner: CliRunner,
        mock_subprocess_run: MagicMock,
        tmp_path,
    ) -> None:
        with patch("meho_app.tools.dev.ENV_FILE", tmp_path / "missing.env"):
            result = runner.invoke(app, ["local"])
        assert result.exit_code == 1
        assert "Missing .env" in result.stdout


class TestTeiProfileToggle:
    """``--profile tei`` must appear iff ``VOYAGE_API_KEY`` is unset everywhere."""

    def test_profile_added_when_voyage_key_absent(
        self,
        mock_subprocess_run: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with patch("meho_app.tools.dev.ENV_FILE", tmp_path / "missing.env"):
            from meho_app.tools.dev import _compose_args

            argv = _compose_args()
        assert "--profile" in argv
        assert "tei" in argv

    def test_profile_skipped_when_voyage_key_in_env(
        self,
        mock_subprocess_run: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "sk-test")
        with patch("meho_app.tools.dev.ENV_FILE", tmp_path / "missing.env"):
            from meho_app.tools.dev import _compose_args

            argv = _compose_args()
        assert "--profile" not in argv


class TestMigrationModeGuard:
    """``_run_migrations`` rejects unknown modes with exit 2 (parity with bash)."""

    def test_unknown_mode_exits_two(self) -> None:
        import typer

        from meho_app.tools.dev import _run_migrations

        with pytest.raises(typer.Exit) as excinfo:
            _run_migrations(mode="oops")
        assert excinfo.value.exit_code == 2
