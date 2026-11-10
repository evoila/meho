# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``space``, ``space_member`` and ``resource_assignment`` tables (migration ``0156``)."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from meho_backplane.db.models import Base

__all__ = ["ResourceAssignment", "Space", "SpaceMember"]


class Space(Base):
    """An area inside one tenant that belongs to a person or a small team.

    The owner is the Keycloak issuer and subject, never an email address.
    """

    __tablename__ = "space"
    __table_args__ = (
        sa.CheckConstraint("kind IN ('person', 'team')", name="ck_space_kind"),
        sa.CheckConstraint("state IN ('active', 'disabled')", name="ck_space_state"),
        Index("space_tenant_name_idx", "tenant_id", "name", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    owner_sub: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)


class SpaceMember(Base):
    """One period of one person in one space.

    A change sets ``valid_to`` on this row and adds a new row. A row with
    ``valid_to`` empty is the current one. Rows are never deleted.
    """

    __tablename__ = "space_member"
    __table_args__ = (
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_space_member_role"),
        sa.CheckConstraint("state IN ('active', 'disabled')", name="ck_space_member_state"),
        Index("space_member_tenant_space_idx", "tenant_id", "space_id", "valid_from"),
        Index(
            "space_member_open_idx",
            "space_id",
            "member_issuer",
            "member_sub",
            unique=True,
            postgresql_where=sa.text("valid_to IS NULL"),
            sqlite_where=sa.text("valid_to IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("space.id"), nullable=False)
    member_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    member_sub: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class ResourceAssignment(Base):
    """One period in which a system belongs to a space.

    ``target_id`` has no foreign key on purpose: a deleted target keeps its
    assignment history. ``object_id`` is the vendor's own id for the thing
    (for a machine, not its name). ``name_at_assignment`` is the name when
    the row was made.
    """

    __tablename__ = "resource_assignment"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('target', 'object', 'service', 'fact')",
            name="ck_resource_assignment_kind",
        ),
        sa.CheckConstraint(
            "access IN ('own', 'shared_summary')", name="ck_resource_assignment_access"
        ),
        Index("resource_assignment_tenant_space_idx", "tenant_id", "space_id", "valid_from"),
        Index("resource_assignment_tenant_target_idx", "tenant_id", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("space.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_at_assignment: Mapped[str] = mapped_column(Text, nullable=False)
    access: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
