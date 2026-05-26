# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0018_seed_rdc_internal_conventions``.

Initiative #229 (G7.1 Tenant conventions + Layer 2 starter), Task
#317 (T5). The migration seeds the ``rdc-internal`` tenant + 8
operational conventions extracted from the consumer's ``CLAUDE.md``
per decision #4. The tests cover the five contracts on the issue body:

* **Tenant + 8 conventions land on a clean DB.** ``upgrade 0018`` from
  a clean DB produces the ``rdc-internal`` tenant row and 8
  ``operational`` convention rows scoped to that tenant; one matching
  history row per convention.
* **Idempotency.** A second ``upgrade`` (via stamp + replay) is a
  no-op: the conventions stay put, no duplicate history rows land,
  no ``updated_at`` movement.
* **Pre-existing tenant is preserved.** A tenant row authored
  manually before the migration ran keeps its ``id`` -- the upsert
  refreshes ``name`` but does not mint a fresh UUID.
* **Operator-curated convention survives.** A convention an operator
  authored under one of the seeded slugs before the migration ran is
  preserved verbatim by ``ON CONFLICT DO NOTHING``; no synthetic
  history row appears against it.
* **Downgrade keeps the tenant.** ``downgrade "0017"`` removes the
  seeded convention rows + their seed-authored history rows, but
  leaves the tenant row intact and leaves operator-authored history
  rows (against seeded slugs) untouched.

These assertions pin the ``0018`` migration's behaviour in isolation
(``upgrade 0018``, not ``upgrade head``). G0.13-T7 (#1137) ships
migration ``0028`` on top of ``0018`` that cleans up the
``rdc-internal`` seed and replaces it with a generic ``default``
tenant for OSS commercialization-readiness -- once ``upgrade head``
runs, ``rdc-internal`` rows are no longer present. The
``0018``-only assertions live here; the post-0028 head-state
assertions live in :mod:`tests.test_alembic_seed_0028_supersede`.

The tests follow the synchronous pattern established by
:mod:`tests.test_migration_0011_backfill_when_to_use`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via
env.py's async cookbook, so the test functions themselves must be
sync. SQLite is the test driver; PG-side shape parity is covered by
the testcontainers replay suite in :mod:`tests.test_migration_rollback`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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

#: The synthetic ``sub`` claim the seed migration records on every
#: row it authors. Mirrors the migration's own constant -- duplicated
#: here so the test couples to the contract, not the implementation.
_SEED_ACTOR_SUB: Final[str] = "migration:seed-rdc-conventions"

#: The seeded tenant's slug.
_TENANT_SLUG: Final[str] = "rdc-internal"

#: The 8 slugs the seed populates. Order does not matter for the
#: assertions; the set membership is what counts.
_EXPECTED_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "vault-canonical",
        "naming-rule-no-ai-tool-names",
        "secret-handling-discipline",
        "cli-wrapper-fallback-discipline",
        "destructive-ops-probe-first",
        "audit-trail-discipline",
        "sensitive-lab-specifics-stay-private",
        "approval-workflow-when-it-lands",
    },
)


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    The fixture is *sync* (returns rather than yields async) because
    :func:`alembic.command.upgrade` calls :func:`asyncio.run`
    internally via the env.py async cookbook -- the same constraint
    that keeps every other migration test in
    :mod:`tests.test_migration_0011_backfill_when_to_use` synchronous.

    The DB file lives under pytest's ``tmp_path`` so each test gets
    an isolated SQLite database; engine + settings caches are reset
    before and after so the alembic env reads *this* DATABASE_URL.
    """
    db_path = tmp_path / "migration_0018.db"
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


def _fetch_tenant_id(sync_url: str, slug: str) -> str | None:
    """Return the tenant id (as a string) for the given slug, or None."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM tenant WHERE slug = :slug"),
                {"slug": slug},
            ).first()
            return None if row is None else str(row[0])
    finally:
        sync_eng.dispose()


