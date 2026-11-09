# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save, list and delete push subscriptions.

Every function takes the person (``operator_sub``) and tenant from the
signed-in session, never from the request body. The callers commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.web_push.endpoints import CheckedSubscription, push_service_name
from meho_backplane.web_push.models import PushSubscription

__all__ = [
    "MAX_SUBSCRIPTIONS_PER_PERSON",
    "SubscriptionView",
    "delete_own_by_endpoint",
    "list_for_person",
    "list_for_tenant",
    "remove_subscription",
    "save_subscription",
]

#: A person rarely has more browsers than this. Above it, the oldest go.
MAX_SUBSCRIPTIONS_PER_PERSON = 10


@dataclass(frozen=True)
class SubscriptionView:
    """What the page shows of a subscription: never the endpoint or keys."""

    id: uuid.UUID
    operator_sub: str
    service: str
    created_at: datetime


def _view(row: PushSubscription) -> SubscriptionView:
    return SubscriptionView(
        id=row.id,
        operator_sub=row.operator_sub,
        service=push_service_name(row.endpoint) or "unknown",
        created_at=row.created_at,
    )


async def save_subscription(
    db: AsyncSession,
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    subscription: CheckedSubscription,
) -> None:
    """Store *subscription* for this person, with the time.

    The endpoint names one browser. When it is stored already (the same
    browser switched on again, or another person now uses it), the row is
    replaced, so a browser belongs to the person who switched it on last.
    Above :data:`MAX_SUBSCRIPTIONS_PER_PERSON` the person's oldest rows go.
    """
    await db.execute(
        delete(PushSubscription).where(PushSubscription.endpoint == subscription.endpoint)
    )
    db.add(
        PushSubscription(
            id=uuid.uuid4(),
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            endpoint=subscription.endpoint,
            p256dh=subscription.p256dh,
            auth=subscription.auth,
            created_at=datetime.now(UTC),
        )
    )
    await db.flush()
    rows = (
        await db.execute(
            select(PushSubscription.id)
            .where(PushSubscription.operator_sub == operator_sub)
            .order_by(PushSubscription.created_at.desc(), PushSubscription.id)
            .offset(MAX_SUBSCRIPTIONS_PER_PERSON)
        )
    ).scalars()
    old = list(rows)
    if old:
        await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(old)))


async def delete_own_by_endpoint(db: AsyncSession, *, operator_sub: str, endpoint: str) -> bool:
    """Delete this person's subscription of one browser; ``True`` when one was deleted."""
    result = await db.execute(
        delete(PushSubscription)
        .where(PushSubscription.operator_sub == operator_sub)
        .where(PushSubscription.endpoint == endpoint.strip())
        .returning(PushSubscription.id)
    )
    return result.first() is not None


async def list_for_person(db: AsyncSession, *, operator_sub: str) -> list[SubscriptionView]:
    """Return this person's subscriptions, newest first."""
    rows = await db.execute(
        select(PushSubscription)
        .where(PushSubscription.operator_sub == operator_sub)
        .order_by(PushSubscription.created_at.desc())
    )
    return [_view(row) for row in rows.scalars()]


async def list_for_tenant(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[SubscriptionView]:
    """Return every subscription made in this tenant, newest first (for its admins)."""
    rows = await db.execute(
        select(PushSubscription)
        .where(PushSubscription.tenant_id == tenant_id)
        .order_by(PushSubscription.created_at.desc())
    )
    return [_view(row) for row in rows.scalars()]


async def remove_subscription(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    operator_sub: str,
    admin_of_tenant: uuid.UUID | None,
) -> bool:
    """Delete one subscription; ``True`` when one was deleted.

    The row must belong to *operator_sub*, or, when *admin_of_tenant* is
    set (the caller is a ``tenant_admin`` there), to that tenant. Any other
    row counts as not found.
    """
    owner_or_admin = PushSubscription.operator_sub == operator_sub
    if admin_of_tenant is not None:
        owner_or_admin = owner_or_admin | (PushSubscription.tenant_id == admin_of_tenant)
    result = await db.execute(
        delete(PushSubscription)
        .where(PushSubscription.id == subscription_id)
        .where(owner_or_admin)
        .returning(PushSubscription.id)
    )
    return result.first() is not None
