# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-unique per-target cache key.

Connector credential / session / client caches are process-global and
shared across tenants. Keying them on ``target.name`` alone is a
cross-tenant isolation bug: two same-named targets in different tenants
collapse to one entry, so one tenant can be served another tenant's
cached session or credential (evoila/meho#1642).

The targets table enforces uniqueness only on ``(tenant_id, name)`` and
``id`` is the table's primary key, so ``(tenant_id, id)`` is the stable,
tenant-unique identity of a target row. This module exposes the single
canonical key so every connector cache derives the same value and no
two-cache-keying-skew bugs can creep in (e.g. NSX's session-token cache
and the shared HTTP-client pool keying differently).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TenantScopedTargetLike(Protocol):
    """Minimum identity shape needed to derive a tenant-unique cache key.

    The concrete ``Target`` model
    (:class:`meho_backplane.targets.Target`) and ORM row both carry a
    UUID ``id`` (primary key) and a NOT-NULL UUID ``tenant_id``, so they
    satisfy this Protocol unchanged. The connector ``*TargetLike``
    Protocols extend this one so the structural-typing contract the
    connectors already rely on keeps mypy honest about the two fields
    the cache key reads.
    """

    id: object
    tenant_id: object


def target_cache_key(target: TenantScopedTargetLike) -> tuple[str, str]:
    """Return the tenant-unique ``(tenant_id, id)`` cache key for *target*.

    Both components are stringified so the key is hashable and stable
    regardless of whether the caller passes ``uuid.UUID`` objects (the
    live model) or plain strings (test doubles). ``id`` is the targets
    table primary key and ``(tenant_id, name)`` is its only uniqueness
    constraint, so ``(tenant_id, id)`` uniquely identifies one target row
    across all tenants — unlike ``name`` alone, which collides across
    tenants (evoila/meho#1642).
    """
    return (str(target.tenant_id), str(target.id))
