# Tenant groups stay at the dangerous risk level (decision)

**Status:** decided by the product owner on 28 October 2026
**Date:** 2026-10-28
**Task:** [#3872](https://github.com/evoila/meho/issues/3872)
**Initiative:** [#3978](https://github.com/evoila/meho/issues/3978) (several tenants in one MEHO)
**Revisit when:** [#3325](https://github.com/evoila/meho/issues/3325) (people who may approve their own changes) is done

## The decision in short

- The four Keycloak group operations keep the **dangerous** risk level:
  `keycloak.group.create`, `keycloak.group.update_attributes`,
  `keycloak.group.member.add` and `keycloak.group.member.remove`.
- A second person must approve each one. The self-approval switch
  (`APPROVAL_ALLOW_SELF_APPROVAL`) still does not cover them.
- There is **no** lower risk level for a new tenant, also not when the
  requester is `tenant_admin` of another tenant.
- `keycloak.user.create` gets no runtime warning for `tenant_*`
  attributes. Its description for agents says to use a role group
  instead, and the guide says the same.
- The path is written down in the guide
  [Add a tenant](../../docs-site/guides/tenant-bootstrap.md).

## Why

- A role group with `tenant_id` and `tenant_role`, and a membership in
  it, give a person access to a tenant. That is the same kind of change
  as giving someone a role. A second person should see it.
- A lower risk level "for a new tenant" is hard to check safely. A group
  for a new tenant UUID looks the same as a group that copies an
  existing tenant's UUID. MEHO would have to know every tenant to tell
  them apart, and it does not keep a tenant list.
- A `tenant_admin` of tenant A has no rights in tenant B. Giving that
  person an easier path to make tenant B would give them rights outside
  their own tenant.
- One operator is not stuck: a second operator approves, or an agent
  makes the requests and the operator approves (the agent-requester
  pattern).
- A runtime warning in `keycloak.user.create` would only help when
  Keycloak drops the values. The agent description and the guide reach
  the same people before they make the mistake.

## What this changes

- Nothing in the risk levels or the approval code.
- The `missing_tenant_claim` error now says the usual cause (the person
  is in no role group) and links the guide. The code stays the first
  word of the error text, so log searches still work.
- `meho status` says the same for a login without a tenant, instead of
  "run `meho login`".
- The agent descriptions of the group operations say that a second
  person must approve, and link the guide.
