# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant mail-recipient allowlist resolver (#3499).

``MAIL_RECIPIENT_ALLOWLIST`` is a single deployment-level instance floor
(:mod:`meho_backplane.connectors.mail.allowlist`). On a shared instance that
floor cannot be narrowed per tenant, so an approved ``mail.send`` in one tenant
can deliver to the recipients another tenant configured. This resolver reads a
**per-tenant narrowing** override (``tenant.mail_recipient_allowlist``) that the
``mail.send`` dispatch handler screens recipients against *before* the instance
floor, letting an operator pin one tenant to no mail while others keep alert
mail. The override can only narrow: the instance floor is still applied by the
transport afterwards, so a tenant can never widen its recipient space past it.

Resolution of ``tenant.mail_recipient_allowlist`` (a nullable ``Text`` column):

* ``NULL`` → :data:`None` (**inherit**): no per-tenant screen; the instance
  floor alone governs the send.
* ``""`` → ``(frozenset(), frozenset())`` (**deny**): the tenant's parsed
  allowlist is empty, so :func:`~meho_backplane.connectors.mail.allowlist.recipient_allowed`
  admits nothing and every dispatched ``mail.send`` for this tenant is refused.
* a comma-separated address/domain string → the parsed
  ``(addresses, domains)`` the tenant may mail (still intersected with the
  instance floor at the transport).

**Fail-closed** — the opposite direction from the flight-recorder resolver's
fail-open (:mod:`meho_backplane.flight_recorder.config`). Capture is a *record*
decision where doubt should record less; this is a *delivery-authorization*
decision where doubt should deliver less. So an unreadable / unparseable policy
resolves to **deny** (an empty allowlist), never to inherit: falling through to
the instance floor on a read error would defeat the containment the override
exists to provide. The deny is not cached, so a transient DB error does not pin
the tenant to deny for the cache TTL.

Cache discipline mirrors :mod:`meho_backplane.flight_recorder.config`: a 60s
per-key TTL keeps the policy off the DB on the per-dispatch path; only a miss
awaits a one-row SELECT. Only successfully-resolved values are cached.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.connectors.mail.allowlist import parse_recipient_allowlist
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant

__all__ = [
    "invalidate_tenant_mail_policy_cache",
    "reset_tenant_mail_policy_cache_for_testing",
    "resolve_tenant_recipient_allowlist",
]

_log = structlog.get_logger(__name__)

#: Per-key TTL, mirroring the flight-recorder / announce-gate resolvers.
_CACHE_TTL_SECONDS: Final[float] = 60.0

#: Per-tenant resolved-policy cache. Value = (resolved, monotonic_expires_at)
#: where ``resolved`` is ``None`` (inherit) or the parsed ``(addresses,
#: domains)`` tuple. A hit is a pure dict lookup; a miss awaits a one-row
#: SELECT of the single ``mail_recipient_allowlist`` column.
_TENANT_CACHE: dict[UUID, tuple[tuple[frozenset[str], frozenset[str]] | None, float]] = {}

#: The deny sentinel a fail-closed resolution returns (empty allowlist admits
#: nothing). Distinct object identity is irrelevant — an empty parsed tuple is
#: the deny state everywhere in the connector.
_DENY: Final[tuple[frozenset[str], frozenset[str]]] = (frozenset(), frozenset())


def reset_tenant_mail_policy_cache_for_testing() -> None:
    """Clear the per-tenant cache (test isolation only)."""
    _TENANT_CACHE.clear()


def invalidate_tenant_mail_policy_cache(tenant_id: UUID) -> None:
    """Evict the cached allowlist for *tenant_id* after an operator policy write.

    The ``PATCH /api/v1/tenants/mail-recipient-policy`` route calls this on a
    change so the new value takes effect on the next dispatched ``mail.send``
    instead of waiting out :data:`_CACHE_TTL_SECONDS` (or a restart).
    Idempotent — popping an absent key is a no-op, so a no-op write can call it
    safely.
    """
    _TENANT_CACHE.pop(tenant_id, None)


async def resolve_tenant_recipient_allowlist(
    tenant_id: UUID,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return the per-tenant recipient allowlist, or ``None`` to inherit.

    ``None`` means the tenant set no override (``NULL`` column, or no tenant
    row at all — a brand-new tenant inherits the instance floor). A returned
    ``(addresses, domains)`` tuple is the tenant's own allowlist; an empty
    tuple denies every recipient.

    Fail-closed: any error reading or parsing the policy resolves to the deny
    sentinel (empty tuple) and is **not** cached, so a transient DB error
    refuses this send without pinning the tenant to deny for the TTL. Cache-
    aware for the success path.
    """
    now = time.monotonic()
    cached = _TENANT_CACHE.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            raw = (
                await session.execute(
                    select(Tenant.mail_recipient_allowlist).where(Tenant.id == tenant_id)
                )
            ).one_or_none()
        # ``raw is None`` → no tenant row; ``raw[0] is None`` → NULL column.
        # Both mean "no per-tenant override" → inherit the instance floor.
        resolved: tuple[frozenset[str], frozenset[str]] | None = (
            None if raw is None or raw[0] is None else parse_recipient_allowlist(raw[0])
        )
    except Exception:
        _log.warning(
            "mail_tenant_policy_resolve_failed_deny",
            tenant_id=str(tenant_id),
            exc_info=True,
        )
        return _DENY
    _TENANT_CACHE[tenant_id] = (resolved, now + _CACHE_TTL_SECONDS)
    return resolved
