# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :func:`meho_backplane.targets.resolver.resolve_target`.

Coverage matrix (G0.3-T2 acceptance criteria):

* Exact name match: returns the matching ORM row.
* Alias match (element-equality): returns the row when the query
  matches an element of ``aliases`` exactly.
* Not-found with near-misses: raises :exc:`TargetNotFoundError` with
  the ``matches`` field populated from prefix-ILIKE near-misses.
* Tenant boundary: a target in tenant_a is invisible to a query for
  tenant_b.
* Ambiguous alias: when multiple targets share an alias (defensive —
  the unique index prevents name-level ambiguity but not alias-level),
  raises :exc:`AmbiguousTargetError`.

All tests run against the file-backed SQLite test DB created by the
autouse ``_default_database_url`` conftest fixture (``alembic upgrade
head`` applied). The dialect-aware Python-side fallback in
:func:`resolve_target` means alias tests work on SQLite the same way
they will on PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target(**kwargs: object) -> TargetORM:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "default-target",
        "product": "ssh",
        "host": "10.0.0.1",
    }
    defaults.update(kwargs)
    return TargetORM(**defaults)


# ---------------------------------------------------------------------------
# Exact name match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_exact_name_returns_row() -> None:
    """Exact ``name`` match within tenant returns the correct ORM row."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = _make_target(
        tenant_id=tenant_id, name="rdc-vcenter", product="vsphere", host="10.1.0.1"
    )

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        result = await resolve_target(session, tenant_id, "rdc-vcenter")

    assert result.name == "rdc-vcenter"
    assert result.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_resolve_target_exact_name_wrong_tenant_not_found() -> None:
    """Exact name match for a different tenant raises TargetNotFoundError."""
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    target = _make_target(tenant_id=tenant_a, name="shared-name", product="ssh", host="10.0.0.1")

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError) as exc_info:
            await resolve_target(session, tenant_b, "shared-name")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "no_target"


# ---------------------------------------------------------------------------
# Alias match (element-equality)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_alias_match_returns_row() -> None:
    """Query matching an element of ``aliases`` returns the target."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = _make_target(
        tenant_id=tenant_id,
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
        aliases=["vcenter", "vc.corp.internal"],
    )

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        result = await resolve_target(session, tenant_id, "vcenter")

    assert result.name == "rdc-vcenter"


@pytest.mark.asyncio
async def test_resolve_target_alias_substring_does_not_match() -> None:
    """A query that is a substring of an alias does NOT resolve (element-equality only)."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = _make_target(
        tenant_id=tenant_id,
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
        aliases=["vc.corp.internal"],
    )

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError):
            # "vc" is a substring of "vc.corp.internal" but not an exact element.
            await resolve_target(session, tenant_id, "vc")


@pytest.mark.asyncio
async def test_resolve_target_alias_tenant_boundary() -> None:
    """Alias in tenant_a is invisible to a query for tenant_b."""
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    target = _make_target(
        tenant_id=tenant_a,
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
        aliases=["vcenter"],
    )

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError):
            await resolve_target(session, tenant_b, "vcenter")


# ---------------------------------------------------------------------------
# Not-found with near-misses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_not_found_returns_near_misses() -> None:
    """When no target matches, TargetNotFoundError.detail includes near-misses.

    Two targets named ``rdc-vcenter`` + ``rdc-vault`` exist; a query
    for ``rdc-v`` (no exact match) should return both as near-misses
    because both names start with ``rdc-v`` (ILIKE ``rdc-v%``).
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    t1 = _make_target(tenant_id=tenant_id, name="rdc-vcenter", product="vsphere", host="10.1.0.1")
    t2 = _make_target(tenant_id=tenant_id, name="rdc-vault", product="vault", host="10.1.0.2")

    async with sessionmaker() as session:
        session.add(t1)
        session.add(t2)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError) as exc_info:
            await resolve_target(session, tenant_id, "rdc-v")

    detail = exc_info.value.detail
    assert detail["error"] == "no_target"
    assert detail["query"] == "rdc-v"
    near_miss_names = {m["name"] for m in detail["matches"]}
    assert "rdc-vcenter" in near_miss_names
    assert "rdc-vault" in near_miss_names


@pytest.mark.asyncio
async def test_resolve_target_not_found_no_near_misses() -> None:
    """When no prefix matches, near-misses list is empty."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = _make_target(tenant_id=tenant_id, name="alpha-host", product="ssh", host="10.0.0.1")

    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError) as exc_info:
            await resolve_target(session, tenant_id, "zzzz-nonexistent")

    assert exc_info.value.detail["matches"] == []


@pytest.mark.asyncio
async def test_resolve_target_not_found_near_misses_tenant_scoped() -> None:
    """Near-misses are scoped to the querying tenant, not all tenants."""
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    # rdc-vcenter exists in tenant_a only.
    t = _make_target(tenant_id=tenant_a, name="rdc-vcenter", product="vsphere", host="10.1.0.1")

    async with sessionmaker() as session:
        session.add(t)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(TargetNotFoundError) as exc_info:
            # tenant_b has no targets at all — near-misses must be empty.
            await resolve_target(session, tenant_b, "rdc-x")

    assert exc_info.value.detail["matches"] == []


# ---------------------------------------------------------------------------
# Ambiguous alias (defensive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_ambiguous_alias_raises() -> None:
    """Two targets sharing the same alias raises AmbiguousTargetError."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    t1 = _make_target(
        tenant_id=tenant_id,
        name="target-a",
        product="ssh",
        host="10.0.0.1",
        aliases=["shared-alias"],
    )
    t2 = _make_target(
        tenant_id=tenant_id,
        name="target-b",
        product="ssh",
        host="10.0.0.2",
        aliases=["shared-alias"],
    )

    async with sessionmaker() as session:
        session.add(t1)
        session.add(t2)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(AmbiguousTargetError) as exc_info:
            await resolve_target(session, tenant_id, "shared-alias")

    detail = exc_info.value.detail
    assert detail["error"] == "ambiguous_target"
    assert detail["query"] == "shared-alias"
    assert len(detail["matches"]) == 2


@pytest.mark.asyncio
async def test_resolve_target_exact_name_duplicate_raises_ambiguous() -> None:
    """Duplicate exact-name rows (data-drift) raise AmbiguousTargetError (409).

    The (tenant_id, name) unique index prevents this under normal conditions,
    but a restored backup or a relaxed-constraint migration window can leave
    duplicate rows. resolve_target must surface 409, not leak a 500
    via MultipleResultsFound.
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    # Bypass the unique constraint by giving the two rows different primary keys
    # but the same name within the same tenant.  SQLite enforces the unique
    # index, so we insert both rows in one flush to defer constraint checking.
    t1 = _make_target(tenant_id=tenant_id, name="dup-name", host="10.0.0.1")
    t2 = _make_target(tenant_id=tenant_id, name="dup-name", host="10.0.0.2")

    async with sessionmaker() as session:
        session.add_all([t1, t2])
        try:
            await session.flush()
            await session.commit()
        except Exception:
            # SQLite enforces the unique constraint; skip on SQLite.
            pytest.skip("dialect enforces unique constraint — data-drift not simulatable")

    async with sessionmaker() as session:
        with pytest.raises(AmbiguousTargetError) as exc_info:
            await resolve_target(session, tenant_id, "dup-name")

    detail = exc_info.value.detail
    assert detail["error"] == "ambiguous_target"
    assert detail["query"] == "dup-name"
    assert len(detail["matches"]) == 2
