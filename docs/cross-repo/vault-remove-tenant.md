<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Remove a tenant from Vault

When a tenant is finished, remove everything it has in Vault. Leftover
login roles, policies and secrets are access that nobody watches.

This page lists the steps in order. For each step it names:

- the MEHO operation, or the Vault command when MEHO has no operation yet;
- the Vault role the step runs under;
- the policy rule that role needs.

A tenant can own up to five things in Vault:

| What | Where |
| --- | --- |
| Its entry in the tenant → login role setting | `VAULT_OIDC_ROLE_BY_TENANT` (backplane setting) |
| Its secrets | `secret/tenants/<tenant_id>/` |
| Its own login role (a JWT auth role) | `auth/<mount>/role/<role>` |
| Its own policy | `sys/policy/<policy>` |
| Its identity groups | `identity/group/name/meho-tenant-<tenant_id>-<role>` |

Skip a row that your deployment does not use. For example, a deployment
that uses only the shared role `meho-mcp` has no tenant login role.

## Before you start

- **Find out which Vault role a call uses.** MEHO picks the role in this
  order: the `vault_role` of the vault target you call, then the
  per-tenant role from `VAULT_OIDC_ROLE_BY_TENANT`, then the global
  `VAULT_OIDC_ROLE` (usually `meho-mcp`). The full order is in
  [`connector-vault-policy.md`](./connector-vault-policy.md) §6.3.
- **The person who approves also needs the rights.** After approval, the
  call runs with the approver's login, not the requester's. Both must pass
  the Vault policy, and both must be in the same MEHO tenant (approvals
  stay inside one tenant).
- **Secrets can only be removed by users of that tenant.** MEHO lets an
  operator change secrets only under `secret/tenants/<their own
  tenant_id>/`. So for step 2, the requester and the approver must both be
  users of the tenant you remove. Do step 2 before you remove the tenant's
  users from your identity provider.
- **Check first, then change.** Every step below that changes Vault
  needs approval from a second person. Read the current state first, so
  the approver sees what will go.

## Step 1: Stop new logins under the tenant's role

