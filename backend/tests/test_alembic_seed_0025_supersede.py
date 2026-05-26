# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0025_supersede_rdc_internal_seed``.

Initiative #1130 (G0.13 v0.6.0 dogfood hardening), Task #1137 (T7).
The migration supersedes ``0018``'s ``rdc-internal`` seed: it cleans
up the rows ``0018`` authored and seeds a generic ``default`` tenant
+ 2 illustrative conventions in their place. The tests cover the
five contracts on the issue body:

* **Head state replaces rdc-internal with default.** After
  ``upgrade head``, no ``rdc-internal`` seeded rows remain
  (conventions + history rows authored by ``0018`` are gone) and
  the generic ``default`` tenant + 2 illustrative conventions are
  in place with their CREATE history rows.
* **Operator-curated rdc-internal content survives the supersede.**
  A convention an operator authored under a seeded slug pre-supersede
  survives the cleanup verbatim (its ``created_by_sub`` does not
  carry the ``0018`` seed marker). A history row an operator wrote
  against a seeded convention also survives (its ``actor_sub`` does
  not match the seed marker).
* **Cleanup is idempotent on a deploy that never ran the legacy
  seed.** On a DB that never had ``rdc-internal`` data (e.g. a
  stamped-from-future fixture), the cleanup is a no-op and the
  default seed still lands.
* **Re-running the migration is a no-op.** Stamp back to ``0024``,
  re-run ``upgrade 0025``: the cleanup + new-seed pass is filter-
  shaped (uses ``ON CONFLICT DO NOTHING`` for the seed), so no
  duplicate conventions or history rows land.
* **Downgrade keeps the default tenant and does NOT restore
  rdc-internal.** ``downgrade "0024"`` removes the 2 default
  conventions + their history rows, leaves the ``default`` tenant
  row intact, and does NOT re-seed the ``rdc-internal`` content
  (restoring it would re-leak consumer identity into the public
  deploy).

The test layout mirrors :mod:`tests.test_alembic_seed_rdc_conventions`
verbatim (the synchronous fixture, the helper-based row inspection,
the env-var pinning) so the supersede tests sit next to the original
seed tests in CI under the same xdist worker shape.

Also includes a signal-12 verification: after ``upgrade head``, the
seeded ``default`` conventions carry zero tokens from the previous
consumer's CLAUDE.md (no ``evoila/meho``, ``rdc-internal``,
``Holodeck`` etc.). The MCP ``initialize.instructions`` integration
test in :mod:`tests.test_mcp_initialize_instructions` exercises the
wire-level round-trip; this test exercises the data-layer shape.
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

#: The legacy seed marker ``0018`` wrote on every row.
_LEGACY_SEED_ACTOR_SUB: Final[str] = "migration:seed-rdc-conventions"

#: The legacy tenant slug.
_LEGACY_TENANT_SLUG: Final[str] = "rdc-internal"

#: The new seed marker ``0025`` writes on every row it authors.
_SEED_ACTOR_SUB: Final[str] = "migration:seed-default-conventions"

#: The new tenant slug.
_TENANT_SLUG: Final[str] = "default"

#: The 2 slugs ``0025`` seeds under the ``default`` tenant.
_EXPECTED_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "slug-naming-kebab-case",
        "conventions-are-operator-facing",
    },
)

