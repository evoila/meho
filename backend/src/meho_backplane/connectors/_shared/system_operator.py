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
**frozen system operator with an empty ``raw_jwt``** — the same shape the
topology scheduler
(:func:`meho_backplane.topology.scheduler._system_operator`) and the
connector ``execute`` shims (e.g.
:func:`meho_backplane.connectors.vault.connector.VaultConnector._synthesise_legacy_operator`)
already use.

The empty ``raw_jwt`` is load-bearing: per the locked decision, an
operator-context Vault credential loader that receives an operator with
``raw_jwt == ""`` MUST fail closed with a clear error rather than silently
falling back to a backplane identity. System-initiated calls that need a
vendor credential are out of scope for v0.x; today the only system callers
(readiness probes) hit unauthenticated endpoints and forward no token, so
the stub loader's existing ``NotImplementedError`` is never reached on
those paths and behaviour is unchanged.
"""

from __future__ import annotations

from uuid import UUID

from meho_backplane.auth.operator import Operator, TenantRole

__all__ = ["SYSTEM_OPERATOR_SUB", "synthesise_system_operator"]

#: Greppable sentinel ``sub`` for the synthesised system operator. Lands
#: on any audit row a system-initiated connector call writes.
SYSTEM_OPERATOR_SUB: str = "system:connector-probe"

#: Nil UUID tenant for the synthesised operator. Typed/ingested
#: registrations are ``tenant_id IS NULL``, so the dispatcher's
#: tenant-scoped descriptor lookup falls through to the global row
#: regardless of this value; it only lands on the audit row.
_SYSTEM_TENANT_ID: UUID = UUID(int=0)


def synthesise_system_operator() -> Operator:
    """Return a frozen system :class:`Operator` with an empty ``raw_jwt``.

    Used by the operator-less connector probe/fingerprint paths that
    reach the HTTP auth surface before a real operator exists. The empty
    ``raw_jwt`` makes any future operator-context Vault read fail closed
    (it cannot authenticate to Vault), which is the intended boundary for
    system-initiated credential reads.
    """
    return Operator(
        sub=SYSTEM_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=_SYSTEM_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )
