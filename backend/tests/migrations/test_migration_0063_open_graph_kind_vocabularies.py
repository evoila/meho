# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0063_open_graph_kind_vocabularies``.

Initiative #2533 (Topology v2), Task #2534 (T1). The migration replaces
the closed IN-list CHECKs ``ck_graph_node_kind`` (14 kinds, migration
``0007``) and ``ck_graph_edge_kind`` (10 kinds, migration ``0010``)
with one portable minimal shape CHECK per table
(``length(kind) BETWEEN 2 AND 63 AND kind = lower(kind)``).

Coverage:

* **Forward** — post-``0063`` a novel node kind (``dns-record``) and a
  novel edge kind (``resolves-to``) insert cleanly; every closed-set
  member still validates; a shape-violating kind (uppercase, too
  short, too long) is rejected by :class:`IntegrityError`.
* **Sibling-CHECK survival** — ``ck_graph_edge_source`` still rejects
  an unknown ``source`` after the batch table rebuild (the SQLite
  batch-mode CHECK caveat: named CHECKs participate in the rebuild,
  and this test pins that behaviour empirically).
* **Pre-upgrade** — at revision ``0062`` the novel kinds are still
  rejected by the closed IN-lists.
* **Round-trip** — ``upgrade 0063`` → ``downgrade 0062`` → ``upgrade
  0063`` is clean on an empty graph.
* **Downgrade refusal** — ``downgrade 0062`` raises
  :class:`RuntimeError` naming kinds + row counts when novel-kind rows
  exist (mirrors migration ``0010``'s pre-check discipline).

**Idempotency pinning (0049/0050/0054/0055 footgun).** Every
forward / round-trip step targets this migration's **own** revision
(``0063``) and its ``down_revision`` (``0062``), never ``head`` — so a
future head migration cannot silently change what these tests
exercise. SQLite is the test driver; the migration uses only
batch-mode generic DDL, so PG parity holds (PG runs the equivalent
native ``ALTER TABLE`` statements).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0063"
_DOWN_REVISION = "0062"

#: The closed vocabularies the migration opens — inlined (not imported
#: from the model layer) so the test pins the historical contract.
_NODE_KINDS_CLOSED: tuple[str, ...] = (
    "target",
    "vm",
    "host",
    "network",
    "datastore",
    "namespace",
    "pod",
    "service",
    "ingress",
    "node",
    "principal",
    "vault-role",
    "vault-mount",
    "volume",
)
_EDGE_KINDS_CLOSED: tuple[str, ...] = (
    "runs-on",
    "mounts",
    "routes-through",
    "belongs-to",
    "authenticates-via",
    "depends-on",
    "replicates-to",
    "backed-up-by",
    "routes-via",
    "policy-binds",
)

#: Kinds that violate the post-0063 shape CHECK (uppercase, too short,
#: too long) — must raise :class:`IntegrityError` on direct insert.
_SHAPE_VIOLATIONS: tuple[str, ...] = ("DNS-Record", "x", "a" * 64)


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0063.db"
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


def _seed_tenant_and_nodes(sync_url: str) -> tuple[str, str, str]:
    """Insert a tenant + two closed-kind nodes; return ``(tenant, from, to)`` ids."""
    eng = sa_create_engine(sync_url)
    tenant_id = str(uuid.uuid4())
    from_id = str(uuid.uuid4())
    to_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": f"t-{tenant_id[:8]}",
                    "name": "Vocabulary Tenant",
                    "created_at": now,
                },
            )
            for node_id, kind, name in (
                (from_id, "service", "svc-under-test"),
                (to_id, "vm", "vm-under-test"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO graph_node "
                        "(id, tenant_id, kind, name, properties, "
                        "discovered_by, first_seen) "
                        "VALUES (:id, :tenant_id, :kind, :name, '{}', "
                        ":discovered_by, :first_seen)"
                    ),
                    {
                        "id": node_id,
                        "tenant_id": tenant_id,
                        "kind": kind,
                        "name": name,
                        "discovered_by": "vocab-test",
                        "first_seen": now,
                    },
                )
    finally:
        eng.dispose()
    return tenant_id, from_id, to_id


def _insert_node(sync_url: str, tenant_id: str, kind: str, name: str) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO graph_node "
                    "(id, tenant_id, kind, name, properties, "
                    "discovered_by, first_seen) "
                    "VALUES (:id, :tenant_id, :kind, :name, '{}', "
                    ":discovered_by, :first_seen)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "name": name,
                    "discovered_by": "vocab-test",
                    "first_seen": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        eng.dispose()


