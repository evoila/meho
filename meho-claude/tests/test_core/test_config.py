"""Tests for Pydantic Settings configuration."""

import os
from pathlib import Path

from meho_claude.core.config import MehoSettings


class TestMehoSettingsDefaults:
    """Test default configuration values."""

    def test_default_state_dir(self):
        settings = MehoSettings()
        assert settings.state_dir == Path.home() / ".meho"

    def test_default_timeout(self):
        settings = MehoSettings()
        assert settings.default_timeout == 30

    def test_default_log_level(self):
        settings = MehoSettings()
        assert settings.log_level == "WARNING"

    def test_default_debug(self):
        settings = MehoSettings()
        assert settings.debug is False


class TestMehoSettingsEnvOverride:
    """Test MEHO_* environment variable overrides."""

    def test_state_dir_override(self, tmp_path: Path, monkeypatch):
        custom = str(tmp_path / "custom_meho")
        monkeypatch.setenv("MEHO_STATE_DIR", custom)
        settings = MehoSettings()
        assert settings.state_dir == Path(custom)

    def test_debug_override(self, monkeypatch):
        monkeypatch.setenv("MEHO_DEBUG", "true")
        settings = MehoSettings()
        assert settings.debug is True

    def test_log_level_override(self, monkeypatch):
        monkeypatch.setenv("MEHO_LOG_LEVEL", "DEBUG")
        settings = MehoSettings()
        assert settings.log_level == "DEBUG"
