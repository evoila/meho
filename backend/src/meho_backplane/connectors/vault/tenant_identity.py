# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scoped Vault identity wiring for the templated per-tenant policies.

#1724 (Goal #221, Initiative #1685). The per-tenant secret layout
(``secret/data/tenants/<tenant_id>/*``) is authorised by **templated** ACL
policies keyed on ``{{identity.entity.metadata.tenant_id}}`` — see the deploy
runbook ``docs/cross-repo/connector-vault-tenant-policy.md``. For that template
to resolve, two facts must be wired into Vault's identity store:

1. the operator's identity **entity** must carry the operator's ``tenant_id`` in
   its ``metadata`` (so ``{{identity.entity.metadata.tenant_id}}`` renders to a
   literal UUID at request time), and
2. the operator's **role** must map onto exactly one of the ≤3 templated
   policies, bound through an identity group (``vault.identity.group.write``'s
   ``policies`` value).

This module is the single source of truth for both mappings. It is a pure,
side-effect-free helper layer: the values it returns are passed verbatim into
the existing ``vault.identity.entity.write`` (``metadata=``) and
``vault.identity.group.write`` (``policies=``) ops by a deploy-time caller
(operator tooling / a provisioning script). It deliberately performs no Vault
I/O of its own — the substrate stays dumb; the identity writes go through the
already-approval-gated identity ops.

The role→policy table here is **authoritative**: the runbook's role→capability
table and these names must agree, and a regression test pins the mapping
exhaustive over :class:`~meho_backplane.auth.operator.TenantRole` so a new role
cannot be added without a deliberate policy decision.
"""

from __future__ import annotations

from meho_backplane.auth.operator import Operator, TenantRole

__all__ = [
    "TENANT_POLICY_FOR_ROLE",
    "tenant_entity_metadata",
    "tenant_group_name",
    "tenant_group_policies",
]


#: The metadata key the templated ACL policies reference as
#: ``{{identity.entity.metadata.tenant_id}}``. Centralised so the helper and
#: the runbook cannot drift on the spelling.
TENANT_ID_METADATA_KEY: str = "tenant_id"


#: Authoritative role → templated-policy-name mapping. Each :class:`TenantRole`
#: binds to exactly one of the ≤3 policies documented in
#: ``docs/cross-repo/connector-vault-tenant-policy.md``:
#:
#: * ``read_only``    → ``meho-tenant-read-only``  (read,list)
#: * ``operator``     → ``meho-tenant-operator``   (read,list + update on the
#:   rotation sub-path)
#: * ``tenant_admin`` → ``meho-tenant-admin``      (full CRUD + the
#:   ``privileged/*`` break-glass sub-path)
#:
#: The three roles map onto three distinct policies, all templated on the same
#: shared per-tenant path — roles are *capabilities on the shared path*, not
#: separate key spaces. A regression test pins this dict exhaustive over
#: :class:`TenantRole`.
TENANT_POLICY_FOR_ROLE: dict[TenantRole, str] = {
    TenantRole.READ_ONLY: "meho-tenant-read-only",
    TenantRole.OPERATOR: "meho-tenant-operator",
    TenantRole.TENANT_ADMIN: "meho-tenant-admin",
}


def tenant_entity_metadata(operator: Operator) -> dict[str, str]:
    """Build the entity ``metadata`` carrying the operator's ``tenant_id``.

    The returned dict is passed verbatim as the ``metadata`` param to
    ``vault.identity.entity.write`` so the operator's Vault identity entity
    carries ``tenant_id`` and the templated policy
    ``secret/data/tenants/{{identity.entity.metadata.tenant_id}}/*`` resolves
    to the operator's own tenant at request time.

    ``operator.tenant_id`` is a :class:`uuid.UUID`; it is rendered in its
    canonical dashed lowercase form (``str(UUID)``) to match how tenant UUIDs
    appear everywhere else (audit rows, JWT claims, the
    :mod:`~meho_backplane.connectors.vault.tenant_scope` guard's rendered
    prefix). Vault stores entity metadata values as strings, so the UUID must
    be stringified here rather than handed across as a ``UUID`` object.
    """
    return {TENANT_ID_METADATA_KEY: str(operator.tenant_id)}


def tenant_group_policies(role: TenantRole) -> list[str]:
    """Return the ``policies`` list to bind a tenant role through a group.

    The returned list is passed verbatim as the ``policies`` param to
    ``vault.identity.group.write`` for the role's identity group. Exactly one
    templated policy is attached per role (see :data:`TENANT_POLICY_FOR_ROLE`);
    binding via the group means every member entity inherits the policy without
    a per-entity policy edit.
    """
    return [TENANT_POLICY_FOR_ROLE[role]]


def tenant_group_name(tenant_id: str, role: TenantRole) -> str:
    """Build the identity-group name for a (tenant, role) pair.

    Convention: ``meho-tenant-<tenant_id>-<role>`` (e.g.
    ``meho-tenant-3f...-operator``). One group per (tenant, role) keeps the
    group's policy binding scoped — the templated policy already constrains the
    *path* to the entity's ``tenant_id`` metadata, so the group only needs to
    carry the role's capability policy and the tenant's operator entities as
    members. The name is a flat handle (no slashes), matching Vault's group-name
    constraint.
    """
    return f"meho-tenant-{tenant_id}-{role.value}"
