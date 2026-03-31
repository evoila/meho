"""SQLite connection with PRAGMAs and migration runner."""

import sqlite3
from importlib import resources
from pathlib import Path

# Performance + consistency PRAGMAs applied on every connection
_CONNECTION_PRAGMAS = [
    "PRAGMA journal_mode = WAL",      # Concurrent readers + single writer
    "PRAGMA synchronous = NORMAL",    # Safe with WAL, reduces fsync overhead
    "PRAGMA foreign_keys = ON",       # Enforce referential integrity
    "PRAGMA busy_timeout = 5000",     # Wait 5s on lock instead of failing
    "PRAGMA cache_size = -8000",      # 8MB page cache (negative = KiB)
    "PRAGMA temp_store = MEMORY",     # Keep temp tables in memory
]


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with correct PRAGMAs."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for pragma in _CONNECTION_PRAGMAS:
        conn.execute(pragma)
    return conn


def run_migrations(conn: sqlite3.Connection, migrations_package: str) -> int:
    """Apply pending migrations using PRAGMA user_version.

    migrations_package: dotted path to migrations directory,
        e.g., "meho_claude.db.migrations.meho"

    Returns the number of migrations applied.
    """
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]

    # Load migration SQL files sorted by number
    migration_files = sorted(
        resources.files(migrations_package).iterdir(),
        key=lambda f: f.name,
    )

    applied = 0
    for migration_file in migration_files:
        if not migration_file.name.endswith(".sql"):
            continue
        # Extract version number from filename: "001_initial.sql" -> 1
        file_version = int(migration_file.name.split("_")[0])
        if file_version <= current_version:
            continue

        sql = migration_file.read_text()
        try:
            conn.executescript(sql)
            applied += 1
        except Exception:
            raise

    return applied


def initialize_databases(state_dir: Path) -> None:
    """Initialize both meho.db and topology.db with migrations."""
    meho_db = state_dir / "meho.db"
    topology_db = state_dir / "topology.db"

    conn = get_connection(meho_db)
    run_migrations(conn, "meho_claude.db.migrations.meho")
    conn.execute("PRAGMA optimize")
    conn.close()

    conn = get_connection(topology_db)
    run_migrations(conn, "meho_claude.db.migrations.topology")
    conn.execute("PRAGMA optimize")
    conn.close()