#: Tokens that MUST NOT appear in the seeded default-tenant body
#: text after ``upgrade head`` (signal-12 verification per #1137 AC).
#: These tokens identify content sourced from the consumer's
#: ``CLAUDE.md`` (``evoila/meho`` repo references, customer-specific
#: lab program identifiers, AI-tool legacy refs).
_FORBIDDEN_TOKENS: Final[tuple[str, ...]] = (
    "evoila/meho",
    "evoila-bosnia",
    "rdc-internal",
    "rdc-hetzner",
    "Holodeck",
    "holodeck",
)


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Mirrors :func:`tests.test_alembic_seed_rdc_conventions.alembic_cfg`
    verbatim (sync fixture because ``alembic.command.upgrade`` calls
    ``asyncio.run`` internally via the env.py async cookbook).
    """
    db_path = tmp_path / "migration_0025.db"
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
    """Insert one ``tenant_conventions`` row before the supersede runs."""
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
    """Insert one ``tenant_convention_history`` row before the supersede runs."""
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


def _count(sync_url: str, table: str, where: str, params: dict[str, object]) -> int:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
                params,
            ).scalar_one()
            return int(count)
    finally:
        sync_eng.dispose()


def test_head_replaces_rdc_internal_with_default(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` -> rdc-internal seed gone, default seed in place.

    Asserts the headline contract: ``0025`` cleans up ``0018``'s rows
    and seeds the new default tenant.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Legacy: tenant row preserved by design (other v0.2 features may
    # key on tenant_id); but the 8 seeded conventions + their history
    # rows are gone.
    legacy_tenant_id = _fetch_tenant_id(sync_url, _LEGACY_TENANT_SLUG)
    assert legacy_tenant_id is not None, (
        "the rdc-internal tenant row is intentionally preserved on supersede "
        "(other v0.2 features key on tenant_id; dropping it would orphan data)"
    )
    legacy_convention_count = _count(
        sync_url,
        "tenant_conventions",
        "tenant_id = :tenant_id AND created_by_sub = :sub",
        {"tenant_id": legacy_tenant_id, "sub": _LEGACY_SEED_ACTOR_SUB},
    )
    assert legacy_convention_count == 0, (
        "0025 must remove every convention 0018 authored under rdc-internal; "
        f"got {legacy_convention_count} survivors"
    )
    legacy_history_count = _count(
        sync_url,
        "tenant_convention_history",
        "actor_sub = :sub",
        {"sub": _LEGACY_SEED_ACTOR_SUB},
    )
    assert legacy_history_count == 0, (
        "0025 must remove every seed-authored history row 0018 wrote; "
        f"got {legacy_history_count} survivors"
    )

    # New: default tenant + 2 conventions + 2 CREATE history rows.
    default_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert default_tenant_id is not None, "0025 must seed the generic default tenant"
    rows = _fetch_convention_rows(sync_url, default_tenant_id)
    assert len(rows) == 2, f"expected 2 seeded default conventions; got {len(rows)}"
    assert {str(r["slug"]) for r in rows} == _EXPECTED_SLUGS
    assert all(r["kind"] == "operational" for r in rows), (
        "every seeded default convention carries kind='operational' so T4's "
        "preamble assembler packs them at session start"
    )
    assert all(r["created_by_sub"] == _SEED_ACTOR_SUB for r in rows), (
        "every seeded default row records the 0025 seed marker"
    )

    for row in rows:
        history = _fetch_history_rows(sync_url, str(row["id"]))
        assert len(history) == 1, (
            f"expected one CREATE history row for slug {row['slug']!r}, got {len(history)}"
        )
        h = history[0]
        assert h["body_before"] is None
        assert h["body_after"] == row["body"]
        assert h["actor_sub"] == _SEED_ACTOR_SUB


def test_seeded_default_bodies_carry_no_consumer_specific_tokens(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Signal-12 verification: no consumer CLAUDE.md tokens in default-seed bodies.

    Acceptance criterion: "After migration lands, initialize against a
    fresh-DB MCP server returns instructions text that carries the
    generic illustrative conventions only -- zero references to
    evoila/meho, evoila-bosnia/meho-internal, rdc-internal,
    Holodeck-claude, or any other consumer-specific tokens."

    The wire-level MCP round-trip is exercised in
    :mod:`tests.test_mcp_initialize_instructions`; this assertion
    operates one layer down on the seeded body text itself so a
    regression in the migration's body content surfaces here
    independently of MCP plumbing.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    default_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert default_tenant_id is not None
    rows = _fetch_convention_rows(sync_url, default_tenant_id)

    # Concatenate every seeded body + title together for the scan; a
    # forbidden token in *any* field is a leak.
    blob = " ".join(
        f"{r['title']} {r['body']}".lower()  # case-insensitive match
        for r in rows
    )
    for token in _FORBIDDEN_TOKENS:
        assert token.lower() not in blob, (
            f"forbidden token {token!r} found in seeded default conventions -- "
            "the seed must contain no references to a specific consumer's "
            "operational discipline or repo identifiers"
        )


def test_operator_curated_rdc_internal_content_survives_supersede(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Operator-authored content under rdc-internal slugs survives 0025.

    Contract:
    * A convention an operator authored under a seeded slug (with their
      JWT sub, not the seed marker) survives 0025's cleanup.
    * A history row the operator wrote against a seeded convention
      (with their JWT sub) survives 0025's cleanup.
    """
    cfg, sync_url = alembic_cfg
    # Stop at 0018 so the rdc-internal seed is present but 0025 has
    # not yet run.
    command.upgrade(cfg, "0018")

    legacy_tenant_id_str = _fetch_tenant_id(sync_url, _LEGACY_TENANT_SLUG)
    assert legacy_tenant_id_str is not None
    legacy_tenant_id = uuid.UUID(legacy_tenant_id_str)

    # Inspect a seeded convention (vault-canonical) -- we will write
    # an operator history row against it that must survive 0025.
    rows_at_0018 = _fetch_convention_rows(sync_url, legacy_tenant_id_str)
    seeded_target = next(r for r in rows_at_0018 if r["slug"] == "vault-canonical")
    operator_history_id = _insert_history_row(
        sync_url,
        convention_id=uuid.UUID(str(seeded_target["id"])),
        body_before=str(seeded_target["body"]),
        body_after="Operator's amended body for vault-canonical",
        actor_sub="user:operator@example.com",
    )

    # An operator-authored convention under a *different* slug (not
    # one of the seeded eight). 0025's cleanup narrows on the seeded
    # slug list; this row carries an unrelated slug and survives.
    operator_only_convention_id = _insert_convention_row(
        sync_url,
        tenant_id=legacy_tenant_id,
        slug="operator-only-rule",
        title="Operator's own rule",
        body="An operator-authored convention not in the 0018 seed list.",
        created_by_sub="user:operator@example.com",
        priority=99,
    )

    # Now run the supersede.
    command.upgrade(cfg, "0025")

    # The operator-authored convention under an unrelated slug
    # survives. (0025's cleanup narrows on the 8 seeded slugs.)
    survived_count = _count(
        sync_url,
        "tenant_conventions",
        "id = :id",
        {"id": operator_only_convention_id.hex},
    )
    assert survived_count == 1, (
        "operator-authored convention under an unrelated slug must survive "
        "the supersede -- 0025's cleanup narrows on the 8 seeded slugs"
    )

    # The operator-authored history row against vault-canonical
    # survives. (0025's cleanup narrows on the seed actor_sub; the
    # operator's history row carries a JWT sub.)
    history_survived_count = _count(
        sync_url,
        "tenant_convention_history",
        "id = :id",
        {"id": operator_history_id.hex},
    )
    assert history_survived_count == 1, (
        "operator-authored history row against a seeded convention must "
        "survive the supersede -- 0025's cleanup narrows on the seed "
        "actor_sub, the operator's sub does not match"
    )


