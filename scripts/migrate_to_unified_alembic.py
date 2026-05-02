#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Rescue script: migrate a legacy multi-tree Alembic deployment to the unified tree.

Goal #294 / Issue #299 collapsed nine per-module Alembic trees into a single
``meho_app/alembic/`` history. Existing deployments running the legacy layout
(nine ``alembic_version_meho_*`` tables) MUST run this script *before* the
first ``alembic upgrade head`` against the new unified config; otherwise the
init migration will see existing tables and fail.

What it does
------------

1. Connect to the database using ``DATABASE_URL``.
2. Verify the unified tree has not already been applied. If
   ``alembic_version`` already contains ``0009_doc_family`` we exit 0 with a
   "nothing to do" message — re-runs are safe.
3. Verify each legacy ``alembic_version_meho_*`` table exists and contains the
   exact head revision the consolidated history was generated from. Any
   missing table or unexpected revision aborts the script with a precise
   error and zero side-effects.
4. In a single transaction:

   * Create the new ``alembic_version`` table (matching Alembic's own DDL).
   * Stamp it with ``0009_doc_family``.
   * Drop the nine legacy ``alembic_version_meho_*`` tables.

The DDL of the actual schema is unchanged — this script only reshapes the
Alembic bookkeeping. It is intentionally *not* invoked from the container
entrypoint or from any startup hook; operators run it explicitly during
deployment, document the action in their change log, then ``alembic upgrade
head`` will be a no-op.

Usage
-----

::

    DATABASE_URL=postgresql://meho:password@localhost:5432/meho \\
        uv run python scripts/migrate_to_unified_alembic.py

Use ``--dry-run`` to print the planned actions without modifying anything.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

import psycopg2
from psycopg2 import sql

if TYPE_CHECKING:
    from psycopg2.extensions import connection as PgConnection

LOG = logging.getLogger("migrate_to_unified_alembic")

# Final consolidated head -- bump this in lockstep with the latest revision
# under meho_app/alembic/versions/.
UNIFIED_HEAD_REVISION = "0009_doc_family"

# Each entry maps the legacy version_table name to the head revision that
# the corresponding module's Alembic chain ended on. The script REJECTS any
# DB whose legacy bookkeeping is at a different revision -- you must bring
# it to head with the legacy tooling before running this rescue script.
LEGACY_TREES: dict[str, str] = {
    "alembic_version_meho_topology": "squash_001",
    "alembic_version_meho_openapi": "conn_0015_webhook_secret",  # connectors module
    "alembic_version_meho_knowledge": "0005",
    "alembic_version_meho_memory": "squash_001",
    "alembic_version_meho_agent": "squash_001",
    "alembic_version_meho_ingestion": "squash_001",
    "alembic_version_meho_scheduled_tasks": "squash_001",
    "alembic_version_meho_orchestrator_skills": "squash_001",
    "alembic_version_meho_audit": "squash_001",
}


class RescueError(Exception):
    """Raised when the database is not in a state we can safely transition."""


def _normalize_dsn(database_url: str) -> str:
    """Strip the optional ``+asyncpg`` driver hint -- psycopg2 wants plain DSNs."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _connect(database_url: str) -> PgConnection:
    LOG.info("connecting to database (driver=psycopg2)")
    conn = psycopg2.connect(_normalize_dsn(database_url))
    conn.autocommit = False
    return conn


def _table_exists(cur: psycopg2.extensions.cursor, table_name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (table_name,),
    )
    row = cur.fetchone()
    return bool(row[0]) if row else False


def _read_unified_revision(cur: psycopg2.extensions.cursor) -> str | None:
    if not _table_exists(cur, "alembic_version"):
        return None
    cur.execute("SELECT version_num FROM alembic_version")
    row = cur.fetchone()
    return str(row[0]) if row else None


def _read_legacy_revision(cur: psycopg2.extensions.cursor, table: str) -> str | None:
    if not _table_exists(cur, table):
        return None
    cur.execute(sql.SQL("SELECT version_num FROM {}").format(sql.Identifier(table)))
    row = cur.fetchone()
    return str(row[0]) if row else None


def verify_legacy_state(conn: PgConnection) -> None:
    """Confirm the database is in the expected legacy multi-tree state.

    Fails loud if even one table is missing or has an unexpected revision.
    Never modifies the database.
    """
    with conn.cursor() as cur:
        unified = _read_unified_revision(cur)
        if unified == UNIFIED_HEAD_REVISION:
            raise RescueError(
                f"alembic_version already contains '{UNIFIED_HEAD_REVISION}'. "
                "The database has already been migrated; nothing to do."
            )
        if unified is not None:
            raise RescueError(
                f"alembic_version exists but contains unexpected revision "
                f"'{unified}'. Expected either no row (legacy state) or "
                f"'{UNIFIED_HEAD_REVISION}' (already migrated). Manual "
                "intervention required."
            )

        missing: list[str] = []
        wrong: list[tuple[str, str, str]] = []
        for table, expected in LEGACY_TREES.items():
            actual = _read_legacy_revision(cur, table)
            if actual is None:
                missing.append(table)
            elif actual != expected:
                wrong.append((table, expected, actual))

        if missing:
            raise RescueError(
                "Cannot migrate: the following legacy version tables are "
                f"missing: {', '.join(sorted(missing))}. This script only "
                "rescues deployments that ran the pre-#299 multi-tree "
                "Alembic layout. Fresh installs should run the new "
                "`alembic -c meho_app/alembic.ini upgrade head` directly "
                "against an empty database."
            )
        if wrong:
            details = "\n".join(
                f"  - {table}: expected '{exp}', found '{act}'"
                for table, exp, act in wrong
            )
            raise RescueError(
                "Cannot migrate: legacy version tables are at unexpected "
                f"revisions:\n{details}\n\n"
                "Bring each legacy chain up to head with the old per-module "
                "`alembic upgrade head` invocations BEFORE running this "
                "rescue script."
            )

    LOG.info("legacy state verified: all 9 version tables at expected heads")


def perform_rescue(conn: PgConnection, *, dry_run: bool) -> None:
    """Stamp the unified table and drop the legacy tables in one transaction."""
    if dry_run:
        LOG.info("[dry-run] would CREATE TABLE alembic_version with version_num='%s'", UNIFIED_HEAD_REVISION)
        for table in LEGACY_TREES:
            LOG.info("[dry-run] would DROP TABLE %s", table)
        return

    with conn.cursor() as cur:
        # alembic_version DDL matches what Alembic emits on first stamp.
        cur.execute(
            "CREATE TABLE alembic_version ("
            "  version_num VARCHAR(32) NOT NULL,"
            "  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
            ")"
        )
        cur.execute(
            "INSERT INTO alembic_version (version_num) VALUES (%s)",
            (UNIFIED_HEAD_REVISION,),
        )
        for table in LEGACY_TREES:
            cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
    conn.commit()
    LOG.info(
        "rescue complete: alembic_version stamped with '%s' and 9 legacy tables dropped",
        UNIFIED_HEAD_REVISION,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions without modifying the database.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log each verification step in detail.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        LOG.error("DATABASE_URL is not set; refusing to guess connection params")
        return 2

    try:
        conn = _connect(database_url)
    except psycopg2.OperationalError as exc:
        LOG.error("could not connect to the database: %s", exc)
        return 2

    try:
        try:
            verify_legacy_state(conn)
        except RescueError as exc:
            msg = str(exc)
            if "already been migrated" in msg:
                LOG.info("%s", msg)
                return 0
            LOG.error("%s", msg)
            return 1

        perform_rescue(conn, dry_run=args.dry_run)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
