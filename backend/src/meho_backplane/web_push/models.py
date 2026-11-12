# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``push_subscription`` table (migrations ``0151`` and ``0161``)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Text, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from meho_backplane.db.models import Base

__all__ = ["PushSubscription"]


class PushSubscription(Base):
    """One browser of one person that wants approval notifications.

    ``endpoint`` is unique: it names one browser. ``tenant_id`` is the
    tenant the person worked in when they switched the notifications on; a
    ``tenant_admin`` of that tenant may remove the row. ``endpoint``,
    ``p256dh`` and ``auth`` are never logged and never shown.

    ``tenant_role``, ``approver``, ``self_approver`` and ``platform_admin``
    are the person's login facts in that tenant, read from their login at
    ``facts_read_at``. MEHO uses them to decide who may approve a request
    (:mod:`meho_backplane.web_push.recipients`). ``tenant_role`` is
    ``NULL`` when the facts were never read or the login is not a person.
    """

    __tablename__ = "push_subscription"
    __table_args__ = (
        UniqueConstraint("endpoint", name="push_subscription_endpoint_key"),
        Index("push_subscription_operator_sub_idx", "operator_sub"),
        Index("push_subscription_tenant_id_idx", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    operator_sub: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_role: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    approver: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    self_approver: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    facts_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
