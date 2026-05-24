# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthesised system :class:`Operator` for operator-less connector calls.

A handful of connector code paths reach the HTTP auth surface
(:meth:`HttpConnector.auth_headers` and the ``_get_json`` / ``_post_json``
transports) *before* a real operator is in scope:

* ``fingerprint()`` / ``probe()`` issue a pre-session ``GET`` against the
  vendor's about/health endpoint — there is no operator on the readiness
  probe path.
* Vendor connectors that establish a session lazily call their own
  ``auth_headers`` from inside ``fingerprint()`` with no operator to thread.

The HTTP auth surface now carries the full :class:`Operator`
(G3.9-T1, threading the operator's identity to where the per-target Vault
credential read will run — see
[docs/architecture/connector-auth.md](docs/architecture/connector-auth.md)).
Those operator-less paths therefore need a stand-in. This helper builds a
**frozen system operator with a non-empty placeholder ``raw_jwt``** so the
cache fast-path defense-in-depth guard (G3.10 hygiene -- empty-jwt
rejection at :meth:`CredentialsCache.get` and each connector's
session-token method) doesn't block the probe path. The placeholder is
**not** a valid Keycloak JWT, so the live Vault loader still rejects the
operator-context read with a clean :class:`VaultClientError` -- preserving
the architectural posture that **system-initiated calls cannot perform an
operator-context Vault read**. The fail-closed semantic moves from
"empty-string sentinel at every layer" to "every Vault-touching layer
rejects unauthenticated operators by checking either the JWT shape (cache
fast-path) or the JWT validity (live Vault round-trip)".

Tests with injected stub loaders are unaffected: the stub returns canned
credentials without touching Vault, the cache fast-path accepts the
placeholder, and the probe wire format is exercised as before.
"""

from __future__ import annotations

from uuid import UUID

from meho_backplane.auth.operator import Operator, TenantRole

__all__ = [
    "SYSTEM_OPERATOR_PLACEHOLDER_JWT",
    "SYSTEM_OPERATOR_SUB",
    "synthesise_system_operator",
]

#: Greppable sentinel ``sub`` for the synthesised system operator. Lands
#: on any audit row a system-initiated connector call writes.
SYSTEM_OPERATOR_SUB: str = "system:connector-probe"

#: Non-empty placeholder JWT for the synthesised system operator. NOT a
#: valid Keycloak JWT -- the live Vault JWT/OIDC auth method rejects it
#: with :class:`VaultClientError`, preserving the "system-initiated calls
#: cannot read per-target vendor credentials" carve-out. The non-empty
#: value satisfies the cache fast-path's defense-in-depth guard
#: (G3.10 hygiene) so probe/fingerprint paths that go through
#: :meth:`auth_headers` -> cache fast-path don't fail synchronously before
#: the wire call; production cache-miss paths still fail closed at the
#: live Vault loader. The string is deliberately greppable in audit logs.
SYSTEM_OPERATOR_PLACEHOLDER_JWT: str = "system:connector-probe-placeholder-jwt"

#: Nil UUID tenant for the synthesised operator. Typed/ingested
#: registrations are ``tenant_id IS NULL``, so the dispatcher's
#: tenant-scoped descriptor lookup falls through to the global row
#: regardless of this value; it only lands on the audit row.
_SYSTEM_TENANT_ID: UUID = UUID(int=0)


def synthesise_system_operator() -> Operator:
    """Return a frozen system :class:`Operator` with a placeholder ``raw_jwt``.

    Used by the operator-less connector probe/fingerprint paths that
    reach the HTTP auth surface before a real operator exists. The
    :data:`SYSTEM_OPERATOR_PLACEHOLDER_JWT` placeholder is **not** a
    valid Keycloak JWT, so any live operator-context Vault read still
    fails closed at the JWT/OIDC auth method (:class:`VaultClientError`)
    -- the architectural intent. The non-empty shape only matters for
    the cache fast-path's defense-in-depth guard at
    :meth:`CredentialsCache.get`: an empty ``raw_jwt`` would short-circuit
    every system-initiated probe before the wire call even when an
    injected (test) loader could supply the credentials.
    """
    return Operator(
        sub=SYSTEM_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt=SYSTEM_OPERATOR_PLACEHOLDER_JWT,
        tenant_id=_SYSTEM_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )
