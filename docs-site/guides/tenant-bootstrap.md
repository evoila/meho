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
- An `operator` or `tenant_admin` login in your current tenant.
- A second person who can approve, or the agent setup from the box
  above.
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

Check the groups and their attributes:

```bash
meho keycloak group list --target keycloak-main --search MEHO-acme --attributes
```

`tenant_role` uses an underscore (`read_only`), never a hyphen.

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

## Step 6 (optional): Give the tenant its own Vault login role

By default every tenant logs in to Vault with the same role
(`VAULT_OIDC_ROLE`, usually `meho-mcp`). To keep the new tenant's
passwords apart from the other tenants:

1. In Vault, create a JWT login role for the tenant. Bind it to the
   tenant with `bound_claims` on `tenant_id`, and give it a policy for
   the tenant's secret paths.
2. Add `<tenant-uuid>=<vault-role>` to `VAULT_OIDC_ROLE_BY_TENANT`
   (comma-separated, through `extraEnv`), then restart MEHO.

A wrong entry stops MEHO from starting, so check the value before you
roll it out. The full policy recipe is in the
[connector Vault policy runbook](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-policy.md).

## Step 7: Check the first login

The new person logs in and checks the tenant:

```bash
meho login https://meho.example.com
meho status --json | jq .operator
```

The output must show the new `tenant_id` and the expected
`tenant_role`.

## If the login fails

| What you see | Cause | Fix |
|---|---|---|
| `missing_tenant_claim` | The person is in no role group. Or the role group has no `tenant_id` attribute, or the client has no `tenant-id` mapper. | Step 5 (add the person to one group), Step 2 (check the attributes), or [Keycloak realm setup, Step 3](../install/keycloak-realm.md#step-3-add-the-six-protocol-mappers). Then log in again. |
| `multiple_role_groups` | The person is in two role groups. | Remove the person from one group (`meho keycloak group member remove`). |
| `malformed_tenant_claim` | `tenant_id` on the group is not a UUID. | Fix it with `meho keycloak group update-attributes`. |
| `unknown_tenant_role` | `tenant_role` on the group is not `read_only`, `operator` or `tenant_admin`. | Fix it with `meho keycloak group update-attributes`. |
| `approve` answers `self_approval_forbidden` | You asked for the change and tried to approve it yourself. | A second person approves, or use the agent-requester pattern (see the box at the top). |
