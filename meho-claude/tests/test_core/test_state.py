"""Tests for state directory initialization and status summary."""

from pathlib import Path

from meho_claude.core.state import ensure_state_dir, get_status_summary


class TestEnsureStateDir:
    """Test ~/.meho/ directory creation."""

    def test_creates_state_dir(self, tmp_path: Path):
        state_dir = tmp_path / ".meho"
        ensure_state_dir(state_dir)
        assert state_dir.exists()
        assert state_dir.is_dir()

    def test_creates_all_subdirs(self, tmp_path: Path):
        state_dir = tmp_path / ".meho"
        ensure_state_dir(state_dir)
        expected = ["connectors", "credentials", "skills", "workflows", "logs", "db"]
        for subdir in expected:
            assert (state_dir / subdir).exists(), f"Missing subdir: {subdir}"
            assert (state_dir / subdir).is_dir()

    def test_idempotent(self, tmp_path: Path):
        state_dir = tmp_path / ".meho"
        ensure_state_dir(state_dir)
        ensure_state_dir(state_dir)  # Second call should not error
        assert state_dir.exists()

    def test_state_dir_permissions(self, tmp_path: Path):
        state_dir = tmp_path / ".meho"
        ensure_state_dir(state_dir)
        # Check that state dir has restricted permissions
        mode = state_dir.stat().st_mode & 0o777
        assert mode == 0o700


class TestGetStatusSummary:
    """Test status summary generation."""

    def test_returns_dict(self, tmp_state_dir: Path):
        result = get_status_summary(tmp_state_dir)
        assert isinstance(result, dict)

    def test_has_status_field(self, tmp_state_dir: Path):
        result = get_status_summary(tmp_state_dir)
        assert "status" in result

    def test_has_connector_count(self, tmp_state_dir: Path):
        result = get_status_summary(tmp_state_dir)
        assert "connectors" in result
        assert result["connectors"] == 0

    def test_has_entity_count(self, tmp_state_dir: Path):
        result = get_status_summary(tmp_state_dir)
        assert "topology_entities" in result
        assert result["topology_entities"] == 0

    def test_not_initialized_returns_status(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        result = get_status_summary(nonexistent)
        assert result["status"] == "not_initialized"
