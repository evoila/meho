# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for the lifespan schema-readiness gate (Goal #294 / #313).

The gate (``meho_app.main._ensure_schema_ready``) is fatal: if the database is
not at the latest Alembic head, it must raise :class:`SystemExit` *before* the
app accepts traffic, with a message that names both the current revision and
the head, plus the commands an operator should run to recover.

These tests verify the three observable behaviours:

* match -- returns cleanly, no SystemExit, logs ``Phase 0 -- schema ready``;
* mismatch -- raises SystemExit, message contains both revisions + fix hint;
* no row -- raises SystemExit, message names "no alembic_version row".

We mock the Alembic + engine surface explicitly so the tests do not depend on
a running Postgres or on whatever revision the local repo happens to be at.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.main import _ensure_schema_ready


def _mock_engine_returning(current_revision: str | None) -> MagicMock:
    """
    Build a mock async SQLAlchemy engine whose ``connect()`` context manager
    yields a connection whose ``run_sync(callback)`` returns ``current_revision``.

    The real implementation calls
    ``conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())``;
    we don't need to honour the callback contents -- the value we return *is*
    the revision the gate will read.
    """
    conn = MagicMock()
    conn.run_sync = AsyncMock(return_value=current_revision)

    conn_ctx = MagicMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.connect = MagicMock(return_value=conn_ctx)
    return engine


class TestEnsureSchemaReady:
    """Verify the three observable behaviours of the schema-readiness gate."""

    async def test_returns_cleanly_when_revision_matches_head(self):
        """When current == head, the gate logs success and returns without raising."""
        with (
            patch("alembic.config.Config", return_value=MagicMock()),
            patch("alembic.script.ScriptDirectory.from_config") as mock_from_config,
            patch(
                "meho_app.main.get_engine",
                return_value=_mock_engine_returning("0009_doc_family"),
            ),
        ):
            mock_from_config.return_value = MagicMock(
                get_current_head=MagicMock(return_value="0009_doc_family"),
            )

            await _ensure_schema_ready()  # must not raise

    async def test_raises_systemexit_when_no_revision_row(self):
        """No alembic_version row -> SystemExit naming the missing-row condition."""
        with (
            patch("alembic.config.Config", return_value=MagicMock()),
            patch("alembic.script.ScriptDirectory.from_config") as mock_from_config,
            patch(
                "meho_app.main.get_engine",
                return_value=_mock_engine_returning(None),
            ),
        ):
            mock_from_config.return_value = MagicMock(
                get_current_head=MagicMock(return_value="0009_doc_family"),
            )
            with pytest.raises(SystemExit) as excinfo:
                await _ensure_schema_ready()

        message = str(excinfo.value)
        assert "no alembic_version row" in message
        assert "0009_doc_family" in message
        assert "alembic -c meho_app/alembic.ini upgrade head" in message

    async def test_raises_systemexit_when_revision_mismatch(self):
        """current != head -> SystemExit naming both revisions and the fix command."""
        with (
            patch("alembic.config.Config", return_value=MagicMock()),
            patch("alembic.script.ScriptDirectory.from_config") as mock_from_config,
            patch(
                "meho_app.main.get_engine",
                return_value=_mock_engine_returning("0007_old_revision"),
            ),
        ):
            mock_from_config.return_value = MagicMock(
                get_current_head=MagicMock(return_value="0009_doc_family"),
            )
            with pytest.raises(SystemExit) as excinfo:
                await _ensure_schema_ready()

        message = str(excinfo.value)
        assert "current revision: 0007_old_revision" in message
        assert "expected (head):  0009_doc_family" in message
        assert "alembic -c meho_app/alembic.ini upgrade head" in message
        assert "docker compose exec meho" in message
        assert "./scripts/dev-env.sh up" in message
