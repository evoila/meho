"""Tests for SQLite database initialization and migration runner."""

import sqlite3
from pathlib import Path

from meho_claude.core.database import get_connection, run_migrations, initialize_databases


class TestGetConnection:
    """Test SQLite connection creation with PRAGMAs."""

    def test_returns_connection(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_wal_journal_mode(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()

    def test_row_factory_is_row(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        assert conn.row_factory == sqlite3.Row
        conn.close()


class TestRunMigrations:
    """Test PRAGMA user_version based migration runner."""

    def test_applies_meho_migration(self, meho_db):
        version = meho_db.execute("PRAGMA user_version").fetchone()[0]
        assert version == 4  # 001_initial + 002_operations + 003_knowledge + 004_memory

    def test_meho_has_connectors_table(self, meho_db):
        cursor = meho_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='connectors'"
        )
        assert cursor.fetchone() is not None

    def test_meho_has_schema_info_table(self, meho_db):
        cursor = meho_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'"
        )
        assert cursor.fetchone() is not None

    def test_topology_has_entities_table(self, topology_db):
        cursor = topology_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topology_entities'"
        )
        assert cursor.fetchone() is not None

    def test_topology_has_relationships_table(self, topology_db):
        cursor = topology_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topology_relationships'"
        )
        assert cursor.fetchone() is not None

    def test_topology_has_correlations_table(self, topology_db):
        cursor = topology_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topology_correlations'"
        )
        assert cursor.fetchone() is not None

    def test_migration_idempotent(self, tmp_state_dir: Path):
        """Running migrations twice should apply 0 the second time."""
        from meho_claude.core.database import get_connection, run_migrations

        db_path = tmp_state_dir / "test_idempotent.db"
        conn = get_connection(db_path)
        first_count = run_migrations(conn, "meho_claude.db.migrations.meho")
        assert first_count == 4  # 001_initial + 002_operations + 003_knowledge + 004_memory
        second_count = run_migrations(conn, "meho_claude.db.migrations.meho")
        assert second_count == 0
        conn.close()


class TestInitializeDatabases:
    """Test full database initialization."""

    def test_creates_both_databases(self, tmp_state_dir: Path):
        initialize_databases(tmp_state_dir)
        assert (tmp_state_dir / "meho.db").exists()
        assert (tmp_state_dir / "topology.db").exists()

    def test_meho_db_has_correct_tables(self, tmp_state_dir: Path):
        initialize_databases(tmp_state_dir)
        conn = sqlite3.connect(str(tmp_state_dir / "meho.db"))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "connectors" in tables
        assert "schema_info" in tables
        conn.close()

    def test_topology_db_has_correct_tables(self, tmp_state_dir: Path):
        initialize_databases(tmp_state_dir)
        conn = sqlite3.connect(str(tmp_state_dir / "topology.db"))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "topology_entities" in tables
        assert "topology_relationships" in tables
        assert "topology_correlations" in tables
        conn.close()
