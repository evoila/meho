# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for LicenseAuditRepository against a real Postgres."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from meho_app.core.licensing import LicensePayload
from meho_app.modules.licensing.audit import (
    DuplicateLicenseIDError,
    LicenseAuditRepository,
)
from meho_app.modules.licensing.models import LicenseIssuance


def _payload(
    *,
    license_id: str = "lic-001",
    org: str = "acme",
    tier: str = "enterprise",
    features: list[str] | None = None,
    issued_at: str = "2026-05-01T12:00:00+00:00",
    expires_at: str | None = "2027-05-01T12:00:00+00:00",
    max_tenants: int | None = 10,
) -> LicensePayload:
    return LicensePayload(
        license_id=license_id,
        org=org,
        tier=tier,
        features=features if features is not None else ["multi_tenancy", "sso"],
        issued_at=issued_at,
        expires_at=expires_at,
        max_tenants=max_tenants,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_writes_row(db_session) -> None:
    repo = LicenseAuditRepository(db_session)

    row = await repo.record_issuance(
        _payload(license_id="lic-001"),
        issuer="ops@evoila.com",
        issuer_type="user",
    )

    assert row.license_id == "lic-001"
    assert row.org == "acme"
    assert row.tier == "enterprise"
    assert row.issuer == "ops@evoila.com"
    assert row.issuer_type == "user"

    fetched = await repo.find_by_license_id("lic-001")
    assert fetched is not None
    assert fetched.features == ["multi_tenancy", "sso"]
    assert fetched.issued_at.tzinfo is not None
    assert fetched.expires_at is not None
    assert fetched.created_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_duplicate_license_id(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    payload = _payload(license_id="lic-dup")

    await repo.record_issuance(payload, issuer="ops@evoila.com", issuer_type="user")

    with pytest.raises(DuplicateLicenseIDError):
        await repo.record_issuance(payload, issuer="ops@evoila.com", issuer_type="user")

    count = await db_session.scalar(select(func.count()).select_from(LicenseIssuance))
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_invalid_issued_at(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    bad = _payload(license_id="lic-bad-iat", issued_at="not-a-date")

    with pytest.raises(ValueError, match="issued_at"):
        await repo.record_issuance(bad, issuer="ops", issuer_type="user")

    count = await db_session.scalar(select(func.count()).select_from(LicenseIssuance))
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_invalid_expires_at(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    bad = _payload(
        license_id="lic-bad-exp",
        issued_at="2026-05-01T12:00:00+00:00",
        expires_at="malformed",
    )

    with pytest.raises(ValueError, match="expires_at"):
        await repo.record_issuance(bad, issuer="ops", issuer_type="user")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_invalid_issuer_type(db_session) -> None:
    """Typo guard at the audit boundary: only 'user' / 'service_account' accepted."""
    repo = LicenseAuditRepository(db_session)

    with pytest.raises(ValueError, match="issuer_type"):
        await repo.record_issuance(
            _payload(license_id="lic-bad-itype"),
            issuer="ops",
            issuer_type="service-account",  # type: ignore[arg-type]  # intentionally wrong
        )

    count = await db_session.scalar(select(func.count()).select_from(LicenseIssuance))
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_empty_org(db_session) -> None:
    repo = LicenseAuditRepository(db_session)

    with pytest.raises(ValueError, match="payload.org"):
        await repo.record_issuance(
            _payload(license_id="lic-empty-org", org=""),
            issuer="ops",
            issuer_type="user",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rejects_whitespace_only_issuer(db_session) -> None:
    repo = LicenseAuditRepository(db_session)

    with pytest.raises(ValueError, match="issuer"):
        await repo.record_issuance(
            _payload(license_id="lic-empty-iss"),
            issuer="   ",
            issuer_type="user",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_persists_features_jsonb(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    payload = _payload(license_id="lic-jsonb", features=["a", "b", "c"])

    await repo.record_issuance(payload, issuer="ops", issuer_type="user")

    fetched = await repo.find_by_license_id("lic-jsonb")
    assert fetched is not None
    assert fetched.features == ["a", "b", "c"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_with_no_expiry(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    payload = _payload(license_id="lic-perp", expires_at=None, max_tenants=None)

    row = await repo.record_issuance(payload, issuer="ops", issuer_type="user")

    assert row.expires_at is None
    assert row.max_tenants is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_naive_datetime_normalized_to_utc(db_session) -> None:
    """Naive ISO timestamps default to UTC, mirroring LicenseService verifier."""
    repo = LicenseAuditRepository(db_session)
    payload = _payload(
        license_id="lic-naive",
        issued_at="2026-05-01T12:00:00",
        expires_at=None,
    )

    row = await repo.record_issuance(payload, issuer="ops", issuer_type="user")

    assert row.issued_at.tzinfo is not None
    utcoffset = row.issued_at.utcoffset()
    assert utcoffset is not None
    assert utcoffset.total_seconds() == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_by_license_id_returns_none_for_unknown(db_session) -> None:
    repo = LicenseAuditRepository(db_session)
    assert await repo.find_by_license_id("never-issued") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_org_returns_only_matching_org(db_session) -> None:
    repo = LicenseAuditRepository(db_session)

    await repo.record_issuance(
        _payload(license_id="acme-1", org="acme"),
        issuer="ops",
        issuer_type="user",
    )
    await repo.record_issuance(
        _payload(license_id="acme-2", org="acme"),
        issuer="ops",
        issuer_type="user",
    )
    await repo.record_issuance(
        _payload(license_id="other-1", org="other"),
        issuer="ops",
        issuer_type="user",
    )

    acme_rows = await repo.list_by_org("acme")
    assert {r.license_id for r in acme_rows} == {"acme-1", "acme-2"}

    other_rows = await repo.list_by_org("other")
    assert {r.license_id for r in other_rows} == {"other-1"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_org_orders_by_issued_at_desc(db_session) -> None:
    repo = LicenseAuditRepository(db_session)

    await repo.record_issuance(
        _payload(
            license_id="old",
            org="acme",
            issued_at="2026-01-01T00:00:00+00:00",
        ),
        issuer="ops",
        issuer_type="user",
    )
    await repo.record_issuance(
        _payload(
            license_id="new",
            org="acme",
            issued_at="2026-05-01T00:00:00+00:00",
        ),
        issuer="ops",
        issuer_type="user",
    )

    rows = await repo.list_by_org("acme")
    assert [r.license_id for r in rows] == ["new", "old"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_org_pagination_stable_with_tied_timestamps(db_session) -> None:
    """Tied issued_at -> license_id is the deterministic tie-breaker.

    Without the secondary sort, page boundaries would be undefined per
    the SQL spec and a paginating compliance reporter could miss or
    duplicate rows.
    """
    repo = LicenseAuditRepository(db_session)

    for lid in ["p-a", "p-b", "p-c", "p-d", "p-e"]:
        await repo.record_issuance(
            _payload(
                license_id=lid,
                org="page-test",
                issued_at="2026-05-01T12:00:00+00:00",
            ),
            issuer="ops",
            issuer_type="user",
        )

    page1 = await repo.list_by_org("page-test", limit=2, offset=0)
    page2 = await repo.list_by_org("page-test", limit=2, offset=2)
    page3 = await repo.list_by_org("page-test", limit=2, offset=4)

    assert [r.license_id for r in page1] == ["p-a", "p-b"]
    assert [r.license_id for r in page2] == ["p-c", "p-d"]
    assert [r.license_id for r in page3] == ["p-e"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_issuance_rollback_on_connection_failure(db_session) -> None:
    """Authentic fault injection: kill backend mid-tx, verify session recoverable.

    Reproduces a real DB-level commit failure by terminating the backend
    via ``pg_terminate_backend(pid)`` from a separate connection. Without
    rollback breadth in ``record_issuance``, the AsyncSession would be
    left in ``PendingRollbackError`` state and any subsequent write on
    the same session would fail -- breaking the contract that the
    repository owns its transaction.

    The earlier monkeypatch-based test bypassed SQLAlchemy entirely and
    didn't exercise the actual aborted-transaction state machine.
    """
    repo = LicenseAuditRepository(db_session)

    pid = (await db_session.execute(text("SELECT pg_backend_pid()"))).scalar_one()

    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://meho:password@localhost:5432/meho_test"
    )
    killer_engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with killer_engine.connect() as conn:
            # pid is a Postgres-generated integer from pg_backend_pid() above;
            # never user input. Safe under f-string.
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            await conn.execute(text(f"SELECT pg_terminate_backend({pid})"))
            await conn.commit()
    finally:
        await killer_engine.dispose()

    with pytest.raises((OperationalError, DBAPIError, InterfaceError)):
        await repo.record_issuance(
            _payload(license_id="lic-killed"),
            issuer="ops",
            issuer_type="user",
        )

    # If the repository rolled back properly, the session can write again
    # (NullPool gives us a fresh connection on the next statement).
    row = await repo.record_issuance(
        _payload(license_id="lic-after-kill"),
        issuer="ops",
        issuer_type="user",
    )
    assert row.license_id == "lic-after-kill"
