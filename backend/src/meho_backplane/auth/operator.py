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
* ``approver`` — whether this principal holds the **approve-only**
  capability: the right to decide (list / show / approve / reject /
  decide) parked approval requests, orthogonal to :class:`TenantRole`
  and deliberately *decoupled from dispatch* (#3243). Lifted from a
  configurable JWT claim (default ``approver``) and defaults to
  ``False`` so every pre-existing token is unaffected (fail-closed).
  The approvals plane admits a principal that is **either**
  ``operator``-or-higher **or** carries this flag
  (:func:`~meho_backplane.auth.rbac.require_approvals_access`), so a
  dedicated approver can be provisioned with ``read_only`` role + this
  flag — able to clear a four-eyes gate yet denied ``call_operation``
  dispatch (which stays ``operator``-gated). The flag never widens the
  linear role lattice and never grants dispatch; it only unlocks the
  approvals plane. The ``sub``-keyed self-approval refusal (#1401) is
  unchanged — an approver still cannot approve a request it parked.
* ``scopes`` — the OAuth 2.0 scopes granted to *this session*, lifted
  from the standard ``scope`` claim (RFC 9068 §2.2.3; space-delimited,
  configurable via ``JWT_SCOPES_CLAIM_NAME``). Drives the MCP
  agent-surface filter (Initiative #3153, #3154): the default working
  surface is always listed, and the operator planes are additionally
  listed only when the set carries ``mcp:admin``
  (:data:`~meho_backplane.mcp.registry.MCP_ADMIN_SCOPE`). Modelled as a
  ``frozenset`` so the frozen :class:`Operator` stays immutable and the
  membership test is O(1). Distinct from ``capabilities`` (tenant-wide
  provisioned add-ons, shared by every session of the tenant) and
  ``platform_admin`` (a standing cross-tenant flag): a scope is a
  **per-session, explicit opt-in** the client requests at token time
  (``MEHO_MCP_SCOPES``, #3156). Defaults to the empty set so a token that
  carries no ``scope`` claim sees only the working surface (fail-closed —
  a missing/garbage claim never elevates).
* ``runner_id`` — the satellite runner principal's row UUID, lifted from
  a configurable JWT claim (default ``runner_id``) when
  ``principal_kind`` is :attr:`PrincipalKind.RUNNER`. Optional and
  defaults to ``None`` so every non-runner token (user / service /
  agent) materialises with ``runner_id is None``. The claim/kind pair is
  fail-closed at extraction: a ``principal_kind=runner`` token that
  carries no ``runner_id`` is rejected 401 (``missing_runner_id_claim``).
  The gateway guard (:func:`~meho_backplane.auth.runner_guard.assert_runner_scope`)
  binds this value to the runner named by the route so a runner token can
  only fetch its own assignment and submit its own results.
* ``check_runner_dispatch`` — internal-only marker that this operator is the
  in-process check-runner's synthetic *background-dispatch* identity
  (#2757). Set to ``True`` by
  :func:`~meho_backplane.checks.runner._sensor_operator` and by the inbound
  event intake's :func:`~meho_backplane.events.ingest.service._build_ingest_operator`
  (both present the check runner's token, so both log in under its role);
  defaults ``False`` for every other construction site (fail-closed). It
  selects the dedicated
  :attr:`~meho_backplane.settings.Settings.vault_check_runner_role`, when one
  is configured, at the single Vault JWT-login site
  (:func:`~meho_backplane.auth.vault.vault_client_for_operator`) so an
  operator can bound background dispatch to a Vault role scoped to just the
  secrets Sensors read. **Never lifted from a JWT claim** —
  :func:`~meho_backplane.auth.jwt.verify_jwt` constructs every
  :class:`Operator` with explicit keyword arguments and never sets this
  field, so no token can forge check-runner dispatch. Deliberately *not*
  modelled as a :class:`PrincipalKind` value: the synthetic operator keeps
  ``principal_kind=user`` so the policy gate still auto-executes the ``safe``
  ops sensors are restricted to.
* ``tenants`` — every tenant the person is in, with the login group
  (and so the role) in each (#5236). Read from the ``meho_tenants`` claim
  (``JWT_TENANTS_CLAIM_NAME``) by
  :func:`~meho_backplane.auth.jwt._extract_tenants`. One login can list
  several tenants (``docs/decisions/2026-10-28-several-tenants-per-person.md``).
  Empty for a token without the claim and always empty for a machine
  login (agent, service, runner). The role and flags of an entry come
  from :func:`group_grant`, the one table in code that matches
  ``docs/decisions/login-role-groups.md``. When the list is not empty,
  ``tenant_id`` / ``tenant_role`` / ``approver`` hold the values of the
  **current** tenant's entry, picked per request by
  :func:`~meho_backplane.auth.current_tenant.select_current_tenant`
  (#5239); the flat ``tenant_id`` / ``tenant_role`` / ``approver`` claims
  are not used.
* ``tenant_chosen`` — ``False`` only for a status answer (``meho status``,
  ``meho_status``) of a login with several tenants that named no tenant.
  The tenant fields then hold the first entry and must not be used for
  any tenant work. ``True`` everywhere else.
* ``role_from_platform_admin`` — ``True`` when a platform admin works in a
  tenant where they have no role group, so the role (``tenant_admin``)
  comes from the platform admin group (#5239). The audit rows of the
  request record it as ``tenant_role_source: platform_admin``.

Email validation uses pydantic's ``EmailStr`` (powered by
``email-validator``); a malformed ``email`` claim from Keycloak is a
configuration bug and surfaces as a 401 rather than silently propagating
garbage downstream.
"""

from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

__all__ = [
    "MACHINE_PRINCIPAL_KINDS",
    "GroupGrant",
    "Operator",
    "PrincipalKind",
    "RoleGroup",
    "TenantMembership",
    "TenantRole",
    "group_grant",
    "is_human_principal",
]


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
    * :attr:`RUNNER` — a satellite runner's service principal registered
      by ``meho runner-principal register`` (Initiative #2415, #2502);
      the token carries ``principal_kind=runner`` plus a ``runner_id``
      claim. A runner is a dumb, push-only executor with a **read-only**
      credential scope: its token is fail-closed 403'd on every
      authenticated route outside the gateway allowlist prefixes
      (:data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`)
      and on the MCP surface. The unforgeable discriminator is what the
      negative cage keys on, so a runner client missing any other mapper
      still cannot roam the read API.
    """

    USER = "user"
    SERVICE = "service"
    AGENT = "agent"
    RUNNER = "runner"


class RoleGroup(StrEnum):
    """The kind of login group a person is in, inside one tenant.

    The five kinds of ``docs/decisions/login-role-groups.md``. The value is
    the last part of a ``meho_tenants`` entry
    (``<tenant-uuid>:<tenant>:<RoleGroup>``) and the end of the Keycloak
    group name (``MEHO-<tenant>-<RoleGroup>``). The match is exact and
    case-sensitive.
    """

    READERS = "Readers"
    OPERATORS = "Operators"
    SA_OPERATORS = "SA-Operators"
    ADMINS = "Admins"
    APPROVERS = "Approvers"


class GroupGrant(NamedTuple):
    """What one login group gives a person in its tenant.

    * ``role`` — the :class:`TenantRole`.
    * ``approver`` — the person may decide approval requests
      (the same meaning as :attr:`Operator.approver`).
    * ``self_approval`` — the person may approve their own change. Only
      ``SA-Operators`` sets it. Nothing reads it yet: the rule comes with
      the self-approval work (#3327).
    """

    role: TenantRole
    approver: bool
    self_approval: bool


#: The one table from login group kind to role and flags. It must match
#: the table in ``docs/decisions/login-role-groups.md``.
_GROUP_GRANTS: dict[RoleGroup, GroupGrant] = {
    RoleGroup.READERS: GroupGrant(TenantRole.READ_ONLY, approver=False, self_approval=False),
    RoleGroup.OPERATORS: GroupGrant(TenantRole.OPERATOR, approver=False, self_approval=False),
    RoleGroup.SA_OPERATORS: GroupGrant(TenantRole.OPERATOR, approver=False, self_approval=True),
    RoleGroup.ADMINS: GroupGrant(TenantRole.TENANT_ADMIN, approver=False, self_approval=False),
    RoleGroup.APPROVERS: GroupGrant(TenantRole.READ_ONLY, approver=True, self_approval=False),
}


def group_grant(group: RoleGroup) -> GroupGrant:
    """Return the role and the flags that the login group *group* gives.

    The single place that turns a group kind into a role, so the token
    reader, the current-tenant choice and the self-approval rule all
    agree.
    """
    return _GROUP_GRANTS[group]


class TenantMembership(BaseModel):
    """One tenant a person is in, read from one ``meho_tenants`` entry.

    * ``tenant_id`` — the tenant's UUID.
    * ``name`` — the tenant's short name (lowercase letters, digits and
      dashes), as in the Keycloak group name.
    * ``group`` — the person's login group kind in that tenant. Pass it to
      :func:`group_grant` for the role and the flags.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    name: str
    group: RoleGroup


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
    #: The OAuth ``clientId`` of a client-credentials (service-account) token,
    #: recovered from the ``service-account-<clientId>`` username marker
    #: (#3178) or, for :attr:`PrincipalKind.SERVICE` and
    #: :attr:`PrincipalKind.AGENT` tokens without the marker, from the verified
    #: ``azp`` / RFC 9068 ``client_id`` claim (#3795, #3446). ``None`` for
    #: interactive users and any token carrying neither. Used by the audit
    #: parent-linkage seam (#3028, #3446) to key a service principal's
    #: orchestration runs, and by agent grant enforcement (#3795).
    client_id: str | None = None
    capabilities: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    platform_admin: bool = False
    approver: bool = False
    runner_id: UUID | None = None
    check_runner_dispatch: bool = False
    #: The ``auth_time`` claim: when the person last entered their
    #: credentials (seconds since the epoch, OIDC Core section 2). A token
    #: refresh keeps it (RFC 9470 section 6.1). ``None`` when the token has
    #: no such claim, which a service token never has. Read by the fresh
    #: sign-in check (:mod:`meho_backplane.auth.fresh_sign_in`).
    auth_time: int | None = None
    #: Every tenant the person is in, with the login group in each, in
    #: token order (#5236). Empty when the token has no ``meho_tenants``
    #: claim, and always empty for a machine login. The current tenant is
    #: picked from it per request (#5239).
    tenants: tuple[TenantMembership, ...] = ()
    #: How the person signed in, from the ``acr`` claim. ``None`` when absent.
    acr: str | None = None
    #: The Keycloak sign-in session id, from the ``sid`` claim. ``None`` when
    #: absent. With ``auth_time`` it names one sign-in, so one sign-in allows
    #: one approval (#3979).
    sid: str | None = None
    #: ``False`` only for a status answer of a login with several tenants
    #: and no chosen tenant (#5239). Never use the tenant fields then.
    tenant_chosen: bool = True
    #: The role comes from the platform admin group, not a role group (#5239).
    role_from_platform_admin: bool = False


#: Machine (non-human) principal kinds. A human operator authenticates as
#: :attr:`PrincipalKind.USER` (the graceful-fallback default for a token
#: carrying no ``principal_kind`` claim); every other kind is a machine
#: credential — an agent client, a non-MEHO service account, or a satellite
#: runner. The human-only governance verbs (approval decisions, agent-grant
#: create / elevate, standing-grant create / revoke for service principals
#: (#4616), agent-principal register) refuse these kinds on **every**
#: transport (meho-internal#289): approval is a human decision (v0.1-spec §7).
MACHINE_PRINCIPAL_KINDS: frozenset[PrincipalKind] = frozenset(
    {PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER}
)


def is_human_principal(operator: Operator) -> bool:
    """Return ``True`` when *operator* is a human (interactive) principal.

    A human principal authenticates as :attr:`PrincipalKind.USER`. Every
    kind in :data:`MACHINE_PRINCIPAL_KINDS` (agent / service / runner) is a
    machine credential and returns ``False``.

    Single source of truth for the human-vs-machine split. It is consulted
    by the transport-independent REST guard
    (:func:`~meho_backplane.auth.rbac.ensure_human_principal`) and the
    approval service-layer backstop
    (:func:`~meho_backplane.operations.approval_queue._check_reviewer_role`)
    so the two layers enforce one policy — the peer of the MCP transport's
    :mod:`meho_backplane.mcp.human_only` block.
    """
    return operator.principal_kind not in MACHINE_PRINCIPAL_KINDS
