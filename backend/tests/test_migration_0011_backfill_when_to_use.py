# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0011_backfill_operation_group_when_to_use``.

Initiative #772 (G0.9.1 v0.3.2 dogfood hardening), Task #774 (T2,
Signal #5 refined). The migration backfills curated ``when_to_use``
strings onto pre-existing ``operation_group`` rows that still hold
the v0.6 substrate's auto-derived template literal -- the gap PR
#731 (kill-switched the default) + #732 (curated source) could not
close because the first-write-wins contract on
:func:`~meho_backplane.operations.typed_register._resolve_or_create_group`
never overwrites the existing row on connector re-registration.

Test matrix
-----------

Five named cases cover the migration's contract:

* **Template row -> curated.** A row whose ``when_to_use`` is the
  exact kill-switched template
  ``"Operations grouped under '<key>' for <product> <impl>."`` is
  rewritten to the curated source string for that natural key.
* **Operator-edited row -> preserved.** A row whose ``when_to_use``
  does *not* start with the template prefix is left untouched, even
  when the ``(product, version, impl_id, group_key)`` matches a
  curated entry. Operator edits made via
  ``meho.connector.edit_group`` survive the upgrade.
* **Tenant-scoped row -> preserved.** A row with
  ``tenant_id IS NOT NULL`` is left untouched even when the
  ``when_to_use`` matches the template prefix -- the migration only
  rewrites built-in / global rows (``tenant_id IS NULL``).
* **Unmapped natural key -> preserved.** A row whose
  ``(product, version, impl_id, group_key)`` isn't in the curated
  payload is left untouched, even when the ``when_to_use`` matches
  the template prefix. Future connectors that hadn't shipped curated
  strings by v0.3.2 keep their template until their own curation
  lands.
* **Idempotency.** Re-running the upgrade after the first run is a
  no-op (the rewritten rows no longer match the template prefix). The
  test exercises this by issuing a second ``upgrade head`` and
  asserting nothing changes (including ``updated_at`` -- a fresh
  no-op pass must not bump the timestamp).

The tests follow the synchronous pattern established by
:mod:`tests.test_db_models` -- ``alembic.command.upgrade`` calls
``asyncio.run`` internally via env.py's async cookbook, so the test
function itself must be sync. SQLite is the test driver; PG-side
shape parity is covered by the testcontainers replay suite in
:mod:`tests.test_migration_rollback` once that suite is extended in
a follow-up to exercise the data migration end-to-end (the unit-
level coverage here proves the SQL is portable and the predicate is
correct -- testcontainers replay would only re-verify the dialect).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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

#: Match the migration's own template-detection predicate. Any row
#: whose ``when_to_use`` starts with this prefix is on the kill-
#: switched template.
_TEMPLATE_PREFIX: Final[str] = "Operations grouped under"

#: One natural-key tuple drawn from the curated payload -- exercises
#: the rewrite path. Bind9's identity group is the smallest curated
#: blurb so failures surface fast in test logs.
_TARGET_NATURAL_KEY: Final[tuple[str, str, str, str]] = (
    "bind9",
    "9.x",
    "bind9-ssh",
    "identity",
)

