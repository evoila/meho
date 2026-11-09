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

from sqlalchemy import Insert, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.web_push.endpoints import CheckedSubscription, push_service_name
from meho_backplane.web_push.models import PushSubscription

__all__ = [
    "MAX_SUBSCRIPTIONS_PER_PERSON",
    "PushStoreError",
    "SubscriptionView",
    "delete_gone",
    "delete_own_by_endpoint",
    "is_own_endpoint",
    "list_for_person",
    "list_for_tenant",
    "refresh_approve_scope",
    "remove_subscription",
    "save_subscription",
]

#: A person rarely has more browsers than this. Above it, the oldest go.
MAX_SUBSCRIPTIONS_PER_PERSON = 10


class PushStoreError(Exception):
    """A database write failed. Carries no endpoint and no key.

    SQLAlchemy errors quote the statement's values, which here are the
    endpoint and the two keys. So the write functions raise this error
    instead, without the original one attached, and a log of it shows
    none of them. Each write runs in a savepoint, so after this error the
    request's own transaction still works.
    """


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
    approve_scope: str | None,
) -> None:
    """Store *subscription* for this person, with the time and *approve_scope*.

    The endpoint names one browser. When it is stored already (the same
    browser switched on again, or another person now uses it), the row is
    replaced, so a browser belongs to the person who switched it on last.
    One ``INSERT ... ON CONFLICT`` does this, so two saves of the same
    browser at the same time both succeed. Above
    :data:`MAX_SUBSCRIPTIONS_PER_PERSON` the person's oldest rows go.

    Raises :class:`PushStoreError` when the database refuses the write.
    """
    try:
        async with db.begin_nested():
            await _upsert(
                db,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                subscription=subscription,
                approve_scope=approve_scope,
            )
            await _drop_oldest(db, operator_sub=operator_sub)
    except SQLAlchemyError:
        raise PushStoreError("could not save the push subscription") from None


async def _upsert(
    db: AsyncSession,
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    subscription: CheckedSubscription,
    approve_scope: str | None,
) -> None:
    values = {
        "id": uuid.uuid4(),
        "operator_sub": operator_sub,
        "tenant_id": tenant_id,
        "endpoint": subscription.endpoint,
        "p256dh": subscription.p256dh,
        "auth": subscription.auth,
        "created_at": datetime.now(UTC),
        "approve_scope": approve_scope,
    }
    replaced = {k: v for k, v in values.items() if k not in ("id", "endpoint")}
    conn = await db.connection()
    stmt: Insert
    if conn.dialect.name == "postgresql":
        pg = pg_insert(PushSubscription).values(**values)
        stmt = pg.on_conflict_do_update(index_elements=["endpoint"], set_=replaced)
    else:
        lite = sqlite_insert(PushSubscription).values(**values)
        stmt = lite.on_conflict_do_update(index_elements=["endpoint"], set_=replaced)
    await db.execute(stmt)


async def _drop_oldest(db: AsyncSession, *, operator_sub: str) -> None:
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
    """Delete this person's subscription of one browser; ``True`` when one was deleted.

    Raises :class:`PushStoreError` when the database refuses the write.
    """
    try:
        async with db.begin_nested():
            result = await db.execute(
                delete(PushSubscription)
                .where(PushSubscription.operator_sub == operator_sub)
                .where(PushSubscription.endpoint == endpoint.strip())
                .returning(PushSubscription.id)
            )
            deleted = result.first() is not None
    except SQLAlchemyError:
        raise PushStoreError("could not delete the push subscription") from None
    return deleted


async def is_own_endpoint(db: AsyncSession, *, operator_sub: str, endpoint: str) -> bool:
    """``True`` when this person has a stored subscription for this browser.

    Raises :class:`PushStoreError` when the database refuses the read.
    """
    try:
        found = await db.execute(
            select(PushSubscription.id)
            .where(PushSubscription.operator_sub == operator_sub)
            .where(PushSubscription.endpoint == endpoint.strip())
        )
    except SQLAlchemyError:
        raise PushStoreError("could not read the push subscription") from None
    return found.first() is not None


async def refresh_approve_scope(
    db: AsyncSession, *, operator_sub: str, tenant_id: uuid.UUID, approve_scope: str | None
) -> None:
    """Set *approve_scope* on this person's rows of *tenant_id*.

    The login groups can change after the switch was turned on; the account
    page calls this each time it opens. Rows of another tenant keep theirs.
    Raises :class:`PushStoreError` when the database refuses the write.
    """
    try:
        async with db.begin_nested():
            await db.execute(
                update(PushSubscription)
                .where(PushSubscription.operator_sub == operator_sub)
                .where(PushSubscription.tenant_id == tenant_id)
                .where(
                    or_(
                        PushSubscription.approve_scope.is_(None),
                        PushSubscription.approve_scope != approve_scope,
                    )
                    if approve_scope is not None
                    else PushSubscription.approve_scope.is_not(None)
                )
                .values(approve_scope=approve_scope)
            )
    except SQLAlchemyError:
        raise PushStoreError("could not update the push subscription") from None


async def delete_gone(db: AsyncSession, *, subscription_id: uuid.UUID) -> None:
    """Delete a subscription whose push service answered that it is gone."""
    await db.execute(delete(PushSubscription).where(PushSubscription.id == subscription_id))


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
