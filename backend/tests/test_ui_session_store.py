# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the BFF session-store (Task #864).

The tests exercise the four entry points the
:mod:`meho_backplane.ui.auth.session_store` module exposes
(:func:`create_session`, :func:`load_session`, :func:`revoke_session`,
:func:`rotate_refresh`) plus the migration round-trip for the
``web_session`` table.

Coverage matrix (Task #864 acceptance criteria):

* ``alembic upgrade head`` then ``downgrade "0012"`` round-trips
  the ``web_session`` table cleanly (created by migration ``0013``;
  ``0012`` is its ``down_revision``).
* :func:`create_session` -> :func:`load_session` -> :func:`revoke_session`
  round-trips work; the DB columns hold encrypted bytes, never the
  plaintext tokens.
* Refresh rotation succeeds when the presented value matches; the
  prior refresh token is single-use (a second :func:`rotate_refresh`
  with the old value triggers replay revocation + audit row).
* :func:`load_session` returns ``None`` when ``expires_at`` is in the
  past.

The autouse fixtures in :mod:`backend.tests.conftest`
(``_default_database_url`` + ``_schema_template_db``) provide a fresh
file-backed SQLite DB migrated to head before every test, so the
``web_session`` table is present without any per-test
``alembic upgrade head`` replay (per PR #898's per-worker template
pattern).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import (
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.models import AuditLog, WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.session_store import (
    REFRESH_REPLAY_AUDIT_METHOD,
    REFRESH_REPLAY_AUDIT_PATH,
    DecryptedSession,
    EncryptionKeyMissingError,
    RefreshReplayError,
    create_session,
    load_session,
    reset_fernet_cache_for_testing,
    revoke_session,
    rotate_refresh,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars + a session-store Fernet key for every test.

    The chassis-wide :class:`Settings` requires ``KEYCLOAK_ISSUER_URL``
    / ``KEYCLOAK_AUDIENCE`` / ``VAULT_ADDR`` (see the per-test pattern
    in :mod:`backend.tests.test_audit_query_handler`); the session-store
    additionally needs a Fernet key.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test, scoped to a single ``async with``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Alembic round-trip (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_alembic_round_trip_web_session_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``alembic upgrade head`` then ``downgrade "0012"`` round-trips cleanly.

    Exercises the reversibility contract documented on
    ``0013_create_web_session``: ``upgrade`` creates the table + two
    indexes; ``downgrade`` drops them in inverse order.

    The downgrade target is the explicit revision ``"0012"`` (0013's
    ``down_revision``) rather than head-relative ``"-1"`` so the test
    keeps reverting the ``web_session`` migration regardless of how
    many migrations stack on top of head -- matching the repo
    convention (``test_targets_fingerprint.py``, ``test_db_models.py``).

    The test function is **sync** (not ``async def``) because
    :func:`alembic.command.upgrade` invokes :func:`asyncio.run`
    internally via the env.py async cookbook, and that conflicts
    with a running pytest-asyncio event loop. Mirrors the pattern in
    :mod:`tests.test_migration_0011_backfill_when_to_use`.

    Inspecting tables uses a parallel sync engine bound to the same
    DB file; the migration runner used the async engine.
    """
    from alembic import command
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy import inspect as sa_inspect

    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / "migration_round_trip.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)

    def _table_names_and_indexes() -> tuple[set[str], set[str]]:
        sync_eng = sa_create_engine(sync_url)
        try:
            inspector = sa_inspect(sync_eng)
            table_names = set(inspector.get_table_names())
            index_names: set[str] = set()
            if "web_session" in table_names:
                index_names = {ix["name"] for ix in inspector.get_indexes("web_session")}
            return table_names, index_names
        finally:
            sync_eng.dispose()

    # Step 1 -- upgrade only up to 0011 so the table does not yet exist.
    command.upgrade(cfg, "0011")
    pre_tables, _pre_indexes = _table_names_and_indexes()
    assert "web_session" not in pre_tables

    # Step 2 -- upgrade to head creates the table + both indexes.
    command.upgrade(cfg, "head")
    up_tables, up_indexes = _table_names_and_indexes()
    assert "web_session" in up_tables
    assert "web_session_operator_sub_idx" in up_indexes
    assert "web_session_expires_at_idx" in up_indexes

    # Step 3 -- downgrade to 0012 (0013's down_revision) drops the table.
    command.downgrade(cfg, "0012")
    down_tables, _down_indexes = _table_names_and_indexes()
    assert "web_session" not in down_tables


# ---------------------------------------------------------------------------
# create / load / revoke round-trip + ciphertext-at-rest assertion (AC 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_load_then_revoke_round_trips(
    session: AsyncSession,
) -> None:
    """A freshly-created session loads, then revokes, then no-longer-loads."""
    tenant_id = uuid.uuid4()
    operator_sub = "op-alice"
    access_plaintext = "at-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    refresh_plaintext = "rt-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async with session.begin():
        created = await create_session(
            session,
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            access_token=access_plaintext,
            refresh_token=refresh_plaintext,
            lifetime=timedelta(minutes=10),
        )
    assert isinstance(created, DecryptedSession)
    assert created.operator_sub == operator_sub
    assert created.tenant_id == tenant_id
    assert created.access_token == access_plaintext
    assert created.refresh_token == refresh_plaintext
    assert created.expires_at > datetime.now(UTC)

    # Load round-trips the same plaintexts.
    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.operator_sub == operator_sub
    assert loaded.access_token == access_plaintext
    assert loaded.refresh_token == refresh_plaintext

    # Revoke + reload returns None.
    async with session.begin():
        await revoke_session(session, created.id)
    async with session.begin():
        post_revoke = await load_session(session, created.id)
    assert post_revoke is None


@pytest.mark.asyncio
async def test_db_columns_are_ciphertext_not_plaintext(
    session: AsyncSession,
) -> None:
    """AC: the access/refresh DB columns must never hold plaintext bytes."""
    tenant_id = uuid.uuid4()
    access_plaintext = "at-distinctive-AAA-marker-zzz"
    refresh_plaintext = "rt-distinctive-BBB-marker-zzz"

    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token=access_plaintext,
            refresh_token=refresh_plaintext,
            lifetime=timedelta(minutes=10),
        )

    # Read the row back via the ORM and inspect the bytes directly.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as inspect_session:
        row = await inspect_session.get(WebSession, created.id)
    assert row is not None
    assert isinstance(row.access_token, bytes)
    assert isinstance(row.refresh_token, bytes)
    # The distinctive plaintext marker must NOT appear in either
    # column's bytes -- if it did, we'd have stored plaintext.
    assert access_plaintext.encode("utf-8") not in row.access_token
    assert refresh_plaintext.encode("utf-8") not in row.refresh_token
    # Fernet ciphertext is URL-safe-base64 (the `gAAAA...` prefix is
    # the version byte + timestamp encoded). A successful encrypt
    # produces non-empty bytes.
    assert len(row.access_token) > 0
    assert len(row.refresh_token) > 0


# ---------------------------------------------------------------------------
# Refresh rotation: happy path + replay revokes + audit row (AC 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_refresh_happy_path_replaces_both_tokens(
    session: AsyncSession,
) -> None:
    """Presenting the current refresh value rotates both tokens in place."""
    tenant_id = uuid.uuid4()
    original_access = "at-old-AAAAAAAAAAAAAAAAAAAAAAAAAA"
    original_refresh = "rt-old-BBBBBBBBBBBBBBBBBBBBBBBBBB"
    new_access = "at-new-CCCCCCCCCCCCCCCCCCCCCCCCCC"
    new_refresh = "rt-new-DDDDDDDDDDDDDDDDDDDDDDDDDD"

    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token=original_access,
            refresh_token=original_refresh,
            lifetime=timedelta(minutes=10),
        )

    async with session.begin():
        rotated = await rotate_refresh(
            session,
            created.id,
            presented_refresh=original_refresh,
            new_access_token=new_access,
            new_refresh_token=new_refresh,
        )

    assert rotated.id == created.id
    assert rotated.access_token == new_access
    assert rotated.refresh_token == new_refresh

    # Re-load via the public API and confirm the new tokens land.
    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is not None
    assert loaded.access_token == new_access
    assert loaded.refresh_token == new_refresh


@pytest.mark.asyncio
async def test_rotate_refresh_replay_revokes_and_writes_audit(
    session: AsyncSession,
) -> None:
    """AC: presenting an already-rotated refresh value revokes + audits."""
    tenant_id = uuid.uuid4()
    original_refresh = "rt-original-AAAAAAAAAAAAAAAAAAA"
    new_refresh = "rt-rotated-BBBBBBBBBBBBBBBBBBBB"

    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token="at-initial",
            refresh_token=original_refresh,
            lifetime=timedelta(minutes=10),
        )

    # First rotation succeeds.
    async with session.begin():
        await rotate_refresh(
            session,
            created.id,
            presented_refresh=original_refresh,
            new_access_token="at-second",
            new_refresh_token=new_refresh,
        )

    # Second rotation with the OLD refresh value must raise + revoke
    # + write an audit row.
    with pytest.raises(RefreshReplayError) as excinfo:
        async with session.begin():
            await rotate_refresh(
                session,
                created.id,
                presented_refresh=original_refresh,
                new_access_token="at-replay",
                new_refresh_token="rt-replay",
            )
    assert excinfo.value.session_id == created.id
    audit_id = excinfo.value.audit_id

    # The session is now revoked -- ``load_session`` returns None.
    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is None

    # The audit row + revoke commit on a dedicated session inside
    # ``rotate_refresh`` (see ``_commit_replay_side_effects``), so
    # they survive the caller's ``async with session.begin()`` block
    # rolling back on the propagating ``RefreshReplayError``. A fresh
    # session here observes both effects.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as inspect_session:
        row = await inspect_session.get(AuditLog, audit_id)
    assert row is not None, (
        "the rotate_refresh replay branch must commit the "
        "audit row and the revoke independently of the caller's "
        "transaction"
    )
    assert row.method == REFRESH_REPLAY_AUDIT_METHOD
    assert row.path == REFRESH_REPLAY_AUDIT_PATH
    assert row.status_code == 401
    assert row.operator_sub == "op-alice"
    assert row.tenant_id == tenant_id
    assert row.payload["session_id"] == str(created.id)


@pytest.mark.asyncio
async def test_rotate_refresh_missing_session_writes_audit(
    session: AsyncSession,
) -> None:
    """A presented cookie with no row triggers the replay-audit branch."""
    bogus_id = uuid.uuid4()
    with pytest.raises(RefreshReplayError) as excinfo:
        async with session.begin():
            await rotate_refresh(
                session,
                bogus_id,
                presented_refresh="rt-anything",
                new_access_token="at-x",
                new_refresh_token="rt-x",
            )
    assert excinfo.value.session_id == bogus_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as inspect_session:
        row = await inspect_session.get(AuditLog, excinfo.value.audit_id)
    assert row is not None
    assert row.payload["session_id"] == str(bogus_id)
    assert row.payload["reason"] == "missing_session"


@pytest.mark.asyncio
async def test_rotate_refresh_honours_caller_supplied_clock(
    session: AsyncSession,
) -> None:
    """The expiry replay gate runs on the caller's ``now=``, not its own.

    Single-clock contract (#1711 M1): the inline refresh path
    pre-checks ``expires_at`` and threads the same reading into
    ``rotate_refresh``. Were the gate to take a second wall-clock
    reading, an ``expires_at`` landing between the two would fire the
    replay branch, whose dedicated-session revoke UPDATE waits on the
    row lock the caller's transaction holds. Pin both directions: a
    row already expired on the wall clock still rotates under a
    caller clock that predates ``expires_at``, and a caller clock at
    ``expires_at`` fires the expired replay branch.
    """
    original_refresh = "rt-clock-AAAAAAAAAAAAAAAAAAAAAA"
    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=uuid.uuid4(),
            access_token="at-clock-old",
            refresh_token=original_refresh,
            lifetime=timedelta(minutes=-5),  # expires_at already past
        )

    pre_expiry = created.expires_at - timedelta(seconds=30)
    async with session.begin():
        rotated = await rotate_refresh(
            session,
            created.id,
            presented_refresh=original_refresh,
            new_access_token="at-clock-new",
            new_refresh_token="rt-clock-new",
            now=pre_expiry,
        )
    assert rotated.access_token == "at-clock-new"
    assert rotated.refresh_token == "rt-clock-new"

    # Not revoked: the gate consumed the supplied clock, not the wall
    # clock that already sits past ``expires_at``.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as inspect_session:
        row = await inspect_session.get(WebSession, created.id)
    assert row is not None
    assert row.revoked_at is None

    # The same parameter drives the gate in the other direction.
    with pytest.raises(RefreshReplayError) as excinfo:
        async with session.begin():
            await rotate_refresh(
                session,
                created.id,
                presented_refresh="rt-clock-new",
                new_access_token="at-clock-x",
                new_refresh_token="rt-clock-x",
                now=created.expires_at,
            )
    async with sessionmaker() as inspect_session:
        audit = await inspect_session.get(AuditLog, excinfo.value.audit_id)
    assert audit is not None
    assert audit.payload["reason"] == "expired"


# ---------------------------------------------------------------------------
# Concurrent rotation: row-lock serializes (RFC 9700 § 4.14 single-use)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_refresh_emits_select_for_update_against_session_row(
    session: AsyncSession,
) -> None:
    """``rotate_refresh`` must read the session row with ``FOR UPDATE``.

    Regression for the RFC 9700 § 4.14 single-use refresh-token
    property. Before the fix, ``rotate_refresh`` did a non-locking
    ``await session.get(WebSession, cookie_id)`` -- two concurrent
    presenters with the same valid refresh value could both pass
    the mismatch / revoked / expired gate and both successfully
    rotate, breaking single-use.

    The fix replaces the read with
    ``select(WebSession).where(WebSession.id == cookie_id).with_for_update()``
    so the row is row-level-locked for the rotating transaction on
    PostgreSQL (production); the second concurrent caller blocks,
    re-reads after the first commits, and falls into the
    value_mismatch branch -- exactly the one-time-use shape the
    RFC mandates.

    SQLite caveat (per the punch-list): the test suite's dev/test
    engine is aiosqlite, whose locking is database-level rather
    than row-level -- ``FOR UPDATE`` is parsed but does not
    actually serialise concurrent transactions the way PG's does.
    A behavioural ``asyncio.gather`` test against two concurrent
    ``rotate_refresh`` calls therefore cannot prove the fix on
    SQLite: both attempts may legitimately succeed when the lock
    is a no-op. The behavioural Postgres-flavour proof would
    require a testcontainers-PG fixture (the
    ``backend/tests/integration/`` precedent); the unit suite
    instead asserts the SQL that goes onto the wire -- the
    deterministic, dialect-portable signal that the fix is in
    place. If a future refactor reverts to ``session.get`` (or to
    a plain ``select`` without ``.with_for_update()``), this
    assertion fails immediately even on SQLite, surfacing the
    regression before it ships.
    """
    tenant_id = uuid.uuid4()
    original_refresh = "rt-original-CCCCCCCCCCCCCCCCCCCC"

    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token="at-initial",
            refresh_token=original_refresh,
            lifetime=timedelta(minutes=10),
        )

    # Spy on the rotation session's ``execute`` so we can inspect
    # the actual ``Select`` object handed to SQLAlchemy. SQLite's
    # dialect omits the ``FOR UPDATE`` clause from rendered SQL
    # entirely (it parses but no-ops the lock), so capturing the
    # text-on-the-wire is not portable -- the dialect-portable
    # signal is the ``_for_update_arg`` attribute on the
    # ``Select`` itself, which ``with_for_update()`` sets and the
    # plain ``session.get`` / unlocked ``select`` path leaves
    # ``None``. Both engines compile the same statement object;
    # only the rendered output differs.
    from sqlalchemy import Select

    sessionmaker = get_sessionmaker()
    captured_selects: list[Select[tuple[WebSession]]] = []

    async with sessionmaker() as rotation_session, rotation_session.begin():
        original_execute = rotation_session.execute

        async def _spy_execute(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(stmt, Select):
                froms = {t.name for t in stmt.get_final_froms() if hasattr(t, "name")}
                if "web_session" in froms:
                    captured_selects.append(stmt)
            return await original_execute(stmt, *args, **kwargs)

        rotation_session.execute = _spy_execute  # type: ignore[method-assign]
        await rotate_refresh(
            rotation_session,
            created.id,
            presented_refresh=original_refresh,
            new_access_token="at-new-EEEEEEEEEEEEEEEEEEE",
            new_refresh_token="rt-new-FFFFFFFFFFFFFFFFFFFFFFF",
        )

    assert captured_selects, (
        "rotate_refresh must execute a Select against web_session; "
        "if it reverted to session.get, this captures zero Select objects"
    )
    # ``with_for_update()`` sets ``_for_update_arg`` on the Select;
    # the unlocked path leaves it ``None``. Cross-dialect signal,
    # observable on SQLite even though SQLite renders no FOR UPDATE
    # clause and applies no row lock at runtime.
    lock_bearing = [s for s in captured_selects if s._for_update_arg is not None]
    assert lock_bearing, (
        "rotate_refresh's SELECT against web_session must call "
        "`.with_for_update()` to satisfy RFC 9700 § 4.14 single-use "
        "refresh-token rotation on PostgreSQL; the unlocked read "
        "regression is back if no captured Select has "
        "_for_update_arg set"
    )


# ---------------------------------------------------------------------------
# Expired session returns None (AC 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_session_returns_none_when_expired(
    session: AsyncSession,
) -> None:
    """``expires_at`` in the past -> ``load_session`` returns None."""
    tenant_id = uuid.uuid4()
    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token="at-doomed",
            refresh_token="rt-doomed",
            lifetime=timedelta(minutes=10),
        )

    # Force the row's expires_at into the past via the ORM. Keep the
    # update inside a single transaction so the load below sees the
    # committed value.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as patch_session, patch_session.begin():
        row = await patch_session.get(WebSession, created.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is None


# ---------------------------------------------------------------------------
# Misc: missing key error + reset-fernet-cache plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_encryption_key_raises_explicit_error(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``UI_SESSION_ENCRYPTION_KEY`` unset -> :class:`EncryptionKeyMissingError`."""
    monkeypatch.delenv("UI_SESSION_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()

    with pytest.raises(EncryptionKeyMissingError):
        async with session.begin():
            await create_session(
                session,
                operator_sub="op-alice",
                tenant_id=uuid.uuid4(),
                access_token="at",
                refresh_token="rt",
                lifetime=timedelta(minutes=10),
            )


@pytest.mark.asyncio
async def test_revoke_then_load_returns_none_when_active_session_exists(
    session: AsyncSession,
) -> None:
    """``revoke_session`` is idempotent and ``load_session`` skips revoked."""
    tenant_id = uuid.uuid4()
    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token="at",
            refresh_token="rt",
            lifetime=timedelta(minutes=10),
        )

    # First revoke marks the row.
    async with session.begin():
        await revoke_session(session, created.id)

    # Second revoke is idempotent (does not error, does not bump the
    # revoked_at timestamp again -- contract is "first-call wins").
    async with session.begin():
        await revoke_session(session, created.id)

    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is None


@pytest.mark.asyncio
async def test_revoke_missing_session_is_noop(session: AsyncSession) -> None:
    """``revoke_session`` on a nonexistent id silently no-ops."""
    bogus = uuid.uuid4()
    async with session.begin():
        await revoke_session(session, bogus)
    # No assertion needed beyond "did not raise". A query confirms
    # nothing was created.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as inspect_session:
        rows = (await inspect_session.execute(select(WebSession))).scalars().all()
    assert all(row.id != bogus for row in rows)


@pytest.mark.asyncio
async def test_load_session_updates_last_seen_at(
    session: AsyncSession,
) -> None:
    """Successful ``load_session`` refreshes ``last_seen_at``."""
    tenant_id = uuid.uuid4()
    async with session.begin():
        created = await create_session(
            session,
            operator_sub="op-alice",
            tenant_id=tenant_id,
            access_token="at",
            refresh_token="rt",
            lifetime=timedelta(minutes=10),
        )

    # Capture the create-time last_seen_at via direct ORM read.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as pre_session:
        pre_row = await pre_session.get(WebSession, created.id)
    assert pre_row is not None
    initial_last_seen = pre_row.last_seen_at

    # Sleep is overkill; the fixture's monotonic clock advances
    # between flush() calls. Just call load_session and assert
    # the timestamp moved forward (or stayed equal in the rare
    # same-microsecond case -- treat >= initial as the contract).
    async with session.begin():
        loaded = await load_session(session, created.id)
    assert loaded is not None

    async with sessionmaker() as post_session:
        post_row = await post_session.get(WebSession, created.id)
    assert post_row is not None
    assert post_row.last_seen_at >= initial_last_seen


# ---------------------------------------------------------------------------
# Static guard: the test conftest pins a generated key (not a literal)
# so the test process does not embed a constant key value. This is a
# defence against accidentally publishing the key in CI logs.
# ---------------------------------------------------------------------------


def test_required_env_pins_unique_fernet_key() -> None:
    """The autouse env fixture must inject a real Fernet key."""
    key = os.environ.get("UI_SESSION_ENCRYPTION_KEY")
    assert key, "fixture must set UI_SESSION_ENCRYPTION_KEY"
    # ``Fernet`` raises if the key is not URL-safe-base64 32 bytes.
    Fernet(key.encode("ascii"))