#: A natural-key tuple deliberately absent from the curated payload.
#: A future connector that ships with the template still in place but
#: hasn't been curated yet should not be touched by this migration --
#: the curation iteration will land its own backfill.
_UNMAPPED_NATURAL_KEY: Final[tuple[str, str, str, str]] = (
    "nsx",
    "4.x",
    "nsx-policy",
    "logical-switch",
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
    :mod:`tests.test_db_models` synchronous.

    The DB file lives under pytest's ``tmp_path`` so each test gets
    an isolated SQLite database; engine + settings caches are reset
    before and after so the alembic env reads *this* DATABASE_URL.
    """
    db_path = tmp_path / "migration_0011.db"
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


def _stamped_template(group_key: str, product: str, impl_id: str) -> str:
    """Render the kill-switched auto-derive template verbatim.

    Mirrors the pre-PR-#731 default literal that lived in
    :func:`~meho_backplane.operations.typed_register._resolve_or_create_group`::

        f"Operations grouped under {group_key!r} for {product} {impl_id}."

    The ``!r`` rendering puts the key in single-quotes, so the
    rendered template reads ``Operations grouped under 'identity'
    for bind9 bind9-ssh.``. Reproducing the exact shape (not just
    "any string starting with the prefix") exercises the predicate
    against realistic v0.3.0-era row contents.
    """
    return f"Operations grouped under '{group_key}' for {product} {impl_id}."


def _insert_operation_group_row(
    sync_url: str,
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
    when_to_use: str,
) -> UUID:
    """Insert one ``operation_group`` row at revision 0010 via raw SQL.

    Bypasses the ORM model (which doesn't exist yet at revision
    0010's schema in the strict sense, but in practice the model
    matches; raw SQL keeps the test independent of any future ORM
    refactor and matches what the migration itself does -- both speak
    SQL through the same SQLAlchemy Core layer).

    Returns the generated UUID so the caller can read the row back
    after the upgrade.
    """
    row_id = uuid4()
    now = datetime.now(UTC)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operation_group (
                        id, tenant_id, product, version, impl_id, group_key,
                        name, when_to_use, review_status, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :product, :version, :impl_id, :group_key,
                        :name, :when_to_use, :review_status, :created_at, :updated_at
                    )
                    """,
                ),
                {
                    "id": str(row_id),
                    "tenant_id": str(tenant_id) if tenant_id is not None else None,
                    "product": product,
                    "version": version,
                    "impl_id": impl_id,
                    "group_key": group_key,
                    # Title-case the key for the placeholder name -- mirrors
                    # the helper's own default; the migration leaves
                    # ``name`` alone, so the test value just needs to be
                    # non-empty.
                    "name": group_key.replace("-", " ").replace("_", " ").title(),
                    "when_to_use": when_to_use,
                    "review_status": "enabled",
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _read_when_to_use(sync_url: str, row_id: UUID) -> tuple[str, str]:
    """Return ``(when_to_use, updated_at)`` for the row, as ISO strings.

    Both columns are returned because the test asserts on
    ``updated_at`` movement (or lack thereof) alongside the
    ``when_to_use`` change.
    """
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT when_to_use, updated_at FROM operation_group WHERE id = :id",
                ),
                {"id": str(row_id)},
            ).one()
            return (str(row.when_to_use), str(row.updated_at))
    finally:
        sync_eng.dispose()


