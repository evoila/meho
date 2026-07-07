# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0048_seed_vmware_collection_manifest``.

Initiative #1912 (corpus grounded-answer pipeline), Task #1920. The
migration fills the agent-facing manifest prose on the *global*
``vmware`` ``doc_collections`` row so #1916's corpus-aware query
expansion (:func:`~meho_backplane.docs_search.expansion.expand_docs_query`)
has ``description`` / ``when_to_use`` to read.

Test matrix
-----------

* **Empty global ``vmware`` row -> filled.** A global row seeded with no
  prose, an empty ``products``, and the bare ``vmware`` vendor token
  carries the hand-authored ``description`` / ``when_to_use``, the
  canonical product list, and ``VMware by Broadcom`` after ``upgrade
  head`` (and a bumped ``updated_at``).
* **Operator-authored prose -> not clobbered.** A row whose
  ``description`` / ``when_to_use`` / ``products`` / ``vendor`` are
  already set keeps its content (fill-only).
* **Tenant-curated ``vmware`` row -> untouched.** The seed scopes to the
  global (``tenant_id IS NULL``) row only.
* **No ``vmware`` row -> no-op (no INSERT).** A deploy without the
  out-of-band global seed gains no row -- the corpus is not registered,
  so there is no manifest to fill.
* **Idempotency.** Re-running ``upgrade()`` against an already-seeded DB
  (stamp back + re-upgrade) does not bump ``updated_at`` a second time.
* **Downgrade is narrow.** ``downgrade`` clears the seeded prose but
  leaves an operator edit made after the seed in place.
* **Manifest reaches the expansion prompt (AC2).** The seeded fields, fed
  through #1916's
  :func:`~meho_backplane.docs_search.expansion._render_manifest_for_prompt`,
  appear as ``description:`` / ``when_to_use:`` lines in the prompt block.

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's async
cookbook through ``asyncio.run``, so the migration-driving test functions
stay sync (the ``0046`` harness).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

#: Deliberately old seed timestamp -- lets assertions tell "migration
#: bumped updated_at" apart from "row untouched" without sleeping.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"

#: A backend routing record every seeded row needs (``backend`` is NOT
#: NULL with no default). The value is incidental to this migration -- it
#: never touches ``backend``.
_BACKEND_JSON: Final[str] = (
    '{"type": "corpus-http", "ref": {"endpoint": "https://corpus.test/v1/search"}}'
)

# The exact values ``0048`` authors. Re-declared here (the alembic versions
# dir is not an importable package and the file name is digit-leading -- the
# same redeclare-the-constants pattern the 0028 / 0046 migration tests use).
# Pinning the literal text is intentional: a change to the seeded manifest
# must update this test in lock-step, so the assertions are the contract for
# "what the vmware row carries".
_VENDOR_CANONICAL: Final[str] = "VMware by Broadcom"
_DESCRIPTION: Final[str] = (
    "VMware vSphere, VCF, and NSX product documentation, Broadcom KB "
    "articles, and curated community posts covering vSphere, vCenter, "
    "ESXi, NSX, vSAN, and the Aria/vRealize suite."
)
_WHEN_TO_USE: Final[str] = (
    "VMware / Broadcom infrastructure questions -- vSphere, vCenter, "
    "ESXi, VCF, NSX, vSAN, and Aria/vRealize (vROps, vRLI)."
)
_PRODUCTS: Final[tuple[str, ...]] = ("vsphere", "vcf", "nsx", "vsan", "vrops", "vrli")


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as
    :mod:`tests.test_migration_0046_reconcile_vrli_target_product`: sync
    fixture (``alembic.command`` calls ``asyncio.run`` internally), a
    per-test SQLite file under ``tmp_path``, settings + engine caches reset
    on both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0048.db"
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


