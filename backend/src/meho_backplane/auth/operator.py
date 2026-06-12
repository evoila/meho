# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The :class:`Operator` model — validated identity passed to routes.

An :class:`Operator` is what every authenticated route handler receives
via ``Depends(verify_jwt)``. The model is **frozen** (pydantic v2
``ConfigDict(frozen=True)``) so a route handler can stash the operator
on request state, log it, and forward it to downstream services without
fear of mutation creating a confused-deputy bug.

Field choices reflect what G2.2 / G2.3 / G0.1 consumers actually need:

* ``sub`` — the OIDC subject identifier; the stable operator id.
  Required.
* ``name`` / ``email`` — soft identity bound from the matching JWT
  claims when present. Used by audit middleware (G2.3) for
  human-readable rows and by the CLI for the ``meho status`` greeting.
* ``raw_jwt`` — the original Bearer token string. Required because
  G2.2-T2's Vault forward-auth passes the original JWT (not a re-issued
  one) to Vault's OIDC auth method; the chain of custody must be
  preserved bit-for-bit.
* ``tenant_id`` — the UUID of the tenant the operator acts on behalf
  of, lifted from the configurable JWT claim (default ``tenant_id``).
  Required: G0.1's tenancy model treats every authenticated request as
  scoped to exactly one tenant; downstream Tasks (T3 contextvar
  binding, T4 RBAC, the future per-tenant query filters) all assume
  this field is present and well-typed.
* ``tenant_role`` — the operator's role within the tenant. Modelled as
  a closed :class:`TenantRole` enum so the RBAC primitive in T4 can be
  exhaustive (``tenant_admin`` / ``operator`` / ``read_only``).
* ``principal_kind`` — whether this principal is a human user, a
  service account, or an agent (G11.2-T1). Defaults to ``user`` so
  existing human-operator tokens that carry no ``principal_kind`` claim
  are unaffected. Agents authenticate via Keycloak clients tagged
  ``kind=agent`` whose tokens carry ``principal_kind=agent``; dispatch
  and audit can branch on this field without touching the identity chain.
* ``capabilities`` — the set of tenant-provisioned capability keys the
  operator's tenant has enabled (G4.5-T1). Lifted from a configurable
  JWT claim (default ``capabilities``). Drives the MCP capability gate:
  a tool carrying ``required_capability="x"`` is absent from
  ``tools/list`` and 403s on ``tools/call`` unless ``"x"`` is in this
  set. Modelled as a ``frozenset`` so the frozen :class:`Operator` stays
  immutable and the membership test is O(1). Defaults to the empty set
  so tokens minted before the capability mapper existed simply see no
  capability-gated tools (fail-closed).
* ``platform_admin`` — whether this principal holds the cross-tenant
  *platform* capability, orthogonal to :class:`TenantRole` (which is
  scoped *within* a single tenant). Lifted from a configurable JWT claim
  (default ``platform_admin``) and defaults to ``False`` so every token
  minted before the claim existed — and every agent / service principal —
  materialises as non-platform-admin (fail-closed). No surface consumes
  this field yet; it is the substrate a later cross-tenant authorization
  gate will check, so that a ``tenant_admin`` cannot be mistaken for a
  platform operator on the strength of role rank alone.

Email validation uses pydantic's ``EmailStr`` (powered by
``email-validator``); a malformed ``email`` claim from Keycloak is a
configuration bug and surfaces as a 401 rather than silently propagating
garbage downstream.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

__all__ = ["Operator", "PrincipalKind", "TenantRole"]


class TenantRole(StrEnum):
    """Per-tenant role granted to the operator by the JWT issuer.

    The set is intentionally small in v0.2: a closed three-value enum
    lets the RBAC primitive (Task #234, ``require_role``) make
    exhaustive comparisons without leaking arbitrary string handling
    into route code. A richer policy engine — topology-aware
    permissions, ABAC, approval workflows — is a separate v0.2.next
    Goal; widening this enum is the only ratcheting mechanism in the
    interim.

    Values are the literal strings the Keycloak protocol-mapper recipe
    (Task #235) emits, so a JWT carrying ``"tenant_admin"`` materialises
    cleanly as :attr:`TENANT_ADMIN`. ``StrEnum`` (PEP 663, stdlib in
    3.11+) gives the members ``str`` semantics for free, so
    ``f"role={role}"`` renders as ``"role=tenant_admin"`` rather than
    ``"role=TenantRole.TENANT_ADMIN"``.
    """

    TENANT_ADMIN = "tenant_admin"
    OPERATOR = "operator"
    READ_ONLY = "read_only"


class PrincipalKind(StrEnum):
    """Discriminator that distinguishes what authenticated this request.

    G11.2-T1 (#815) adds this field to :class:`Operator` so dispatch,
    audit, and the approval gate can tell a human operator apart from
    a service account or an agent without re-examining the JWT payload.

    Values are the literal strings the Keycloak ``principal_kind``
    protocol mapper emits on the agent client (or that the v0.3 token
    exchange layer will emit). Existing tokens that carry no
    ``principal_kind`` claim default to :attr:`USER` — the graceful-
    fallback contract means all pre-G11.2 human-operator flows are
    unaffected.

    Members:

    * :attr:`USER` — a human operator authenticated via the interactive
      device-code flow. Default when no claim is present.
    * :attr:`SERVICE` — a non-interactive service account client that
      uses client-credentials flow but is not a MEHO-managed agent.
    * :attr:`AGENT` — a Keycloak client registered by
      ``meho agent-principal register``; the token carries
      ``principal_kind=agent``. G11.2-T2 (RFC 8693 delegation) and
      G11.2-T3 (per-principal permission model) branch on this value
      to apply agent-specific authz.
    """

    USER = "user"
    SERVICE = "service"
    AGENT = "agent"


class Operator(BaseModel):
    """Validated operator identity extracted from a verified JWT.

    ``raw_jwt`` is excluded from :meth:`__repr__` (``Field(repr=False)``)
    so that an :class:`Operator` accidentally bound into a structured
    log record — e.g. via ``logger.bind(operator=op)`` under structlog,
    whose JSON renderer calls ``repr()`` on non-primitive values — never
    leaks the bearer token to stdout or any downstream log shipper.
    The model field is still populated and accessible by name; only the
    default string representation is sanitised.
    """

    model_config = ConfigDict(frozen=True)

    sub: str
    name: str | None = None
    email: EmailStr | None = None
    raw_jwt: str = Field(repr=False)
    tenant_id: UUID
    tenant_role: TenantRole
    principal_kind: PrincipalKind = PrincipalKind.USER
    capabilities: frozenset[str] = frozenset()
    platform_admin: bool = False
