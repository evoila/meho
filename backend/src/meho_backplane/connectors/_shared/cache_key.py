# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-unique cache keys for connector caches.

Two keys live here:

* :func:`target_cache_key` -- one entry per target. Used for HTTP client
  pools, SSH connections and other caches that carry per-target settings
  (TLS checks, pins, server names).
* :func:`credential_cache_key` -- one entry per credential. Used for login
  tokens and sessions. Two targets in one tenant that use the same secret
  against the same login address (scheme, host, port) share one token, one
  login and one renewal.
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

from typing import Any, Final, Protocol, runtime_checkable


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


#: Default port per transport scheme. Shared with the HTTP adapter so the
#: base URL and the credential cache key resolve the same login address.
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
_ALLOWED_SCHEMES: Final = frozenset(DEFAULT_PORTS)


def effective_scheme(target: object) -> str:
    """Return the transport scheme for *target* — ``https`` unless opted out.

    Reads an optional ``scheme`` key from ``target.extras``. Absent (the
    default for every existing target — ``extras`` is ``{}`` by default) →
    ``https``, so behaviour stays byte-identical to before. Present → must be
    exactly ``http`` or ``https``; anything else raises :exc:`ValueError` at
    dispatch rather than being silently coerced, so a typo like ``htp`` fails
    loud instead of falling back to https and masking the misconfiguration.

    Honouring an explicit ``http`` is what lets an ``HttpConnector`` reach a
    plain-HTTP management API — RabbitMQ's Management API is HTTP on
    ``15672`` (HTTPS only on ``15671`` when the mgmt TLS listener is enabled)
    (evoila/meho#2587). The scheme is deliberately opt-in and is **never**
    derived from ``verify_tls``: certificate trust and transport selection
    are orthogonal, and deriving one from the other would silently change the
    transport of every existing ``verify_tls=false`` target.
    """
    extras = getattr(target, "extras", None)
    if not isinstance(extras, dict):
        return "https"
    scheme = extras.get("scheme", "https")
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"target {getattr(target, 'name', '?')!r} has an invalid "
            f"extras.scheme {scheme!r}; expected one of {sorted(_ALLOWED_SCHEMES)}"
        )
    # Membership above guarantees a literal "http"/"https"; str() narrows the
    # ``Any`` that flows in from the free-form ``extras`` dict for the typer.
    return str(scheme)


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


def _login_host(raw: object) -> str:
    """Return *raw* host with the host name case-folded and any path kept as is.

    A target ``host`` may carry a service path (``appliance/Fleet-LCM``). The
    HTTP adapter puts that path in the base URL unchanged, so two targets
    whose paths differ only in case reach different login paths. Host names
    are case-insensitive, paths are not.
    """
    name, slash, path = str(raw or "").strip().partition("/")
    return name.lower() + slash + path


def credential_cache_key(
    target: TenantScopedTargetLike, *login_inputs: Any, secret_ref: str | None = None
) -> tuple[str, ...]:
    """Return the cache key for a login token or session of *target*.

    The key is the credential: ``(tenant_id, secret_ref, scheme, host, port,
    fqdn)`` plus *login_inputs*. Two targets in one tenant with the same
    secret and the same login address get the same key, so they share one
    token.

    * ``tenant_id`` keeps tenants apart (evoila/meho#1642).
    * ``scheme``, ``host`` and ``port`` name the login address the way the
      HTTP adapter builds it: the scheme comes from :func:`effective_scheme`
      and an unset port is the scheme's default port. So a target with no
      port and a target with the explicit default port share one key, and an
      HTTP target never shares a key (or a token) with an HTTPS target. Only
      the host name is case-folded; a service path carried on ``host`` keeps
      its case, because the adapter sends it as is and paths are
      case-sensitive.
    * ``fqdn`` is part of the address because some connectors send it as the
      ``Host`` header, and the appliance picks the organisation from it.
    * *login_inputs* are the other target values the connector's login reads
      (for example a login domain or an auth source). Two targets that differ
      in one of them log in differently, so they must not share a token.
    * *secret_ref* replaces ``target.secret_ref`` when the login uses another
      secret than the target's own (the VCF Automation provider plane).

    A target with no secret reference falls back to :func:`target_cache_key`:
    with no secret reference there is nothing to compare, so the token stays
    per target.
    """
    if secret_ref is None:
        secret_ref = getattr(target, "secret_ref", None)
    if not secret_ref:
        return target_cache_key(target)
    scheme = effective_scheme(target)
    host = _login_host(getattr(target, "host", None))
    port = getattr(target, "port", None) or DEFAULT_PORTS[scheme]
    fqdn = str(getattr(target, "fqdn", None) or "").strip().lower()
    return (
        str(target.tenant_id),
        str(secret_ref),
        scheme,
        host,
        str(port),
        fqdn,
        *("" if value is None else str(value) for value in login_inputs),
    )
