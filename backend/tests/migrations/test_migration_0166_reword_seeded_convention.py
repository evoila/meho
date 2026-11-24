# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for migration ``0166``: it rewords the texts that ``0018`` stored.

* A row that still holds an old seeded text gets the new text.
* A row that an admin edited is not touched.
* A new install (``0018`` itself) stores the new texts.

The database tests use a made-up old text and its hash in place of the
real ones, so this public file holds no old text.
"""

from __future__ import annotations

import hashlib
import importlib.util
import types
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_VERSIONS: Final[Path] = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_SEED_ACTOR: Final[str] = "migration:seed-rdc-conventions"

#: A made-up old seeded text and the text the test expects in its place.
_OLD_TEXT: Final[str] = "Made-up seeded text for the migration test."
_NEW_TEXT: Final[str] = "Made-up replacement text."
_EDITED_TEXT: Final[str] = _OLD_TEXT + " Edited by an admin."


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, next(_VERSIONS.glob(f"{name}_*.py")))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_bodies_0018() -> dict[str, str]:
    return {slug: body for slug, _title, _priority, body in _load("0018")._SEED_CONVENTIONS}


def _use_made_up_rewords(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next Alembic commands run ``0166`` with the made-up text only.

    Alembic loads each migration file again for every command. So the
    test builds one script directory, changes the table of ``0166`` in
    it, and has the commands use that script directory.
    """
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("0166")
    assert revision is not None
    monkeypatch.setattr(revision.module, "_REWORDS", {_sha256(_OLD_TEXT): _NEW_TEXT})
    monkeypatch.setattr(
        command, "ScriptDirectory", types.SimpleNamespace(from_config=lambda _config: script)
    )


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0166.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _insert(sync_url: str, *, tenant_slug: str, body: str, before: str | None) -> uuid.UUID:
    """Insert a convention row and one history row (``before`` -> ``body``) for a new tenant."""
    engine = sa_create_engine(sync_url)
    tenant_id = uuid.uuid4()
    convention_id = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenant (id, slug, name, created_at) VALUES (:i, :s, :n, :t)"),
                {"i": tenant_id.hex, "s": tenant_slug, "n": tenant_slug, "t": now},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_conventions (id, tenant_id, slug, title, body, kind,"
                    " priority, created_by_sub, created_at, updated_at) VALUES (:id, :tid,"
                    " 'example-convention', 't', :body, 'operational', 1, 'user:a', :now, :now)"
                ),
                {"id": convention_id.hex, "tid": tenant_id.hex, "body": body, "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_convention_history (id, convention_id, body_before,"
                    " body_after, actor_sub, ts, audit_id) VALUES (:id, :cid, :before, :body,"
                    " 'user:a', :now, NULL)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "cid": convention_id.hex,
                    "before": before,
                    "body": body,
                    "now": now,
                },
            )
    finally:
        engine.dispose()
    return convention_id


def _bodies(sync_url: str, convention_id: uuid.UUID) -> tuple[str, str | None, str]:
    """Return (body, history body_before, history body_after) of a convention."""
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            body = conn.execute(
                text("SELECT body FROM tenant_conventions WHERE id = :i"),
                {"i": convention_id.hex},
            ).scalar_one()
            before, after = conn.execute(
                text(
                    "SELECT body_before, body_after FROM tenant_convention_history"
                    " WHERE convention_id = :i"
                ),
                {"i": convention_id.hex},
            ).one()
    finally:
        engine.dispose()
    return body, before, after


def _seed_rows_left(sync_url: str) -> int:
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM tenant_conventions WHERE created_by_sub = :a"),
                    {"a": _SEED_ACTOR},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_new_bodies_in_0166_equal_the_0018_seeds() -> None:
    migration = _load("0166")
    seeds = _seed_bodies_0018()
    assert seeds["naming-rule-no-ai-tool-names"] == migration._NEW_NAMING_BODY
    assert seeds["sensitive-lab-specifics-stay-private"] == migration._NEW_PRIVATE_BODY
    assert set(migration._REWORDS.values()) == {
        migration._NEW_NAMING_BODY,
        migration._NEW_PRIVATE_BODY,
    }


def test_a_new_body_is_not_an_old_one() -> None:
    migration = _load("0166")
    for new_body in migration._REWORDS.values():
        assert _sha256(new_body) not in migration._REWORDS


def test_upgrade_from_0018_rewords_old_text_and_keeps_edited_row(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, "0018")
    assert _seed_rows_left(sync_url) > 0
    old_id = _insert(sync_url, tenant_slug="tenant-old", body=_OLD_TEXT, before=None)
    edited_id = _insert(sync_url, tenant_slug="tenant-edited", body=_EDITED_TEXT, before=_OLD_TEXT)
    _use_made_up_rewords(cfg, monkeypatch)

    command.upgrade(cfg, "0166")

    # 0028 removes the rows that 0018 seeded; the rows of other tenants stay.
    assert _seed_rows_left(sync_url) == 0
    assert _bodies(sync_url, old_id) == (_NEW_TEXT, None, _NEW_TEXT)
    assert _bodies(sync_url, edited_id) == (_EDITED_TEXT, _NEW_TEXT, _EDITED_TEXT)


def test_restored_old_text_is_reworded_and_edited_row_is_kept(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, "0165")
    old_id = _insert(sync_url, tenant_slug="tenant-old", body=_OLD_TEXT, before=None)
    edited_id = _insert(sync_url, tenant_slug="tenant-edited", body=_EDITED_TEXT, before=None)
    _use_made_up_rewords(cfg, monkeypatch)

    command.upgrade(cfg, "0166")

    assert _bodies(sync_url, old_id) == (_NEW_TEXT, None, _NEW_TEXT)
    assert _bodies(sync_url, edited_id) == (_EDITED_TEXT, None, _EDITED_TEXT)


def test_running_it_twice_changes_nothing(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, "0165")
    row_id = _insert(sync_url, tenant_slug="tenant-old", body=_OLD_TEXT, before=None)
    _use_made_up_rewords(cfg, monkeypatch)
    command.upgrade(cfg, "0166")
    command.stamp(cfg, "0165")
    command.upgrade(cfg, "0166")
    assert _bodies(sync_url, row_id) == (_NEW_TEXT, None, _NEW_TEXT)