def _insert_collection(
    sync_url: str,
    *,
    collection_key: str = "vmware",
    tenant_id: UUID | None = None,
    vendor: str = "vmware",
    products_json: str = "[]",
    description: str | None = None,
    when_to_use: str | None = None,
) -> UUID:
    """Insert one ``doc_collections`` row at the migration's down-revision.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the migration
    runs against. UUID binds use ``.hex`` per ``docs/codebase/migrations.md``.
    ``products`` is supplied as a JSON-text array (the SQLite storage form).
    """
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO doc_collections (
                        id, tenant_id, collection_key, vendor, products,
                        backend, status, description, when_to_use,
                        created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :collection_key, :vendor, :products,
                        :backend, 'ready', :description, :when_to_use,
                        :ts, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": tenant_id.hex if tenant_id is not None else None,
                    "collection_key": collection_key,
                    "vendor": vendor,
                    "products": products_json,
                    "backend": _BACKEND_JSON,
                    "description": description,
                    "when_to_use": when_to_use,
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _read_row(sync_url: str, row_id: UUID) -> dict[str, str | None]:
    """Return the manifest fields for one collection row, as strings."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT vendor, products, description, when_to_use, updated_at
                    FROM doc_collections WHERE id = :id
                    """,
                ),
                {"id": row_id.hex},
            ).one()
            return {
                "vendor": str(row.vendor),
                "products": str(row.products),
                "description": row.description,
                "when_to_use": row.when_to_use,
                "updated_at": str(row.updated_at),
            }
    finally:
        sync_eng.dispose()


