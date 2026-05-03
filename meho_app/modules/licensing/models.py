# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SQLAlchemy models for the license-issuance audit log.

``LicenseIssuance`` records every signed enterprise token minted by
``scripts/issue-license.py``. The row is written before the token is
returned to the caller; failure aborts issuance (fail-closed, enforced
by :class:`meho_app.modules.licensing.audit.LicenseAuditRepository`).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from meho_app.database import Base


class LicenseIssuance(Base):
    """One row per signed enterprise license token.

    Columns mirror :class:`meho_app.core.licensing.LicensePayload` plus
    issuer attribution and revocation columns reserved for future tooling.

    Table: ``license_issuance``
    """

    __tablename__ = "license_issuance"

    license_id = sa.Column(sa.Text, primary_key=True)
    org = sa.Column(sa.Text, nullable=False)
    tier = sa.Column(sa.Text, nullable=False)
    features = sa.Column(JSONB, nullable=False)
    issued_at = sa.Column(sa.TIMESTAMP(timezone=True), nullable=False)
    expires_at = sa.Column(sa.TIMESTAMP(timezone=True), nullable=True)
    max_tenants = sa.Column(sa.Integer, nullable=True)
    issuer = sa.Column(sa.Text, nullable=False)
    # 'user' or 'service_account'; allowed values are validated at the
    # repository boundary (LicenseAuditRepository.record_issuance) so a
    # typo never enters the permanent compliance record. Adding a third
    # value is a code change in audit.py; no migration needed.
    issuer_type = sa.Column(sa.Text, nullable=False)
    revoked_at = sa.Column(sa.TIMESTAMP(timezone=True), nullable=True)
    revocation_reason = sa.Column(sa.Text, nullable=True)
    # Server clock at row insert. Distinct from issued_at (the license
    # claim) so that drift between them is forensic signal for clock
    # skew on the issuance host.
    created_at = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        # Composite index for the actual list_by_org access pattern
        # (filter by org, order by issued_at). Postgres uses a backwards
        # btree scan to satisfy ORDER BY issued_at DESC, so the index
        # itself stays in default ASC ordering.
        sa.Index("ix_license_issuance_org_issued_at", "org", "issued_at"),
        # Standalone issued_at index for cross-org date-range reporting
        # ("all issuances last quarter") that v0.2 compliance work will
        # likely add.
        sa.Index("ix_license_issuance_issued_at", "issued_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<LicenseIssuance(license_id={self.license_id!r}, "
            f"org={self.org!r}, tier={self.tier!r})>"
        )
