# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""License-issuance audit repository.

:class:`LicenseAuditRepository` writes one row to ``license_issuance``
per signed enterprise token, owns its transaction, and surfaces typed
exceptions so the issuance CLI can fail-closed before returning a token
to the customer.

Idempotency is enforced by ``license_id`` PRIMARY KEY: a duplicate
issuance raises :class:`DuplicateLicenseIDError` rather than silently
inserting a second row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from meho_app.modules.licensing.models import LicenseIssuance

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_app.core.licensing import LicensePayload


# Compliance-grade audit fields are typo-prone if left as free-form text.
# Validated at the repository boundary so a corrupt value never reaches
# the permanent record. Adding a third issuer_type is a one-line code
# change; no migration needed.
IssuerType = Literal["user", "service_account"]
_VALID_ISSUER_TYPES: frozenset[str] = frozenset({"user", "service_account"})


class DuplicateLicenseIDError(ValueError):
    """``record_issuance`` called with a license_id already in the table."""


class LicenseAuditRepository:
    """Write/read access to the ``license_issuance`` table.

    Owns its transaction: ``record_issuance`` calls ``commit()`` on
    success and ``rollback()`` on **any** commit failure so the
    ``AsyncSession`` is always reusable when ``record_issuance``
    returns. Read methods do not commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_issuance(
        self,
        payload: LicensePayload,
        *,
        issuer: str,
        issuer_type: IssuerType,
    ) -> LicenseIssuance:
        """Write one issuance row and commit.

        Args:
            payload: Validated license payload from the issuance script.
            issuer: Identifier of the principal minting the license
                (operator email, service-account name, etc.).
                Must be non-empty.
            issuer_type: ``'user'`` or ``'service_account'``.

        Returns:
            The persisted :class:`LicenseIssuance` row.

        Raises:
            DuplicateLicenseIDError: ``payload.license_id`` is already
                in the table. Idempotency contract.
            ValueError: payload contains empty/whitespace-only required
                fields, ``issuer_type`` is not a valid value, or
                ``payload.issued_at`` / ``payload.expires_at`` cannot
                be parsed as ISO 8601.
            Exception: any other commit failure. The session is rolled
                back before re-raising so the caller can fail-closed
                without leaving the session in ``PendingRollbackError``.
        """
        _require_non_empty(payload.license_id, field="payload.license_id")
        _require_non_empty(payload.org, field="payload.org")
        _require_non_empty(payload.tier, field="payload.tier")
        _require_non_empty(issuer, field="issuer")
        if issuer_type not in _VALID_ISSUER_TYPES:
            raise ValueError(
                f"issuer_type must be one of {sorted(_VALID_ISSUER_TYPES)}, got {issuer_type!r}"
            )

        issued_at = _parse_iso(payload.issued_at, field="issued_at")
        expires_at = (
            _parse_iso(payload.expires_at, field="expires_at") if payload.expires_at else None
        )

        row = LicenseIssuance(
            license_id=payload.license_id,
            org=payload.org,
            tier=payload.tier,
            features=list(payload.features),
            issued_at=issued_at,
            expires_at=expires_at,
            max_tenants=payload.max_tenants,
            issuer=issuer,
            issuer_type=issuer_type,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except Exception as exc:
            # Roll back on every commit failure: an InterfaceError /
            # OperationalError leaves the AsyncSession in a
            # PendingRollbackError state that fails any subsequent
            # write on the same session. The narrower IntegrityError
            # case is translated to a typed error for idempotency.
            await self.session.rollback()
            if isinstance(exc, IntegrityError) and _is_pk_violation(exc):
                raise DuplicateLicenseIDError(
                    f"license_id={payload.license_id!r} already recorded"
                ) from exc
            raise
        return row

    async def find_by_license_id(self, license_id: str) -> LicenseIssuance | None:
        stmt = sa.select(LicenseIssuance).where(LicenseIssuance.license_id == license_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_org(
        self,
        org: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[LicenseIssuance]:
        # license_id (PK, unique) is the tie-breaker so pagination is
        # totally ordered when issued_at values tie. Without it, page
        # boundaries are non-deterministic per the SQL spec and a
        # paginating compliance reporter could miss or duplicate rows.
        stmt = (
            sa.select(LicenseIssuance)
            .where(LicenseIssuance.org == org)
            .order_by(LicenseIssuance.issued_at.desc(), LicenseIssuance.license_id)
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


def _require_non_empty(value: str, *, field: str) -> None:
    """Reject empty or whitespace-only required strings at the boundary."""
    if not value or not value.strip():
        raise ValueError(f"{field} must be non-empty, got {value!r}")


def _parse_iso(value: str, *, field: str) -> datetime:
    """Parse an ISO 8601 string, defaulting naive datetimes to UTC.

    Mirrors the boundary normalization in
    :class:`meho_app.core.licensing.LicenseService` so verifier and
    audit log agree on tz handling.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO 8601 in payload.{field}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_pk_violation(exc: IntegrityError) -> bool:
    """True iff the underlying error is a Postgres unique-violation (SQLSTATE 23505).

    asyncpg surfaces this on ``exc.orig.sqlstate``; the SQLSTATE check
    keeps the helper driver-agnostic without importing asyncpg.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "23505"
