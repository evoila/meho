"""State directory initialization and status summary."""

from pathlib import Path

_SUBDIRS = [
    "connectors",       # YAML connector configs
    "credentials",      # Fernet-encrypted credential files
    "skills",           # Generated and custom skill markdown
    "workflows",        # Workflow template markdown
    "logs",             # Structured log files
    "db",               # Reserved for future use (cache, etc.)
]


def ensure_state_dir(state_dir: Path) -> None:
    """Create ~/.meho/ and all subdirectories. Idempotent."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for subdir in _SUBDIRS:
        (state_dir / subdir).mkdir(mode=0o700, exist_ok=True)


def get_status_summary(state_dir: Path | None = None) -> dict:
    """Quick status for bare `meho` command (like `git status`)."""
    if state_dir is None:
        state_dir = Path.home() / ".meho"

    if not state_dir.exists():
        return {
            "status": "not_initialized",
            "message": "Run any meho command to initialize ~/.meho/",
            "state_dir": str(state_dir),
        }

    # Count connectors
    connectors_dir = state_dir / "connectors"
    connector_count = len(list(connectors_dir.glob("*.yaml"))) if connectors_dir.exists() else 0

    # Count topology entities (if DB exists)
    entity_count = 0
    topology_db = state_dir / "topology.db"
    if topology_db.exists():
        import sqlite3

        try:
            conn = sqlite3.connect(str(topology_db))
            row = conn.execute("SELECT COUNT(*) FROM topology_entities").fetchone()
            entity_count = row[0] if row else 0
            conn.close()
        except Exception:
            pass  # Table may not exist yet

    return {
        "status": "ok",
        "state_dir": str(state_dir),
        "connectors": connector_count,
        "topology_entities": entity_count,
    }
