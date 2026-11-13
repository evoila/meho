# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for migration ``0166``: it rewords the text that ``0018`` stored.

* A row that still holds the old seeded text gets the new text.
* A row that an admin edited is not touched.
* A new install (``0018`` itself) never stores the old text.
"""

from __future__ import annotations

import hashlib
import importlib.util
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_SLUG: Final[str] = "sensitive-lab-specifics-stay-private"
_VERSIONS: Final[Path] = Path(__file__).resolve().parents[2] / "alembic" / "versions"

#: The new wording that both ``0018`` and ``0166`` hold.
_NEW_PHRASE: Final[str] = "a private repository"

#: The old repository name, built from pieces so this file does not hold it.
_OLD_PHRASE: Final[str] = "`" + "-".join(["evoila", "bosnia/meho"]) + "-" + "internal" + "`"


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, next(_VERSIONS.glob(f"{name}_*.py")))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_body_0018() -> str:
    module = _load("0018")
    for slug, _title, _priority, body in module._SEED_CONVENTIONS:  # type: ignore[attr-defined]
        if slug == _SLUG:
            return str(body)
    raise AssertionError("seeded convention not found")


def _old_body() -> str:
    return _seed_body_0018().replace(_NEW_PHRASE, _OLD_PHRASE)


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


def _insert(sync_url: str, *, tenant_slug: str, slug: str, body: str) -> uuid.UUID:
    """Insert a convention row (and one history row) for a new tenant."""
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
                    " :slug, 't', :body, 'operational', 1, 'user:a', :now, :now)"
                ),
                {
                    "id": convention_id.hex,
                    "tid": tenant_id.hex,
                    "slug": slug,
                    "body": body,
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_convention_history (id, convention_id, body_before,"
                    " body_after, actor_sub, ts, audit_id) VALUES (:id, :cid, NULL, :body,"
                    " 'user:a', :now, NULL)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "cid": convention_id.hex,
                    "body": body,
                    "now": now,
                },
            )
    finally:
        engine.dispose()
    return convention_id


def _bodies(sync_url: str, convention_id: uuid.UUID) -> tuple[str, str]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            body = conn.execute(
                text("SELECT body FROM tenant_conventions WHERE id = :i"),
                {"i": convention_id.hex},
            ).scalar_one()
            after = conn.execute(
                text("SELECT body_after FROM tenant_convention_history WHERE convention_id = :i"),
                {"i": convention_id.hex},
            ).scalar_one()
    finally:
        engine.dispose()
    return str(body), str(after)


def test_old_body_matches_the_hash_in_the_migration() -> None:
    migration = _load("0166")
    digest = hashlib.sha256(_old_body().encode()).hexdigest()
    assert digest == migration._OLD_BODY_SHA256  # type: ignore[attr-defined]


def test_new_body_in_0166_equals_the_0018_seed() -> None:
    migration = _load("0166")
    assert _seed_body_0018() == migration._NEW_BODY  # type: ignore[attr-defined]


def test_0018_seed_has_the_new_wording() -> None:
    body = _seed_body_0018()
    assert _NEW_PHRASE in body
    assert _OLD_PHRASE not in body


def test_old_text_is_reworded_and_edited_row_is_kept(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, "0165")
    old_id = _insert(sync_url, tenant_slug="tenant-old", slug=_SLUG, body=_old_body())
    edited_text = _old_body() + " Edited by an admin."
    edited_id = _insert(sync_url, tenant_slug="tenant-edited", slug=_SLUG, body=edited_text)

    command.upgrade(cfg, "0166")

    new_body, new_after = _bodies(sync_url, old_id)
    assert new_body == _seed_body_0018()
    assert new_after == _seed_body_0018()
    assert _OLD_PHRASE not in new_body
    assert _bodies(sync_url, edited_id) == (edited_text, edited_text)


def test_running_it_twice_changes_nothing(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, "0165")
    row_id = _insert(sync_url, tenant_slug="tenant-old", slug=_SLUG, body=_old_body())
    command.upgrade(cfg, "0166")
    command.stamp(cfg, "0165")
    command.upgrade(cfg, "0166")
    assert _bodies(sync_url, row_id) == (_seed_body_0018(), _seed_body_0018())
