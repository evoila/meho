# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Migrate knowledge module's alembic version tracking from the default
`alembic_version` table to the scoped `alembic_version_meho_knowledge` table.

This script must be run ONCE on existing deployments before running knowledge
module migrations after the env.py change. It is idempotent and safe to run
multiple times.

Usage:
    DATABASE_URL=postgresql://meho:password@localhost:5432/meho python scripts/migrate_knowledge_alembic_version.py

What it does:
    1. Creates `alembic_version_meho_knowledge` table if it doesn't exist
    2. Copies the current knowledge head revision from `alembic_version`
    3. Removes the knowledge-specific row from `alembic_version`
    4. Leaves rows belonging to other modules (e.g. agent) untouched

The knowledge module's known revisions are identified by their prefixes/IDs.
If the old table doesn't exist or has no matching rows, the script exits cleanly.
"""
import os
import sys
import sqlalchemy as sa

# Known knowledge module revision IDs (all revisions ever created for this module).
# These are the revision IDs from meho_app/modules/knowledge/alembic/versions/*.py
KNOWLEDGE_REVISIONS = {
    "20251211_0001",  # initial_schema
    "20260226_0002",  # voyage_ai_embeddings
    "20260228_0003",  # connector_scoped_knowledge
}

NEW_TABLE = "alembic_version_meho_knowledge"
OLD_TABLE = "alembic_version"


def migrate():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required.")
        sys.exit(1)

    # Convert async URL to sync if needed
    if "+asyncpg" in database_url:
        database_url = database_url.replace("+asyncpg", "")

    engine = sa.create_engine(database_url)

    with engine.begin() as conn:
        inspector = sa.inspect(conn)

        # Step 1: Create new table if it doesn't exist
        if not inspector.has_table(NEW_TABLE):
            conn.execute(sa.text(f"""
                CREATE TABLE {NEW_TABLE} (
                    version_num VARCHAR(32) NOT NULL,
                    CONSTRAINT {NEW_TABLE}_pkc PRIMARY KEY (version_num)
                )
            """))
            print(f"Created table: {NEW_TABLE}")
        else:
            print(f"Table {NEW_TABLE} already exists, skipping creation.")

        # Check if new table already has a version (idempotency)
        result = conn.execute(sa.text(f"SELECT version_num FROM {NEW_TABLE}"))
        existing_new = result.scalar_one_or_none()
        if existing_new:
            print(f"Table {NEW_TABLE} already has version '{existing_new}'. Nothing to migrate.")
            return

        # Step 2: Check if old table exists
        if not inspector.has_table(OLD_TABLE):
            print(f"Old table {OLD_TABLE} does not exist. Fresh installation, nothing to migrate.")
            return

        # Step 3: Find knowledge revision in old table
        result = conn.execute(sa.text(f"SELECT version_num FROM {OLD_TABLE}"))
        rows = result.fetchall()

        knowledge_version = None
        for row in rows:
            version = row[0]
            if version in KNOWLEDGE_REVISIONS:
                knowledge_version = version
                break

        if not knowledge_version:
            print(f"No knowledge revision found in {OLD_TABLE}. Nothing to migrate.")
            return

        # Step 4: Copy to new table
        conn.execute(sa.text(
            f"INSERT INTO {NEW_TABLE} (version_num) VALUES (:ver)"
        ), {"ver": knowledge_version})
        print(f"Copied version '{knowledge_version}' to {NEW_TABLE}.")

        # Step 5: Remove from old table
        conn.execute(sa.text(
            f"DELETE FROM {OLD_TABLE} WHERE version_num = :ver"
        ), {"ver": knowledge_version})
        print(f"Removed version '{knowledge_version}' from {OLD_TABLE}.")

    print("Migration complete.")


if __name__ == "__main__":
    migrate()