Remove the tenant's entry from `VAULT_OIDC_ROLE_BY_TENANT` and roll out the
setting (it is set through the chart's `extraEnv`). From then on, no new
MEHO login uses the tenant's role. The tenant's users log in under
`VAULT_OIDC_ROLE` instead.

- Operation: none. This is a deployment change.
- Skip this step when the tenant has no entry in the setting.

## Step 2: Delete the tenant's secrets

1. List the secrets: `vault.kv.list` with `mount=secret`,
   `path=tenants/<tenant_id>`. Repeat for each sub-folder in the answer.
2. Remove each secret for good: `vault.kv.metadata.delete`. It removes
   every version and the metadata. It cannot be undone.

   If you want a way back for some time first, use `vault.kv.delete`
   instead. It is a soft delete: Vault can undelete the versions. Run
   `vault.kv.metadata.delete` later to remove them for good.

Each call needs its own approval today. `vault.kv.folder.delete` (a soft
delete of many secrets with one approval) is planned in #3939 and not
released yet.

**Which role runs it.** Use one of these two ways:

- **A teardown target (recommended).** Register a vault target whose
  `vault_role` names a teardown role, as in
  [`vault-provisioning.md`](./vault-provisioning.md) §8, and call the
  operations on that target. The target's role is used even while
  `VAULT_OIDC_ROLE_BY_TENANT` still names a tenant role. Give the
  teardown role these rules for the tenant folder:

  ```hcl
  # List the folder (vault.kv.list) and remove secrets for good
  # (vault.kv.metadata.delete reads the path before and after it deletes).
  path "secret/metadata/tenants/{{identity.entity.metadata.tenant_id}}/*" {
    capabilities = ["read", "list", "delete"]
  }
  # Soft delete (vault.kv.delete) sends POST secret/delete/<path>.
  path "secret/delete/tenants/{{identity.entity.metadata.tenant_id}}/*" {
    capabilities = ["update"]
  }
  ```

  The template needs the `tenant_id` metadata on each user's Vault
  identity entity ([`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
  §3). If your entities do not carry it, write the tenant id in its place
  for this one teardown, and remove those rules again afterwards.

- **The tenant's own admin rights.** Call the operations on a vault
  target without a `vault_role`. A `tenant_admin` user of the tenant has
  the `meho-tenant-admin` policy through the tenant's identity group
  ([`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
  §2.3). That policy allows `vault.kv.list`, `vault.kv.metadata.delete`
  and `vault.kv.delete`. Policies written from that page before the
  `secret/delete/tenants/...` rule was added do not allow
  `vault.kv.delete`; add the rule first.

MEHO checks the role's rights before it asks for approval. If a right is
missing, the call is refused with `write_capability_denied` and nobody is
asked to approve.

If no user of the tenant is left, MEHO cannot remove the secrets. A Vault
admin runs:

```bash
vault kv list -mount=secret tenants/<tenant_id>
vault kv metadata delete -mount=secret tenants/<tenant_id>/<key>
```

## Step 3: Delete the tenant's login role

MEHO has no operation that deletes a JWT login role. Operations to read,
list and change JWT login roles are planned in #3694; a delete is not part
of that work. Until MEHO has one, this is a step for a Vault admin:

```bash
# Check that it is the right role, and that no other tenant in
# VAULT_OIDC_ROLE_BY_TENANT still uses it.
vault read auth/<mount>/role/<role>
vault delete auth/<mount>/role/<role>
```

`<mount>` is the JWT auth mount (`VAULT_OIDC_MOUNT_PATH`, default `jwt`).
The admin needs `read` and `delete` on `auth/<mount>/role/<role>`.

## Step 4: Delete the tenant's own policy

Delete a policy only when it was made for this tenant alone. Do **not**
delete the shared policies `meho-tenant-read-only`,
`meho-tenant-operator` and `meho-tenant-admin`: every tenant uses them.
Vault does not check whether a role or group still names a policy, so do
step 3 first.

1. Read it: `vault.sys.policy.read` with `name=<policy>`.
2. Delete it: `vault.sys.policy.delete` with `name=<policy>`.

**Which role runs it.** Call both on a vault target whose `vault_role`
names a role that may delete policies. Do not give this right to
`meho-mcp`: every operator logs in with that role. Give the role a rule
for exactly the policies it may delete:

```hcl
# vault.sys.policy.delete sends DELETE /v1/sys/policy/<policy>.
# vault.sys.policy.read needs `read` on the same path.
path "sys/policy/<policy>" {
  capabilities = ["read", "delete"]
}
```

Use a `*` at the end of the path (for example `sys/policy/meho-tenant-*`)
only for a role that may delete every policy with that name start.

MEHO checks `delete` on `sys/policy/<policy>` under the target's role
before it asks for approval. If the right is missing, the call is refused
with `write_capability_denied`. On a target without a `vault_role`, the
call uses `meho-mcp` (or the per-tenant role), which usually has no
`delete` there, so it is refused at once.

## Step 5: Delete the tenant's identity groups

If you followed
[`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
§4, the tenant has up to three groups:
`meho-tenant-<tenant_id>-read_only`, `-operator` and `-tenant_admin`.

1. Read each: `vault.identity.group.read`.
2. Delete each: `vault.identity.group.delete`.

**Which role runs it.** The identity operations do not use the target's
`vault_role` yet. They run under the caller's normal role (the per-tenant
role, else `VAULT_OIDC_ROLE`), and MEHO does not check the rights before
it asks for approval. That role needs:

```hcl
path "identity/group/name/meho-tenant-<tenant_id>-*" {
  capabilities = ["read", "delete"]
}
```

If you do not want to give that rule to the normal role, a Vault admin
runs `vault delete identity/group/name/<group>` for each group.

## Check that nothing is left

```bash
vault kv list -mount=secret tenants/<tenant_id>     # expect: no value found
vault list auth/<mount>/role                        # the tenant's role is gone
vault policy list                                   # the tenant's policy is gone
vault list identity/group/name                      # no meho-tenant-<tenant_id>-* group
```

## References

- Role order and write rules: [`connector-vault-policy.md`](./connector-vault-policy.md) §6
- Tenant policies and groups: [`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
- Teardown target and `vault_role`: [`vault-provisioning.md`](./vault-provisioning.md) §8
- MEHO's tenant check for secrets: [`connectors-vault-tenant-scope.md`](../codebase/connectors-vault-tenant-scope.md)
- Vault — [`/sys/policy` API](https://developer.hashicorp.com/vault/api-docs/system/policy)
- Vault — [policies and capabilities](https://developer.hashicorp.com/vault/docs/concepts/policies)
