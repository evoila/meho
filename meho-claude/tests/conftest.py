"""Shared test fixtures for meho-claude tests."""

import pytest
from pathlib import Path


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Create a temporary ~/.meho/ equivalent for testing."""
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    for subdir in ["connectors", "credentials", "skills", "workflows", "logs", "db"]:
        (state_dir / subdir).mkdir()
    return state_dir


@pytest.fixture
def meho_db(tmp_state_dir: Path):
    """Initialize a test meho.db with migrations applied."""
    from meho_claude.core.database import get_connection, run_migrations

    db_path = tmp_state_dir / "meho.db"
    conn = get_connection(db_path)
    run_migrations(conn, "meho_claude.db.migrations.meho")
    yield conn
    conn.close()


@pytest.fixture
def topology_db(tmp_state_dir: Path):
    """Initialize a test topology.db with migrations applied."""
    from meho_claude.core.database import get_connection, run_migrations

    db_path = tmp_state_dir / "topology.db"
    conn = get_connection(db_path)
    run_migrations(conn, "meho_claude.db.migrations.topology")
    yield conn
    conn.close()
