# Add a tenant

A tenant is a separate space in MEHO, with its own systems and
passwords. This guide adds a new tenant to a running MEHO and lets its
first people log in.

!!! warning "You need a second person"

    Every step that changes a Keycloak group runs at the **dangerous**
    risk level. MEHO waits until a **second person** approves each one.
    The self-approval switch (`APPROVAL_ALLOW_SELF_APPROVAL`) does **not**
    cover the dangerous risk level. So **one operator alone cannot add a
    tenant**, even with that switch on.

    Before you start, pick one of these:

    - **A second operator** in your current tenant approves each step.
    - **An agent makes the requests** and you approve them. This is the
      agent-requester pattern in
      [Approvals and break-glass](approvals-and-break-glass.md#option-1-the-agent-requester-pattern-recommended).
      The agent needs a grant with `--verdict needs-approval` on the
      `keycloak.group.*` operations, because agents are refused
      dangerous operations by default.

    This is a decision of 28 October 2026. It stays like this until the
    rules for people who may approve their own changes are done. The
    decision record is
    [tenant groups stay at the dangerous risk level](https://github.com/evoila/meho/blob/main/docs/decisions/2026-10-28-tenant-groups-stay-dangerous.md).

## How a person gets a tenant

MEHO has no "create tenant" command. A tenant is only a UUID in the
login token (the `tenant_id` value). The person's **role group** in
Keycloak puts it there:

- A role group carries two attributes: `tenant_id` (the tenant's UUID)
  and `tenant_role` (`read_only`, `operator` or `tenant_admin`).
- The `tenant-id` and `tenant-role` mappers copy these values from the
  person's group into the token.
- A person in **no** role group gets no tenant. MEHO refuses the login
  with `401 missing_tenant_claim`.
- A person in **two** role groups is refused with
  `401 multiple_role_groups`.

So adding a tenant means: make role groups that carry the new UUID, and
put people in them.

!!! note "Do not put the tenant on the user"

    Do not set `tenant_id` or `tenant_role` as attributes of the user
    when you create the user. With Keycloak's default user profile,
    Keycloak drops these values, and the person still gets
    `missing_tenant_claim`. Always use a role group.

The first tenant of a site is made at install time by
`meho admin keycloak bootstrap-clients` (see
[Keycloak realm setup](../install/keycloak-realm.md#step-5-create-the-five-groups)).
This guide adds the next tenant through MEHO's Keycloak operations, so
each change is approved and stored in the audit log.

## Before you start

- A Keycloak target registered in MEHO (here called `keycloak-main`).
- A Vault target registered in MEHO (here called `vault-main`), and the
  right to write Vault identities and groups.
- An `operator` or `tenant_admin` login in your current tenant.
- A second person who can approve, or the agent setup from the box
  above. Step 8 needs an approver in the new tenant too, because an
  approval belongs to one tenant. Step 8 explains the choices.
- Today one person has one tenant. A person who should work in two
  tenants needs a second Keycloak user for now.

## Step 1: Make the tenant UUID

```bash
uuidgen | tr 'A-Z' 'a-z'
```

Write the UUID down. The steps below call it `<tenant-uuid>`. Pick a
short name for the tenant too, for example `acme`.

## Step 2: Create the role groups

Create one group per role the tenant needs. Name them
`MEHO-<tenant>-<Role>`, like the groups that ship today. Each group
carries the new UUID:

```bash
meho keycloak group create --target keycloak-main --name MEHO-acme-Readers \
  --attribute tenant_id=<tenant-uuid> --attribute tenant_role=read_only
meho keycloak group create --target keycloak-main --name MEHO-acme-Operators \
  --attribute tenant_id=<tenant-uuid> --attribute tenant_role=operator
meho keycloak group create --target keycloak-main --name MEHO-acme-Admins \
  --attribute tenant_id=<tenant-uuid> --attribute tenant_role=tenant_admin
```

Each command waits for approval and prints an approval id. The second
person approves it:

```bash
meho approvals list
meho approvals approve <approval-id>
```

Check the groups and their attributes. The plain table shows only the
name, the path and the id, so ask for JSON:

```bash
meho keycloak group list --target keycloak-main --search MEHO-acme --attributes --json \
  | jq '.result.rows[] | {name, attributes}'
```

Each group must show the new UUID under `tenant_id` and its role under
`tenant_role`. `tenant_role` uses an underscore (`read_only`), never a
hyphen.

## Step 3: Tell MEHO the new group names

MEHO checks that a person is in only one role group. It knows the role
groups by name, from the `JWT_ROLE_GROUPS` setting. The default is the
five `MEHO-*` groups that ship with MEHO.

Add the new names to the setting, through `extraEnv` in your Helm
values. The value replaces the default, so list the five default groups
too:

```yaml
extraEnv:
  - name: JWT_ROLE_GROUPS
    value: >-
      MEHO-Readers,MEHO-Operators,MEHO-SA-Operators,MEHO-Admins,MEHO-Approvers,
      MEHO-acme-Readers,MEHO-acme-Operators,MEHO-acme-Admins
```

Without this step, logins still work, but MEHO does not notice a person
who is in a default group and in a new group at the same time.

## Step 4: Create the users

Create each person with a password that is stored in Vault. Do not put
`tenant_id` or `tenant_role` in the user file.

```bash
cat > alice.json <<'EOF'
{"username": "alice@acme.example", "email": "alice@acme.example", "enabled": true}
EOF
meho keycloak user create --target keycloak-main -f alice.json \
  --password-secret-ref <vault-path-of-the-password> --temporary
```

This step waits for approval too (risk level `caution`).

## Step 5: Put each person in exactly one group

```bash
meho keycloak group member add --target keycloak-main \
  --username alice@acme.example --group-name MEHO-acme-Admins
```

The second person approves it. Check the members:

```bash
meho keycloak group member list --target keycloak-main --id <group-id>
```

## Step 6: Give each person access to the tenant's secrets in Vault

MEHO keeps a tenant's passwords in Vault under
`secret/tenants/<tenant-uuid>/<target>`. Vault lets a person read that
path only when the person's **Vault identity** has two things:

- the metadata `tenant_id=<tenant-uuid>`, and
- membership in a Vault group that carries the policy for the person's
  role.

Keycloak groups do not create either. Without this step, the person
can log in, but every operation that needs a password fails with a
Vault permission error.

The policy bodies and the full recipe are in the runbook
[per-tenant Vault policies](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-tenant-policy.md).
The steps below run that recipe through MEHO's Vault operations, so each
write is approved and stored in the audit log. `vault-main` is your
Vault target. Every identity write runs at the **dangerous** risk level,
so the second person approves each one.

1. **Check that the three shared policies exist.** They are written
   once per site and shared by every tenant:

   ```bash
   meho operation call vault-1.x vault.sys.policy.list --target vault-main
   ```

   The list must contain `meho-tenant-read-only`,
   `meho-tenant-operator` and `meho-tenant-admin`. If one is missing,
   write it from section 2 of the runbook with `vault.sys.policy.write`.

2. **Create the person's Vault identity with the tenant metadata.** The
   identity name is the person's Keycloak user id (the `sub` claim).
   Keycloak's `--username` filter is a partial match, so `alice` also
   finds `alice2`. The `jq` filter below keeps only the exact username
   and stops when it does not find exactly one user:

   ```bash
   meho keycloak user list --target keycloak-main --username alice@acme.example --json \
     | jq -r '[.result.rows[] | select(.username == "alice@acme.example")]
              | if length == 1 then .[0].id else error("expected one user, found \(length)") end'
   meho operation call vault-1.x vault.identity.entity.write --target vault-main \
     --params '{"name": "<keycloak-user-id>", "metadata": {"tenant_id": "<tenant-uuid>"}}'
   ```

   Write down the `entity_id` from the result.

3. **Link the person's login to that identity.** Vault needs an alias
   on the JWT auth mount that MEHO logs in with (`jwt` by default; the
   setting is `VAULT_OIDC_MOUNT_PATH`). Without the alias, Vault makes a
   second, empty identity on the first login, and the metadata from
   step 2 is not used.

   ```bash
   meho vault sys auth-list --target vault-main --json \
     | jq -r '.result.auth_methods["jwt/"].accessor'
   meho operation call vault-1.x vault.identity.entity_alias.write --target vault-main \
     --params '{"name": "<keycloak-user-id>", "canonical_id": "<entity-id>", "mount_accessor": "<accessor>"}'
   ```

4. **Put the identity in the tenant's role group.** One group per
   tenant and role, named `meho-tenant-<tenant-uuid>-<role>`. The group
   carries the policy for that role:

   | Keycloak `tenant_role` | Vault group | Policy |
   |---|---|---|
   | `read_only` | `meho-tenant-<tenant-uuid>-read_only` | `meho-tenant-read-only` |
   | `operator` | `meho-tenant-<tenant-uuid>-operator` | `meho-tenant-operator` |
   | `tenant_admin` | `meho-tenant-<tenant-uuid>-tenant_admin` | `meho-tenant-admin` |

   ```bash
   meho operation call vault-1.x vault.identity.group.write --target vault-main \
     --params '{"name": "meho-tenant-<tenant-uuid>-tenant_admin", "policies": ["meho-tenant-admin"], "member_entity_ids": ["<entity-id>"]}'
   ```

   The member list replaces the old one. When you add a second person
   to a group that exists, list every member again.

Repeat steps 2 to 4 for each person. The Vault group must match the
`tenant_role` of the person's Keycloak group from Step 5.

## Step 7 (optional): Give the tenant its own Vault login role

By default every tenant logs in to Vault with the same role
(`VAULT_OIDC_ROLE`, usually `meho-mcp`). Step 6 already keeps each
tenant's passwords apart. A separate login role adds a second fence,
for example a different token lifetime per tenant:

1. In Vault, create a JWT login role for the tenant. Bind it to the
   tenant with `bound_claims` on `tenant_id`, and give it the same
   policies as the shared role.
2. Add `<tenant-uuid>=<vault-role>` to `VAULT_OIDC_ROLE_BY_TENANT`
   (comma-separated, through `extraEnv`), then restart MEHO.

A wrong entry stops MEHO from starting, so check the value before you
roll it out. The login-role recipe is in the
[connector Vault policy runbook](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-policy.md).

## Step 8: Check the first login and the secret access

The new person logs in and checks the tenant:

```bash
meho login https://meho.example.com
meho status --json | jq .operator
```

The output must show the new `tenant_id` and the expected
`tenant_role`.

Then the new `tenant_admin` checks that Vault accepts the tenant. The
test write below waits for approval, and so does its cleanup. An
approval belongs to one tenant: the second person from your current
tenant cannot see or approve a request made in the new tenant. So the
new tenant needs its own approver first. Pick one:

- **A second person in the new tenant.** Run Steps 4 to 6 again for a
  second person with the `operator` or `tenant_admin` role. You make
  these requests in your current tenant, so your current approver
  approves them as before.
- **An agent requester in the new tenant.** The new `tenant_admin` sets
  up the agent-requester pattern from
  [Approvals and break-glass](approvals-and-break-glass.md#option-1-the-agent-requester-pattern-recommended)
  in the new tenant. The agent needs a grant with
  `--verdict needs-approval` on `vault.kv.delete` for the cleanup, and
  the agent makes the test write and the delete instead of the person.

The self-approval switch is not enough here. It covers the test write
(risk level `caution`), but never the cleanup delete (risk level
`dangerous`).

Note that an approved operation runs as the **approver**, not as the
person who asked for it. So the person who approves the write needs the
Vault identity from Step 6 too. The write checks the approver's Vault
access, and the read checks the reader's.

The new tenant has no targets yet, so register the Vault target in the
new tenant first. It needs no password, because MEHO logs in to Vault
with the person's own token. Then write one test secret, let the
approver approve it, and read it back:

```bash
meho targets add vault-main --product vault --host vault.example.com --no-secret-ref
meho vault kv put --target vault-main secret tenants/<tenant-uuid>/smoke-test --data '{"ok": "yes"}'
# The approver, logged in to the new tenant:
meho approvals list
meho approvals approve <approval-id>
# Back as the first person:
meho vault kv read --target vault-main secret tenants/<tenant-uuid>/smoke-test
```

The read must return `{"ok": "yes"}`. A `permission denied` from Vault
on the write means the identity metadata or the group from Step 6 is
missing for the approver. The same error on the read means it is
missing for the reader.

Delete the test secret afterwards. The delete runs at the `dangerous`
risk level, so the same approver (or the agent setup) is needed again:

```bash
meho vault kv delete --target vault-main secret tenants/<tenant-uuid>/smoke-test --versions 1
# The approver, logged in to the new tenant:
meho approvals approve <approval-id>
```

## If the login fails

| What you see | Cause | Fix |
|---|---|---|
| `missing_tenant_claim` | The person is in no role group. Or the role group has no `tenant_id` attribute, or the client has no `tenant-id` mapper. | Step 5 (add the person to one group), Step 2 (check the attributes), or [Keycloak realm setup, Step 3](../install/keycloak-realm.md#step-3-add-the-six-protocol-mappers). Then log in again. |
| `multiple_role_groups` | The person is in two role groups. | Remove the person from one group (`meho keycloak group member remove`). |
| `malformed_tenant_claim` | `tenant_id` on the group is not a UUID. | Fix it with `meho keycloak group update-attributes`. |
| `unknown_tenant_role` | `tenant_role` on the group is not `read_only`, `operator` or `tenant_admin`. | Fix it with `meho keycloak group update-attributes`. |
| Login works, but an operation fails with `permission denied` from Vault | The person's Vault identity has no `tenant_id` metadata, or is not in the tenant's Vault group. An approved write runs as the approver, so this can be the approver's identity. | Step 6 for that person. Check with section 5 of the [per-tenant Vault policies runbook](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-tenant-policy.md). |
| `approve` answers `self_approval_forbidden` | You asked for the change and tried to approve it yourself. | A second person approves, or use the agent-requester pattern (see the box at the top). |
| `meho approvals list` in the new tenant shows nothing, but a request is waiting | The request was made in another tenant. Approvals belong to one tenant. | Log in to the tenant where the request was made, or set up an approver in the new tenant (Step 8). |
