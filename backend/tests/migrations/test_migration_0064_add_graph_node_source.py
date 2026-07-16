# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0064_add_graph_node_source``.

Initiative #2533 (Topology v2), Task #2536 (T2). The migration adds
``graph_node.source`` (``TEXT NOT NULL DEFAULT 'auto'``, CHECK
``ck_graph_node_source`` mirroring ``ck_graph_edge_source``) and
backfills ``source='curated'`` for rows carrying the manual-seed
``properties.seeded_by`` stamp.

Coverage:

* **Backfill** — a pre-0064 row with ``seeded_by`` in ``properties``
  lands as ``curated``; a probe-discovered row (no stamp) lands as
  ``auto``.
* **Default** — a post-0064 insert that omits ``source`` gets
  ``'auto'`` from the server default.
* **CHECK** — a post-0064 insert with an out-of-vocabulary ``source``
  is rejected with :class:`IntegrityError`.
* **Sibling-CHECK survival** — ``ck_graph_node_kind`` still rejects a
  malformed kind after the batch table rebuild (the SQLite batch-mode
  caveat migration ``0063`` pinned for the edge table, now pinned for
  the node table).
* **Round-trip** — ``upgrade 0064`` → ``downgrade 0063`` (column gone)
  → ``upgrade 0064`` is clean; the backfill replays identically.

**Idempotency pinning (0049/0050/0054/0055 footgun).** Every
forward / round-trip step targets this migration's **own** revision
(``0064``) and its ``down_revision`` (``0063``), never ``head`` — so a
future head migration cannot silently change what these tests
exercise. SQLite is the test driver; the migration branches only on
the backfill predicate (PG ``properties ? 'seeded_by'`` vs SQLite
``json_extract``), both asserting the same key-existence contract.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0064"
_DOWN_REVISION = "0063"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0064.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _seed_tenant(sync_url: str) -> str:
    """Insert one tenant; return its id."""
    eng = sa_create_engine(sync_url)
    tenant_id = str(uuid.uuid4())
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": f"t-{tenant_id[:8]}",
                    "name": "Source Tenant",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        eng.dispose()
    return tenant_id


def _insert_node_pre_0064(
    sync_url: str,
    tenant_id: str,
    *,
    name: str,
    properties: dict[str, Any],
) -> str:
    """Insert a ``graph_node`` row at revision 0063 (no ``source`` column)."""
    node_id = str(uuid.uuid4())
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO graph_node "
                    "(id, tenant_id, kind, name, properties, "
                    "discovered_by, first_seen) "
                    "VALUES (:id, :tenant_id, 'vm', :name, :properties, "
                    "'source-test', :first_seen)"
                ),
                {
                    "id": node_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "properties": json.dumps(properties),
                    "first_seen": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        eng.dispose()
    return node_id


def _insert_node_post_0064(
    sync_url: str,
    tenant_id: str,
    *,
    name: str,
    kind: str = "vm",
    source: str | None = None,
) -> None:
    """Insert a row post-0064; ``source=None`` omits the column (default)."""
    columns = "id, tenant_id, kind, name, properties, discovered_by, first_seen"
    values = ":id, :tenant_id, :kind, :name, '{}', 'source-test', :first_seen"
    params: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "kind": kind,
        "name": name,
        "first_seen": datetime.now(UTC).isoformat(),
    }
    if source is not None:
        columns += ", source"
        values += ", :source"
        params["source"] = source
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(f"INSERT INTO graph_node ({columns}) VALUES ({values})"),
                params,
            )
    finally:
        eng.dispose()


def _select_source(sync_url: str, node_id: str) -> str:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT source FROM graph_node WHERE id = :id"),
                {"id": node_id},
            ).one()
    finally:
        eng.dispose()
    return str(row.source)


def test_backfill_marks_seeded_rows_curated_and_probe_rows_auto(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The 0064 backfill keys on the ``properties.seeded_by`` stamp."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    tenant_id = _seed_tenant(sync_url)
    seeded_id = _insert_node_pre_0064(
        sync_url,
        tenant_id,
        name="manually-seeded",
        properties={
            "note": "seeded from INVENTORY.md",
            "seeded_by": "op-1",
            "seeded_at": "2026-01-01T00:00:00+00:00",
        },
    )
    probe_id = _insert_node_pre_0064(
        sync_url,
        tenant_id,
        name="probe-discovered",
        properties={"power": "on"},
    )

    command.upgrade(cfg, _REVISION)

    assert _select_source(sync_url, seeded_id) == "curated"
    assert _select_source(sync_url, probe_id) == "auto"


def test_post_upgrade_insert_defaults_to_auto(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A post-0064 insert omitting ``source`` gets ``'auto'`` from the default."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id = _seed_tenant(sync_url)
    _insert_node_post_0064(sync_url, tenant_id, name="defaulted")

    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT source FROM graph_node WHERE name = 'defaulted'"),
            ).one()
    finally:
        eng.dispose()
    assert row.source == "auto"


def test_check_rejects_unknown_source(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``ck_graph_node_source`` rejects values outside (auto, curated)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id = _seed_tenant(sync_url)

    with pytest.raises(IntegrityError):
        _insert_node_post_0064(sync_url, tenant_id, name="bad-source", source="inferred")

    _insert_node_post_0064(sync_url, tenant_id, name="ok-auto", source="auto")
    _insert_node_post_0064(sync_url, tenant_id, name="ok-curated", source="curated")


def test_sibling_kind_check_survives_batch_rebuild(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``ck_graph_node_kind`` still rejects a malformed kind post-0064.

    The SQLite batch-mode caveat: :func:`op.batch_alter_table` rebuilds
    the table, and only **named** CHECK constraints participate in the
    recreate. Both node CHECKs are named — this is the empirical pin
    (mirror of migration ``0063``'s edge-side pin).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id = _seed_tenant(sync_url)

    with pytest.raises(IntegrityError):
        _insert_node_post_0064(sync_url, tenant_id, name="bad-kind", kind="DNS-Record")


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade 0063`` drops the column; ``upgrade 0064`` replays the backfill.

    Pinned to this migration's own revision on both legs (never
    ``head``). The seeded row's ``properties.seeded_by`` stamp survives
    the column drop, so the replayed backfill lands ``curated`` again.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    tenant_id = _seed_tenant(sync_url)
    seeded_id = _insert_node_pre_0064(
        sync_url,
        tenant_id,
        name="round-trip-seed",
        properties={"seeded_by": "op-1"},
    )

    command.upgrade(cfg, _REVISION)
    assert _select_source(sync_url, seeded_id) == "curated"

    command.downgrade(cfg, _DOWN_REVISION)
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(graph_node)")).all()}
    finally:
        eng.dispose()
    assert "source" not in cols, "downgrade must drop the column"

    command.upgrade(cfg, _REVISION)
    assert _select_source(sync_url, seeded_id) == "curated"