def _count_collections(sync_url: str) -> int:
    """Return the number of ``doc_collections`` rows."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM doc_collections")).scalar_one())
    finally:
        sync_eng.dispose()


def test_empty_global_vmware_row_is_filled(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A global ``vmware`` row with empty prose gains the hand-authored manifest."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047")

    row_id = _insert_collection(sync_url)  # global, bare vendor, [] products, no prose

    command.upgrade(cfg, "head")

    row = _read_row(sync_url, row_id)
    assert row["description"] == _DESCRIPTION
    assert row["when_to_use"] == _WHEN_TO_USE
    assert row["vendor"] == _VENDOR_CANONICAL
    # products filled from empty to the canonical list (JSON-text on SQLite).
    for product in _PRODUCTS:
        assert product in str(row["products"])
    assert row["updated_at"] != _SEED_TS, "a fill must bump updated_at"


def test_operator_authored_prose_not_clobbered(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A row that already carries a manifest keeps its operator content.

    Fill-only: ``description`` / ``when_to_use`` / ``products`` / ``vendor``
    are all pre-set, so the migration matches no fill predicate and writes
    nothing (``updated_at`` stays at the seed value).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047")

    row_id = _insert_collection(
        sync_url,
        vendor="VMware (operator label)",
        products_json='["esxi"]',
        description="Operator-authored corpus description.",
        when_to_use="Operator-authored when_to_use blurb.",
    )

    command.upgrade(cfg, "head")

    row = _read_row(sync_url, row_id)
    assert row["description"] == "Operator-authored corpus description."
    assert row["when_to_use"] == "Operator-authored when_to_use blurb."
    assert row["vendor"] == "VMware (operator label)"
    assert row["products"] == '["esxi"]'
    assert row["updated_at"] == _SEED_TS, "fill-only must not touch an already-populated row"


def test_tenant_curated_vmware_row_untouched(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A tenant-scoped ``vmware`` row is left alone -- the seed is global-only."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047")

    tenant_id = UUID("22222222-2222-2222-2222-222222222222")
    row_id = _insert_collection(sync_url, tenant_id=tenant_id)  # empty prose, but tenant-scoped

    command.upgrade(cfg, "head")

    row = _read_row(sync_url, row_id)
    assert row["description"] is None
    assert row["when_to_use"] is None
    assert row["vendor"] == "vmware", "tenant row vendor must not be canonicalised"
    assert row["updated_at"] == _SEED_TS


def test_no_vmware_row_is_a_noop_no_insert(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A deploy without the global ``vmware`` seed gains no row.

    The migration UPDATEs; it never INSERTs (it cannot author the NOT-NULL
    ``backend`` routing record). An unrelated collection is untouched and
    the row count is unchanged.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047")

    other_id = _insert_collection(sync_url, collection_key="other-corpus")

    command.upgrade(cfg, "head")

    assert _count_collections(sync_url) == 1, "migration must not INSERT a vmware row"
    other = _read_row(sync_url, other_id)
    assert other["description"] is None, "an unrelated collection must be untouched"
    assert other["updated_at"] == _SEED_TS


def test_upgrade_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Re-running the upgrade against an already-seeded DB changes nothing.

    Stamp back to the down-revision and upgrade again -- the replay shape
    the sibling ``0046`` / ``0011`` tests use. The second pass finds the
    prose already present, so no row is rewritten (no second ``updated_at``
    bump).

    Both passes pin the target to ``0048`` (not ``head``) so future
    non-idempotent schema migrations cannot leak into the stamp-back
    replay: ``command.stamp`` only rewrites alembic's version table — it
    does not run downgrade SQL — so any column a later migration adds on
    the first upgrade is still physically present, and replaying its
    non-idempotent DDL through ``head`` would fail (e.g. 0050's
    ``add_column("targets", "tls_server_name")`` → "duplicate column").
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047")

    row_id = _insert_collection(sync_url)

    command.upgrade(cfg, "0048")
    updated_after_first = _read_row(sync_url, row_id)["updated_at"]

    command.stamp(cfg, "0047")
    command.upgrade(cfg, "0048")

    row_after_second = _read_row(sync_url, row_id)
    assert row_after_second["description"] == _DESCRIPTION
    assert row_after_second["updated_at"] == updated_after_first, (
        "idempotent re-run must not bump updated_at a second time"
    )


def test_downgrade_clears_seeded_prose_but_keeps_operator_edit(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade`` rewinds only the prose this migration authored.

    A ``when_to_use`` an operator rewrote after the seed survives the
    rollback; the seed-authored ``description`` is cleared back to NULL.

    Pin the two upgrades to ``0048`` (not ``head``) so the stamp-back
    re-seed below replays only 0048's body: ``command.stamp`` skips
    downgrade SQL, so a later migration's non-idempotent DDL (e.g. 0050's
    ``add_column("targets", "tls_server_name")``) would otherwise re-run
    against a column that already exists and fail with "duplicate column".
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    row_id = _insert_collection(sync_url)
    # Run the seed by stamping back + re-upgrading (the row was inserted at
    # 0048, after 0048 already ran on an empty DB).
    command.stamp(cfg, "0047")
    command.upgrade(cfg, "0048")

    # Operator rewrites when_to_use after the seed; description stays seeded.
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text("UPDATE doc_collections SET when_to_use = :w WHERE id = :id"),
                {"w": "Operator override blurb.", "id": row_id.hex},
            )
    finally:
        sync_eng.dispose()

    command.downgrade(cfg, "0047")

    row = _read_row(sync_url, row_id)
    assert row["description"] is None, "seed-authored description must be cleared on downgrade"
    assert row["when_to_use"] == "Operator override blurb.", (
        "an operator edit made after the seed must survive the downgrade"
    )


def test_seeded_manifest_reaches_the_expansion_prompt() -> None:
    """AC2: the seeded fields flow into #1916's expansion prompt block.

    Build a :class:`DocCollection` carrying the migration's exact manifest
    constants and render it through #1916's
    :func:`_render_manifest_for_prompt` -- the seeded ``description`` /
    ``when_to_use`` (and ``vendor`` / ``products``) must appear as labelled
    lines, proving the data this migration writes is exactly what corpus-
    aware expansion reads.
    """
    from meho_backplane.db.models import DocCollection
    from meho_backplane.docs_search.expansion import _render_manifest_for_prompt

    collection = DocCollection(
        collection_key="vmware",
        vendor=_VENDOR_CANONICAL,
        products=list(_PRODUCTS),
        description=_DESCRIPTION,
        when_to_use=_WHEN_TO_USE,
        backend={"type": "corpus-http", "ref": {"endpoint": "https://corpus.test/v1/search"}},
    )

    block = _render_manifest_for_prompt(collection)

    assert f"description: {_DESCRIPTION}" in block
    assert f"when_to_use: {_WHEN_TO_USE}" in block
    assert f"vendor: {_VENDOR_CANONICAL}" in block
    # Every seeded product token surfaces in the products line.
    for product in _PRODUCTS:
        assert product in block