def _fetch_convention_rows(
    sync_url: str,
    tenant_id: str,
) -> list[dict[str, object]]:
    """Return all convention rows for the tenant as dicts."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        """
                    SELECT id, slug, title, body, kind, priority,
                           created_by_sub, created_at, updated_at
                    FROM tenant_conventions
                    WHERE tenant_id = :tenant_id
                    ORDER BY slug
                    """,
                    ),
                    {"tenant_id": tenant_id},
                )
                .mappings()
                .all()
            )
    finally:
        sync_eng.dispose()
    return [dict(row) for row in rows]


def _fetch_history_rows(
    sync_url: str,
    convention_id: str,
) -> list[dict[str, object]]:
    """Return all history rows for one convention, oldest first."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        """
                    SELECT id, body_before, body_after, actor_sub, ts,
                           audit_id
                    FROM tenant_convention_history
                    WHERE convention_id = :convention_id
                    ORDER BY ts ASC
                    """,
                    ),
                    {"convention_id": convention_id},
                )
                .mappings()
                .all()
            )
    finally:
        sync_eng.dispose()
    return [dict(row) for row in rows]


def _insert_tenant_row(
    sync_url: str,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    name: str,
) -> None:
    """Insert one ``tenant`` row at a revision before the seed runs."""
    sync_eng = sa_create_engine(sync_url)
    now = datetime.now(UTC).isoformat()
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenant (id, slug, name, created_at)
                    VALUES (:id, :slug, :name, :created_at)
                    """,
                ),
                {
                    # 32-char hex form mirrors SQLAlchemy's ``Uuid`` bind
                    # processor for SQLite (``value.hex``); the seed
                    # migration uses the same form via its ``_uuid_param``
                    # helper so an ORM-issued FK lookup against the
                    # seeded tenant matches bytewise.
                    "id": tenant_id.hex,
                    "slug": slug,
                    "name": name,
                    "created_at": now,
                },
            )
    finally:
        sync_eng.dispose()


def _insert_convention_row(
    sync_url: str,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    title: str,
    body: str,
    created_by_sub: str,
    priority: int = 0,
    kind: str = "operational",
) -> uuid.UUID:
    """Insert one ``tenant_conventions`` row before the seed runs."""
    sync_eng = sa_create_engine(sync_url)
    convention_id = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenant_conventions (
                        id, tenant_id, slug, title, body, kind, priority,
                        created_by_sub, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :slug, :title, :body, :kind,
                        :priority, :created_by_sub, :created_at,
                        :updated_at
                    )
                    """,
                ),
                {
                    # 32-char hex form -- see ``_insert_tenant_row``
                    # rationale above. Same store form as the ORM and
                    # the seed migration's ``_uuid_param`` helper.
                    "id": convention_id.hex,
                    "tenant_id": tenant_id.hex,
                    "slug": slug,
                    "title": title,
                    "body": body,
                    "kind": kind,
                    "priority": priority,
                    "created_by_sub": created_by_sub,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    finally:
        sync_eng.dispose()
    return convention_id


def _insert_history_row(
    sync_url: str,
    *,
    convention_id: uuid.UUID,
    body_before: str | None,
    body_after: str,
    actor_sub: str,
) -> uuid.UUID:
    """Insert one ``tenant_convention_history`` row before / after the seed."""
    sync_eng = sa_create_engine(sync_url)
    history_id = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenant_convention_history (
                        id, convention_id, body_before, body_after,
                        actor_sub, ts, audit_id
                    ) VALUES (
                        :id, :convention_id, :body_before, :body_after,
                        :actor_sub, :ts, NULL
                    )
                    """,
                ),
                {
                    # 32-char hex -- same rationale as ``_insert_tenant_row``.
                    "id": history_id.hex,
                    "convention_id": convention_id.hex,
                    "body_before": body_before,
                    "body_after": body_after,
                    "actor_sub": actor_sub,
                    "ts": now,
                },
            )
    finally:
        sync_eng.dispose()
    return history_id


