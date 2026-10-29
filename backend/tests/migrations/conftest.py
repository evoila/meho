# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared fixtures for the migration tests: migrated SQLite files, built once.

Most migration tests start from a database at some revision (often the
one before the migration under test) and then run only the step they
check. Building that start point with ``command.upgrade(cfg, rev)`` on an
empty file replays the whole chain of migrations, for every test. With
more than 100 migrations, that replay is most of the job's time.

:func:`copy_migrated_db` builds a SQLite file at each revision a test
asks for **once per test session**, keeps it, and gives each test a
byte copy (:func:`shutil.copyfile`, like ``_schema_template_db`` in
``tests/conftest.py``). Each test still works on its own file.

A saved file for revision N is built from the saved file of the nearest
earlier revision, so the chain is replayed at most once per session.
One exception: a migration that reads ``starting_heads`` (set by
``alembic/env.py``) acts differently when the database is created from
empty in the same run. The builder never steps over such a migration
from a saved file; it starts from empty instead, so every saved file
is the same as a database built from empty in one upgrade.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from meho_backplane.db.migrations import alembic_config

#: Text that marks a migration whose result depends on the database being
#: created from empty in the same upgrade run (see ``alembic/env.py``).
_FROM_EMPTY_MARKER = "starting_heads"


class MigratedDbTemplates:
    """Build and keep one migrated SQLite file per revision."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        script = ScriptDirectory.from_config(alembic_config())
        self._script = script
        # The chain is linear (``test_migration_heads`` pins one head), so
        # a list from base to head gives every revision its position.
        self._chain = [rev.revision for rev in reversed(list(script.walk_revisions()))]
        self._position = {rev: index for index, rev in enumerate(self._chain)}
        self._from_empty_only = {
            rev.revision
            for rev in script.walk_revisions()
            if rev.path is not None and _FROM_EMPTY_MARKER in Path(rev.path).read_text()
        }
        self._built: dict[str, Path] = {}

    def path_for(self, revision: str) -> Path:
        """Return the saved file for ``revision``; build it on first use."""
        script_rev = self._script.get_revision(revision)
        if script_rev is None:
            raise ValueError(f"unknown revision {revision!r}")
        rev = script_rev.revision
        if rev not in self._built:
            self._built[rev] = self._build(rev)
        return self._built[rev]

    def _build(self, rev: str) -> Path:
        target = self._directory / f"{rev}.db"
        base = self._nearest_saved_base(rev)
        if base is not None:
            shutil.copyfile(self._built[base], target)
        url = f"sqlite+aiosqlite:///{target}"
        # ``alembic/env.py`` reads ``DATABASE_URL`` before the config value,
        # so set it for the build and put the test's value back after.
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", url)
            command.upgrade(cfg, rev)
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
        return target

    def _nearest_saved_base(self, rev: str) -> str | None:
        """Return the latest saved revision at or before ``rev`` that is safe to start from."""
        position = self._position[rev]
        best: str | None = None
        for saved in self._built:
            saved_position = self._position[saved]
            if saved_position > position:
                continue
            if best is not None and saved_position <= self._position[best]:
                continue
            steps = self._chain[saved_position + 1 : position + 1]
            if any(step in self._from_empty_only for step in steps):
                continue
            best = saved
        return best


@pytest.fixture(scope="session")
def _migrated_db_templates(tmp_path_factory: pytest.TempPathFactory) -> MigratedDbTemplates:
    return MigratedDbTemplates(tmp_path_factory.mktemp("migrated-db-templates"))


@pytest.fixture
def copy_migrated_db(
    _migrated_db_templates: MigratedDbTemplates,
) -> Callable[[Config, str], None]:
    """Put a database at ``revision`` into the file that ``cfg`` points to.

    Use it in place of the first ``command.upgrade(cfg, revision)`` of a
    test. The result is the same database, without replaying the chain.
    The test's database file must not hold data yet: the copy replaces it.
    """

    def _copy(cfg: Config, revision: str) -> None:
        url = cfg.get_main_option("sqlalchemy.url")
        database = make_url(url).database if url else None
        if not database:
            raise ValueError("the Alembic config has no SQLite file in sqlalchemy.url")
        db_path = Path(database)
        if db_path.exists() and db_path.stat().st_size > 0:
            raise RuntimeError(f"{db_path} already holds data; copy the migrated database first")
        shutil.copyfile(_migrated_db_templates.path_for(revision), db_path)

    return _copy