def test_template_row_is_backfilled_to_curated(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Built-in template row -> curated string after upgrade.

    Seeds one ``tenant_id IS NULL`` row at revision 0010 with the
    exact kill-switched template, runs ``upgrade head``, and asserts
    the row's ``when_to_use`` carries the curated text (substring
    match on a load-bearing phrase that only the curated bind9
    identity blurb contains -- pinning the full text would couple
    the test to the curated prose's punctuation).
    """
    cfg, sync_url = alembic_cfg
    # Step 1 -- upgrade to 0010 (the revision before this migration)
    # so the table exists but the data fix hasn't run yet.
    command.upgrade(cfg, "0010")

    product, version, impl_id, group_key = _TARGET_NATURAL_KEY
    row_id = _insert_operation_group_row(
        sync_url,
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        when_to_use=_stamped_template(group_key, product, impl_id),
    )

    # Step 2 -- run the migration under test.
    command.upgrade(cfg, "head")

    when_to_use, _ = _read_when_to_use(sync_url, row_id)
    # Load-bearing phrase from the curated bind9 identity blurb --
    # ``bind9.about`` is the op the blurb names by op_id; no other
    # connector's curated text includes it. Substring match keeps the
    # assertion robust against minor punctuation drift in the curated
    # prose without losing the "yes this is the right blurb" signal.
    assert "bind9.about" in when_to_use
    # And the template is gone -- the predicate was correctly applied.
    assert not when_to_use.startswith(_TEMPLATE_PREFIX)


def test_operator_edited_row_is_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Operator-edited row -> unchanged across upgrade.

    The contract the issue calls out explicitly: rows touched by
    ``meho.connector.edit_group`` must survive the upgrade verbatim.
    The migration's predicate is "starts with the template prefix",
    so any prose written by an operator (which never starts with
    those three words) is left alone. Even when the row's natural
    key matches a curated entry -- the operator's intent wins.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0010")

    product, version, impl_id, group_key = _TARGET_NATURAL_KEY
    edited_text = (
        "Operator-curated override for the bind9 identity group -- "
        "deployed against this Hetzner DC's nameserver pool; do not "
        "auto-rewrite."
    )
    row_id = _insert_operation_group_row(
        sync_url,
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        when_to_use=edited_text,
    )

    command.upgrade(cfg, "head")

    when_to_use, _ = _read_when_to_use(sync_url, row_id)
    assert when_to_use == edited_text, (
        "operator edit must survive the upgrade -- the migration filters on "
        "'starts with the template prefix' so any other prose is preserved"
    )


def test_tenant_scoped_row_is_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Tenant-scoped row -> unchanged even when the prose is the template.

    The migration narrows on ``tenant_id IS NULL`` -- tenant-scoped
    rows are operator-owned by definition (only the ingest /
    edit-group paths create them) and a backfill migration must not
    rewrite operator-owned rows even when their prose happens to look
    template-shaped. This is the same boundary the registration helper
    enforces: tenant-scoped curation is a different track from built-
    in curation.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0010")

    product, version, impl_id, group_key = _TARGET_NATURAL_KEY
    template = _stamped_template(group_key, product, impl_id)
    tenant_id = uuid4()
    row_id = _insert_operation_group_row(
        sync_url,
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        when_to_use=template,
    )

    command.upgrade(cfg, "head")

    when_to_use, _ = _read_when_to_use(sync_url, row_id)
    assert when_to_use == template, (
        "tenant-scoped rows are operator-owned and must not be rewritten by a "
        "backfill migration, even when their prose matches the template "
        "predicate"
    )


def test_unmapped_natural_key_is_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Row outside the curated payload -> unchanged across upgrade.

    A future connector that shipped before its curation landed (or a
    third-party connector loaded from a plugin) keeps its template
    text until its own curation arrives. The migration's natural-key
    table is closed -- it lists exactly the connectors PR #732
    curated (plus the harbor robot group this Initiative adds), so
    nothing else moves.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0010")

    product, version, impl_id, group_key = _UNMAPPED_NATURAL_KEY
    template = _stamped_template(group_key, product, impl_id)
    row_id = _insert_operation_group_row(
        sync_url,
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        when_to_use=template,
    )

    command.upgrade(cfg, "head")

    when_to_use, _ = _read_when_to_use(sync_url, row_id)
    assert when_to_use == template, (
        "rows with a natural key the migration doesn't know about must be "
        "left intact -- their own curation lands in a future migration"
    )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Second ``upgrade head`` is a no-op on already-backfilled rows.

    The predicate ``when_to_use LIKE 'Operations grouped under%'``
    no longer matches after the first run, so re-running the
    migration (or replaying it under the testcontainers cycle suite)
    leaves the row -- and its ``updated_at`` -- untouched.

    Idempotency matters at two layers:

    * ``alembic upgrade head`` itself is idempotent on the second
      call (the revision is already in ``alembic_version``), so the
      data path doesn't actually re-execute through the standard
      path. The interesting assertion is therefore the *narrower*
      "the predicate is filter-shaped, not stamp-shaped": running
      the migration's ``upgrade()`` against an already-migrated DB
      is safe even outside Alembic's revision gate.

    We exercise that by calling the migration's ``upgrade()``
    function directly a second time against the same DB and
    asserting nothing moves.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0010")

    product, version, impl_id, group_key = _TARGET_NATURAL_KEY
    row_id = _insert_operation_group_row(
        sync_url,
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        when_to_use=_stamped_template(group_key, product, impl_id),
    )

    command.upgrade(cfg, "0011")
    curated_text, first_updated_at = _read_when_to_use(sync_url, row_id)
    assert "bind9.about" in curated_text  # sanity: first pass landed

    # Second invocation -- stamp the DB back to 0010 (without
    # rolling the data change back; ``downgrade()`` is a documented
    # no-op so the rewritten row stays curated) and re-run the
    # 0011 upgrade. Alembic now actually re-executes the
    # migration's ``upgrade()`` because the revision is no longer
    # stamped, exercising the SQL on an already-curated row. A
    # filter-shaped predicate is a no-op on the second pass; a
    # stamp-shaped one would clobber ``updated_at`` or worse.
    #
    # Pinning the target to ``0011`` (not ``head``) keeps the replay
    # scoped to the migration under test -- downstream schema
    # migrations (e.g. 0012's ``CREATE TABLE graph_*_history`` and
    # 0013's ``CREATE TABLE web_session``) are not idempotent by
    # design (Alembic gates them on ``alembic_version`` in
    # production) and would raise ``table already exists`` if the
    # second ``upgrade`` walked past 0011.
    command.stamp(cfg, "0010")
    command.upgrade(cfg, "0011")

    second_text, second_updated_at = _read_when_to_use(sync_url, row_id)
    assert second_text == curated_text, "second invocation must not rewrite already-curated rows"
    assert second_updated_at == first_updated_at, (
        "second invocation must not bump updated_at on already-curated rows -- "
        "the predicate filters them out before the UPDATE runs"
    )