def _insert_edge(
    sync_url: str,
    tenant_id: str,
    from_id: str,
    to_id: str,
    kind: str,
    *,
    source: str = "curated",
) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO graph_edge "
                    "(id, tenant_id, from_node_id, to_node_id, kind, "
                    "source, properties, discovered_by, first_seen) "
                    "VALUES (:id, :tenant_id, :from_id, :to_id, :kind, "
                    ":source, '{}', :discovered_by, :first_seen)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "from_id": from_id,
                    "to_id": to_id,
                    "kind": kind,
                    "source": source,
                    "discovered_by": "vocab-test",
                    "first_seen": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        eng.dispose()


def test_upgrade_accepts_novel_and_closed_kinds(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Post-0063: novel slugs and every closed-set member insert cleanly."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)

    # Novel kinds — the point of the migration.
    _insert_node(sync_url, tenant_id, "dns-record", "www.example.com")
    _insert_node(sync_url, tenant_id, "keycloak-realm", "master")
    _insert_edge(sync_url, tenant_id, from_id, to_id, "resolves-to")
    _insert_edge(sync_url, tenant_id, from_id, to_id, "same-as")

    # Every closed-set member still validates (backward compatibility).
    for kind in _NODE_KINDS_CLOSED:
        _insert_node(sync_url, tenant_id, kind, f"compat-{kind}")
    for kind in _EDGE_KINDS_CLOSED:
        _insert_edge(sync_url, tenant_id, from_id, to_id, kind)


@pytest.mark.parametrize("bad_kind", _SHAPE_VIOLATIONS)
def test_upgrade_shape_check_rejects_malformed_kind(
    alembic_cfg: tuple[Config, str],
    bad_kind: str,
) -> None:
    """Post-0063: uppercase / too-short / too-long kinds violate the CHECK."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)

    with pytest.raises(IntegrityError):
        _insert_node(sync_url, tenant_id, bad_kind, "bad-node")
    with pytest.raises(IntegrityError):
        _insert_edge(sync_url, tenant_id, from_id, to_id, bad_kind)


def test_upgrade_preserves_ck_graph_edge_source(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``ck_graph_edge_source`` survives the batch table rebuild.

    The SQLite batch-mode caveat: :func:`op.batch_alter_table` rebuilds
    the table under the SQLite dialect, and only **named** CHECK
    constraints participate in the recreate (Alembic batch docs,
    "Working with constraints"). Both graph CHECKs are named, so the
    sibling ``source`` constraint must still reject an unknown value
    post-0063 — this test is the empirical pin for that behaviour.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)

    with pytest.raises(IntegrityError):
        _insert_edge(
            sync_url,
            tenant_id,
            from_id,
            to_id,
            "runs-on",
            source="inferred",
        )


def test_pre_upgrade_rejects_novel_kinds(
    alembic_cfg: tuple[Config, str],
) -> None:
    """At revision 0062 the closed IN-lists still reject novel kinds."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)

    with pytest.raises(IntegrityError):
        _insert_node(sync_url, tenant_id, "dns-record", "www.example.com")
    with pytest.raises(IntegrityError):
        _insert_edge(sync_url, tenant_id, from_id, to_id, "resolves-to")


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade 0062`` → ``upgrade 0063`` is clean on a compliant graph.

    Pinned to this migration's own revision on both legs (never
    ``head``). The seeded rows all use closed-set kinds, so the
    downgrade pre-check passes; post-round-trip the open constraint is
    back in effect (novel kind inserts cleanly).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)
    _insert_edge(sync_url, tenant_id, from_id, to_id, "depends-on")

    command.downgrade(cfg, _DOWN_REVISION)
    with pytest.raises(IntegrityError):
        _insert_node(sync_url, tenant_id, "dns-record", "www.example.com")

    command.upgrade(cfg, _REVISION)
    _insert_node(sync_url, tenant_id, "dns-record", "www.example.com")


def test_downgrade_refuses_when_novel_kind_rows_exist(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade 0062`` raises :class:`RuntimeError` naming novel kinds.

    Mirrors migration ``0010``'s downgrade pre-check discipline: the
    refusal must name each offending kind with its row count so the
    operator can write the targeted cleanup before retrying.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id, from_id, to_id = _seed_tenant_and_nodes(sync_url)
    _insert_node(sync_url, tenant_id, "dns-record", "www.example.com")
    _insert_edge(sync_url, tenant_id, from_id, to_id, "resolves-to")

    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(cfg, _DOWN_REVISION)

    message = str(exc_info.value)
    assert "Cannot downgrade migration 0063" in message
    # graph_node is pre-checked first, so its kind is the one named.
    assert "dns-record=1" in message
