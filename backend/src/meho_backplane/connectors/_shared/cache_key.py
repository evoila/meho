# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-unique cache keys for connector caches.

Two keys live here:

* :func:`target_cache_key` -- one entry per target. Used for HTTP client
  pools, SSH connections and other caches that carry per-target settings
  (TLS checks, pins, server names).
* :func:`credential_cache_key` -- one entry per credential. Used for login
  tokens and sessions. Two targets in one tenant that use the same secret
  against the same login host share one token, one login and one renewal.
  Some vendors allow only one active session per user, so a second login
  with the same user ends the first one; a per-target token cache would
  let two such targets end each other's token (evoila/meho#5033).

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

from typing import Any, Protocol, runtime_checkable


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


def credential_cache_key(target: TenantScopedTargetLike, *login_inputs: Any) -> tuple[str, ...]:
    """Return the cache key for a login token or session of *target*.

    The key is the credential: ``(tenant_id, secret_ref, host, port, fqdn)``
    plus *login_inputs*. Two targets in one tenant with the same secret and
    the same login host get the same key, so they share one token.

    * ``tenant_id`` keeps tenants apart (evoila/meho#1642).
    * ``host``, ``port`` and ``fqdn`` name the login host. ``fqdn`` is part of
      it because some connectors send it as the ``Host`` header, and the
      appliance picks the organisation from that header.
    * *login_inputs* are the other target values the connector's login reads
      (for example a login domain or an auth source). Two targets that differ
      in one of them log in differently, so they must not share a token.

    A target with no ``secret_ref`` falls back to :func:`target_cache_key`:
    with no secret reference there is nothing to compare, so the token stays
    per target.
    """
    secret_ref = getattr(target, "secret_ref", None)
    if not secret_ref:
        return target_cache_key(target)
    host = str(getattr(target, "host", None) or "").strip().lower()
    port = getattr(target, "port", None)
    fqdn = str(getattr(target, "fqdn", None) or "").strip().lower()
    return (
        str(target.tenant_id),
        str(secret_ref),
        host,
        "" if port is None else str(port),
        fqdn,
        *("" if value is None else str(value) for value in login_inputs),
    )