def test_seed_lands_tenant_and_eight_conventions(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` from clean DB lands tenant + 8 conventions + 8 history rows.

    Asserts the full acceptance contract from #317:

    * Tenant ``rdc-internal`` exists.
    * 8 conventions exist for that tenant, every one with
      ``kind='operational'``.
    * Slugs match the expected set verbatim.
    * Every convention carries ``created_by_sub='migration:seed-rdc-conventions'``.
    * Every convention has exactly one history row whose
      ``body_before`` is NULL (CREATE event) and ``body_after`` matches
      the convention's body.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0018")

    tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id is not None, "seed must create the rdc-internal tenant"

    rows = _fetch_convention_rows(sync_url, tenant_id)
    assert len(rows) == 8, f"expected 8 seeded conventions, got {len(rows)}"
    assert {str(r["slug"]) for r in rows} == _EXPECTED_SLUGS
    assert all(r["kind"] == "operational" for r in rows), (
        "every seeded convention must carry kind='operational' per decision #4"
    )
    assert all(r["created_by_sub"] == _SEED_ACTOR_SUB for r in rows), (
        "every seeded row must record the synthetic seed marker so G8 audit "
        "queries can distinguish seed-vs-operator content"
    )

    # Every convention has exactly one CREATE-shape history row.
    for row in rows:
        history = _fetch_history_rows(sync_url, str(row["id"]))
        assert len(history) == 1, (
            f"expected exactly one CREATE history row for slug {row['slug']!r}, got {len(history)}"
        )
        h = history[0]
        assert h["body_before"] is None, (
            f"the CREATE history row for slug {row['slug']!r} must record body_before=NULL"
        )
        assert h["body_after"] == row["body"]
        assert h["actor_sub"] == _SEED_ACTOR_SUB
        assert h["audit_id"] is None, (
            "the seed migration runs outside any HTTP request so the "
            "history row's audit_id soft-FK has nothing to reference"
        )


def test_priority_tiers_match_documented_assignment(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The three documented priority tiers are applied to the expected slugs.

    ``docs/architecture/conventions-seed.md`` documents the priority
    assignment:

    * ``priority=100`` -- unrecoverable-breach rules (vault, secrets,
      sensitive lab specifics).
    * ``priority=50`` -- recoverable-but-costly rules (naming,
      CLI-wrapper, destructive-ops-probe).
    * ``priority=10`` -- aspirational rules (audit-trail,
      approval-workflow).

    This test couples to the contract (priorities documented in the
    seed doc) so a regression in either the migration or the doc
    surfaces here.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0018")

    tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id is not None
    rows = _fetch_convention_rows(sync_url, tenant_id)
    priority_by_slug = {str(r["slug"]): int(str(r["priority"])) for r in rows}

    high_priority = {
        "vault-canonical",
        "secret-handling-discipline",
        "sensitive-lab-specifics-stay-private",
    }
    medium_priority = {
        "naming-rule-no-ai-tool-names",
        "cli-wrapper-fallback-discipline",
        "destructive-ops-probe-first",
    }
    low_priority = {
        "audit-trail-discipline",
        "approval-workflow-when-it-lands",
    }

    for slug in high_priority:
        assert priority_by_slug[slug] == 100, (
            f"slug {slug!r} should be priority=100 (unrecoverable breach tier)"
        )
    for slug in medium_priority:
        assert priority_by_slug[slug] == 50, (
            f"slug {slug!r} should be priority=50 (recoverable-but-costly tier)"
        )
    for slug in low_priority:
        assert priority_by_slug[slug] == 10, (
            f"slug {slug!r} should be priority=10 (aspirational tier)"
        )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Re-running ``upgrade()`` against a seeded DB is a no-op.

    The interesting assertion is the *narrower* "the migration's upsert
    + ON CONFLICT DO NOTHING shape is filter-shaped, not stamp-shaped":
    running ``upgrade()`` against an already-migrated DB is safe even
    outside Alembic's revision gate. The test exercises that by
    stamping back to 0017 and replaying upgrade -- the row count must
    not change, and no duplicate history rows must land.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0018")

    tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id is not None

    first_rows = _fetch_convention_rows(sync_url, tenant_id)
    assert len(first_rows) == 8

    # Replay -- stamp back to 0017 so the data path actually re-runs,
    # then run the seed migration again.
    command.stamp(cfg, "0017")
    command.upgrade(cfg, "0018")

    second_rows = _fetch_convention_rows(sync_url, tenant_id)
    assert len(second_rows) == 8, (
        "second invocation must not insert duplicate conventions -- "
        "the ON CONFLICT (tenant_id, slug) DO NOTHING shape filters them out"
    )
    # The convention ids must be identical between the two passes --
    # the existing rows survived and no fresh inserts landed.
    assert {str(r["id"]) for r in first_rows} == {str(r["id"]) for r in second_rows}

    # And no duplicate history rows landed -- the RETURNING-id gate in
    # the migration's upgrade() skips the history write when the
    # convention insert is skipped.
    for row in second_rows:
        history = _fetch_history_rows(sync_url, str(row["id"]))
        assert len(history) == 1, (
            f"second invocation must not duplicate history rows for slug "
            f"{row['slug']!r}; got {len(history)} (expected 1)"
        )


def test_pre_existing_tenant_is_upserted_not_recreated(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A tenant row authored manually pre-seed keeps its id; only name refreshes.

    The migration uses ``ON CONFLICT (slug) DO UPDATE SET name =
    EXCLUDED.name``, so an operator who manually created the
    ``rdc-internal`` tenant (chassis-era bootstrap, prior dev
    fixture) keeps their row's id. The conventions seed proceeds
    against that pre-existing id.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0017")  # all tables created, no seed yet

    pre_existing_id = uuid.uuid4()
    _insert_tenant_row(
        sync_url,
        tenant_id=pre_existing_id,
        slug=_TENANT_SLUG,
        name="Operator's Custom Name",
    )

    command.upgrade(cfg, "0018")

    tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id == pre_existing_id.hex, (
        "the pre-existing tenant row's id must survive the upsert -- "
        "ON CONFLICT (slug) DO UPDATE preserves the row, only refreshes name"
    )

    # The 8 conventions land against that pre-existing tenant id.
    rows = _fetch_convention_rows(sync_url, pre_existing_id.hex)
    assert {str(r["slug"]) for r in rows} == _EXPECTED_SLUGS


def test_operator_curated_convention_survives_seed(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A convention an operator authored pre-seed under a seeded slug is preserved.

    Contract: ``ON CONFLICT (tenant_id, slug) DO NOTHING`` -- the seed
    never overwrites operator-authored content even when their slug
    happens to collide with one the seed would write. The
    operator-authored row's body, title, priority, and created_by_sub
    survive verbatim. No synthetic seed history row is added because
    the RETURNING-id gate yields nothing on the skipped insert.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0017")

    operator_tenant_id = uuid.uuid4()
    _insert_tenant_row(
        sync_url,
        tenant_id=operator_tenant_id,
        slug=_TENANT_SLUG,
        name="Operator Tenant",
    )

    operator_body = (
        "Operator-curated override for the vault-canonical convention -- do not auto-rewrite."
    )
    operator_sub = "user:operator@example.com"
    operator_convention_id = _insert_convention_row(
        sync_url,
        tenant_id=operator_tenant_id,
        slug="vault-canonical",
        title="Operator's title",
        body=operator_body,
        created_by_sub=operator_sub,
        priority=42,
    )

    command.upgrade(cfg, "0018")

    # The operator's row survives unchanged.
    rows = _fetch_convention_rows(sync_url, operator_tenant_id.hex)
    operator_row = next(r for r in rows if r["slug"] == "vault-canonical")
    assert str(operator_row["id"]) == operator_convention_id.hex, (
        "operator-authored row id must survive -- ON CONFLICT DO NOTHING "
        "skips the seed insert, the operator's row stays put"
    )
    assert operator_row["body"] == operator_body
    assert operator_row["created_by_sub"] == operator_sub
    assert int(str(operator_row["priority"])) == 42

    # And no synthetic seed history row landed against the operator's
    # convention -- the RETURNING-id gate yields zero rows when the
    # insert was skipped, so the history-write branch never fires.
    history = _fetch_history_rows(sync_url, operator_convention_id.hex)
    assert history == [], (
        "no seed history row may land against an operator-authored "
        "convention -- lying about the lineage of a row the operator owns"
    )

    # Sanity: the other 7 seeded conventions did land against the
    # operator's tenant.
    other_slugs = {str(r["slug"]) for r in rows} - {"vault-canonical"}
    assert other_slugs == _EXPECTED_SLUGS - {"vault-canonical"}


def test_downgrade_removes_seed_keeps_tenant_and_operator_history(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0017"`` removes seeded rows; tenant + operator edits survive.

    Asserts the full reversibility contract:

    * The 8 seeded conventions are removed.
    * The seed-authored history rows are removed.
    * The ``rdc-internal`` tenant row survives (other v0.2 features key
      on tenant_id; cascading the delete would orphan that data).
    * An operator-authored history row (against a seeded slug,
      simulating a post-seed PATCH) survives the downgrade -- the
      ``actor_sub`` narrowing in ``downgrade()`` keeps operator
      history untouched.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0018")

    tenant_id_str = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id_str is not None

    rows_before_operator_edit = _fetch_convention_rows(sync_url, tenant_id_str)
    target_row = next(r for r in rows_before_operator_edit if r["slug"] == "vault-canonical")

    # Simulate an operator PATCH after the seed: write a second
    # history row against the seeded convention. The downgrade must
    # leave this row alone (it carries an operator JWT sub, not the
    # seed marker).
    operator_history_id = _insert_history_row(
        sync_url,
        convention_id=uuid.UUID(str(target_row["id"])),
        body_before=str(target_row["body"]),
        body_after="Operator's amended body",
        actor_sub="user:operator@example.com",
    )

    command.downgrade(cfg, "0017")

    # Tenant survives.
    surviving_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert surviving_tenant_id == tenant_id_str, (
        "rdc-internal tenant must survive downgrade -- other v0.2 "
        "features key on tenant_id and dropping it would orphan data"
    )

    # Seeded convention rows are gone.
    surviving_rows = _fetch_convention_rows(sync_url, tenant_id_str)
    assert surviving_rows == [], "all 8 seed-authored conventions must be removed on downgrade"

    # Operator-authored history row survived (under the deleted
    # convention's id -- the soft-FK lets the row exist orphaned;
    # the downgrade's narrowing predicate kept it because the
    # actor_sub did not match the seed marker).
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id FROM tenant_convention_history
                    WHERE id = :id
                    """,
                ),
                {"id": operator_history_id.hex},
            ).first()
    finally:
        sync_eng.dispose()
    assert row is not None, (
        "operator-authored history row must survive downgrade -- the "
        "actor_sub narrowing in downgrade() keeps operator content "
        "untouched even when the parent convention was removed"
    )


def test_seed_conventions_fit_individual_token_budget(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Each seeded body fits the individual 600-token write-time gate.

    The T2 POST/PATCH route rejects a single ``operational`` body whose
    estimated token cost exceeds
    :data:`~meho_backplane.conventions.schemas.DEFAULT_MAX_PREAMBLE_TOKENS`.
    The seed migration writes directly to the DB and bypasses the API
    gate, but its bodies must still respect the budget so a future
    PATCH against a seeded body (without changing the body itself)
    doesn't trip 422 at validation time. This is a regression guard
    on the seed prose.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0018")

    from meho_backplane.conventions.schemas import (
        DEFAULT_MAX_PREAMBLE_TOKENS,
        estimate_tokens,
    )

    tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert tenant_id is not None
    rows = _fetch_convention_rows(sync_url, tenant_id)

    for row in rows:
        body = str(row["body"])
        estimated = estimate_tokens(body)
        assert estimated <= DEFAULT_MAX_PREAMBLE_TOKENS, (
            f"seeded body for slug {row['slug']!r} estimates "
            f"{estimated} tokens, exceeding the per-entry budget of "
            f"{DEFAULT_MAX_PREAMBLE_TOKENS}; a future PATCH against "
            f"this slug would trip 422 at the API validation gate"
        )