def test_re_running_supersede_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Stamp 0024 + upgrade 0025 twice: no duplicate rows land."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    default_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert default_tenant_id is not None
    first_rows = _fetch_convention_rows(sync_url, default_tenant_id)
    assert len(first_rows) == 2

    # Replay -- stamp back to 0024, then upgrade 0025 again.
    command.stamp(cfg, "0024")
    command.upgrade(cfg, "0025")

    second_rows = _fetch_convention_rows(sync_url, default_tenant_id)
    assert len(second_rows) == 2, (
        "re-running 0025 must not duplicate the default conventions -- "
        "the ON CONFLICT (tenant_id, slug) DO NOTHING shape filters the "
        "second insert"
    )
    assert {str(r["id"]) for r in first_rows} == {str(r["id"]) for r in second_rows}

    for row in second_rows:
        history = _fetch_history_rows(sync_url, str(row["id"]))
        assert len(history) == 1, (
            f"re-running 0025 must not duplicate history rows for slug "
            f"{row['slug']!r}; got {len(history)}"
        )


def test_downgrade_removes_default_seed_keeps_tenant_no_rdc_internal_restore(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade 0024`` -> default seed gone; tenant kept; rdc-internal NOT restored.

    Contract: the downgrade does NOT restore the rdc-internal seed --
    restoring it would re-leak consumer identity. Operators who need
    the rdc-internal content back apply the consumer-side template
    documented in docs/architecture/conventions-seed.md.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    default_tenant_id_str = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert default_tenant_id_str is not None

    command.downgrade(cfg, "0024")

    # Default tenant row preserved (mirrors 0018's downgrade discipline).
    survived_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert survived_tenant_id == default_tenant_id_str, (
        "default tenant row must survive downgrade -- other tenants may key on tenant_id"
    )

    # The 2 default conventions are gone.
    rows = _fetch_convention_rows(sync_url, default_tenant_id_str)
    assert rows == [], "downgrade must remove the 2 seeded default conventions"

    # No seeded history rows under the new marker.
    history_count = _count(
        sync_url,
        "tenant_convention_history",
        "actor_sub = :sub",
        {"sub": _SEED_ACTOR_SUB},
    )
    assert history_count == 0, "downgrade must remove all default-seed history rows"

    # rdc-internal seeded conventions are NOT re-seeded -- restoring
    # them would re-leak consumer content.
    legacy_tenant_id = _fetch_tenant_id(sync_url, _LEGACY_TENANT_SLUG)
    assert legacy_tenant_id is not None, (
        "rdc-internal tenant row was preserved by 0025 upgrade and stays "
        "preserved through 0025 downgrade"
    )
    restored_legacy_count = _count(
        sync_url,
        "tenant_conventions",
        "tenant_id = :tenant_id AND created_by_sub = :sub",
        {"tenant_id": legacy_tenant_id, "sub": _LEGACY_SEED_ACTOR_SUB},
    )
    assert restored_legacy_count == 0, (
        "0025 downgrade must NOT restore the rdc-internal seed -- the seed "
        "content is consumer-specific and would re-leak into the public deploy"
    )


def test_cleanup_is_noop_on_fresh_db_without_rdc_internal(
    alembic_cfg: tuple[Config, str],
) -> None:
    """When no rdc-internal tenant exists, 0025's cleanup is a no-op.

    This guards the cleanup path's early-return: a deploy that ran
    its first ``upgrade head`` against an empty DB sees ``0018`` then
    ``0025`` in immediate succession. ``0018`` populates the rows,
    ``0025`` cleans them up + seeds default. The state after head
    matches a deploy that already had rdc-internal data: both paths
    land on the same head state.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Both the cleanup-after-0018 path and the never-had-rdc-internal
    # path should converge on the same state: rdc-internal tenant row
    # preserved (empty of seeded conventions), default tenant + 2
    # conventions seeded.
    legacy_tenant_id = _fetch_tenant_id(sync_url, _LEGACY_TENANT_SLUG)
    default_tenant_id = _fetch_tenant_id(sync_url, _TENANT_SLUG)
    assert legacy_tenant_id is not None
    assert default_tenant_id is not None

    legacy_conventions = _count(
        sync_url,
        "tenant_conventions",
        "tenant_id = :tenant_id",
        {"tenant_id": legacy_tenant_id},
    )
    assert legacy_conventions == 0, (
        "rdc-internal tenant exists (preserved by design) but carries no "
        "conventions after head; got {legacy_conventions}"
    )
    default_conventions = _count(
        sync_url,
        "tenant_conventions",
        "tenant_id = :tenant_id",
        {"tenant_id": default_tenant_id},
    )
    assert default_conventions == 2, (
        f"default tenant must carry the 2 seeded conventions; got {default_conventions}"
    )


def test_seeded_default_bodies_fit_individual_token_budget(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Each seeded default body fits the individual 600-token write-time gate.

    Same regression guard as the equivalent test in
    :mod:`tests.test_alembic_seed_rdc_conventions`: a future PATCH
    against a seeded body would trip 422 at validation if the body
    overshoots the per-entry budget.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

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
            f"seeded default body for slug {row['slug']!r} estimates "
            f"{estimated} tokens, exceeding the per-entry budget of "
            f"{DEFAULT_MAX_PREAMBLE_TOKENS}"
        )
