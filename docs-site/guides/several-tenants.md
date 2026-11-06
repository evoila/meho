# One person in several tenants

A tenant is a separate space in MEHO, with its own systems and
passwords. This guide lets one person work in more than one tenant with
one login. At the end:

- the person logs in once,
- MEHO lists all of their tenants, with their role in each,
- they pick one tenant at a time in the web interface, on the command
  line or in the MCP settings, and switch without logging out.

!!! note "A site with one tenant needs to do nothing"

    MEHO behaves exactly as before. Every person keeps their tenant and
    their role, and no request needs a tenant header. A test in CI
    checks this against a real Keycloak, before and after the setup adds
    the tenant list. See [Upgrades](../install/upgrades.md#version-specific-notes).

!!! warning "Each change waits for approval"

    Putting a person in a group, or taking them out, runs at the
    **dangerous** risk level when you do it through MEHO, so each change
    waits for approval. Only a **MEHO-SA-Operator** may approve their own
    change; anyone else needs a second person (a MEHO-SA-Operator or an
    admin). The self-approval switch does not cover the dangerous risk
    level (decision record:
    [tenant groups stay at the dangerous risk level](https://github.com/evoila/meho/blob/main/docs/decisions/2026-10-28-tenant-groups-stay-dangerous.md)).

## How it works

- Every role group in Keycloak carries the attribute `meho_tenant`. Its
  value is `<tenant-uuid>:<tenant>:<Role>`, for example
  `<tenant-uuid>:team-b:Operators`.
- The Keycloak mapper `meho-tenants` copies the values of **all** of the
  person's groups into the login token, as the list `meho_tenants`.
- MEHO always works in **one current tenant**. A request names it in
  the `Meho-Tenant` header (a tenant name or ID). The CLI and the MCP
  connection helpers send this header for you. The web interface keeps
  the current tenant in your web session.
- The role in the current tenant comes from the person's group in that
  tenant. A role in another tenant never applies.
- A login with one tenant needs no header. A login with several tenants
  and no header gets `400 tenant_not_chosen`, with the list of tenants.

The design is in the
[decision record](https://github.com/evoila/meho/blob/main/docs/decisions/2026-10-28-several-tenants-per-person.md).

## Before you start

- Your realm has the `meho-tenants` mapper and the `meho_tenant`
  attribute on the five groups of your first tenant. The standard setup
  adds them from v0.39.0 on. To add them to an older realm, run the
  setup again (see [Upgrades](../install/upgrades.md#version-specific-notes)).
- MEHO v0.40.0 or later. In v0.40.0 to v0.43.0 the web interface answers
  `tenant_not_chosen` to a person in several tenants. The tenant picker
  of the web interface comes in the release after v0.43.0. The CLI and
  MCP work from v0.40.0 on.
- A MEHO-SA-Operator who approves their own changes, a second person
  who can approve, or the agent setup from
  [Approvals and break-glass](approvals-and-break-glass.md#option-2-the-agent-requester-pattern).

## Step 1: Add the tenant in Keycloak

A new tenant needs a UUID and a short name. The short name uses
lowercase letters, digits and dashes, for example `team-b`. Make the
UUID once and keep it:

```bash
uuidgen | tr 'A-Z' 'a-z'
```

The tenant gets five role groups. They carry **only** `meho_tenant`, no
`tenant_id` and no `tenant_role`:

| Group | `meho_tenant` | Role in MEHO |
|---|---|---|
| `MEHO-team-b-Readers` | `<tenant-uuid>:team-b:Readers` | `read_only` |
| `MEHO-team-b-Operators` | `<tenant-uuid>:team-b:Operators` | `operator` |
| `MEHO-team-b-SA-Operators` | `<tenant-uuid>:team-b:SA-Operators` | `operator`, may approve their own changes |
| `MEHO-team-b-Admins` | `<tenant-uuid>:team-b:Admins` | `tenant_admin` |
| `MEHO-team-b-Approvers` | `<tenant-uuid>:team-b:Approvers` | `read_only`, may approve |

Pick the way that matches your Keycloak. The two setups are safe to run twice.

### Your own Keycloak

Run the setup command with `--add-tenant`. Pass the **same**
`--tenant-id` as on the first run: without it, the command writes
the all-zero UUID to the five groups of your first tenant. If your
first tenant is not called `main`, pass its name with
`--tenant-name` too.

```bash
KEYCLOAK_ADMIN_PASSWORD=... KEYCLOAK_ADMIN_USER_PASSWORD=... \
meho admin keycloak bootstrap-clients \
  --keycloak-base-url https://keycloak.example.com \
  --realm <realm> \
  --admin-username <keycloak-admin> \
  --mcp-resource-uri https://meho.example.com/mcp \
  --tenant-id <first-tenant-uuid> \
  --admin-user-username <first-admin> \
  --add-tenant team-b \
  --add-tenant-id <tenant-uuid>
```

Use the same values as on your first run. The command does not reset
the password of a user that exists already. Without `--add-tenant-id`,
the command reuses the UUID the groups
already carry, or makes a new one and prints it. Keep it.

The command puts the `meho-tenants` mapper on `meho-cli`,
`meho-cli-approve` and `meho-mcp`. It does not change the web
client `meho-web`. If you use the web interface, add the mapper to
`meho-web` by hand, with the settings in
[Keycloak realm setup, Step 3](../install/keycloak-realm.md#step-3-add-the-protocol-mappers).

### The built-in Keycloak

Add the tenant to the Helm values, then run `helm upgrade`. The
bootstrap Job makes the five groups.

```yaml
coreKeycloak:
  tenantName: main          # the short name of your first tenant
  tenants:
    - name: team-b
      id: <tenant-uuid>
```

The built-in realm already has the `meho-tenants` mapper on
`meho-cli`, `meho-mcp` and `meho-web`.

### By hand

Create the five groups at the top level, each with the one
attribute `meho_tenant` from the table above. Check that the
`meho-tenants` mapper is on every client people log in with, with
**Multivalued** and **Aggregate attribute values** on. The settings
are in [Keycloak realm setup, Step 3](../install/keycloak-realm.md#step-3-add-the-protocol-mappers).

### For every way

Never set `meho_tenant` on a person. With **Aggregate attribute values**
on, Keycloak adds a value on the person to their list. Keep people from
editing their own attributes, as
[Keycloak realm setup, Step 5](../install/keycloak-realm.md#step-5-create-the-five-groups)
says.

## Step 2: Give the tenant its own Vault login role

MEHO keeps a tenant's passwords in Vault under
`secret/tenants/<tenant-uuid>/`. The shared policies of
[Add a tenant, Step 6](tenant-bootstrap.md#step-6-give-each-person-access-to-the-tenants-secrets-in-vault)
read the tenant from the person's Vault identity, and one identity holds
one tenant. So a person in two tenants needs **one Vault login role per
tenant**. Each role can read only its own tenant's path. The rights of
Step 6 do not go away on their own: Vault keeps them on the person's
identity and adds them to every login, with any role. So the person also
leaves the Vault groups of Step 6 (item 4), and you check each role
(item 5).

1. **Write a policy for this tenant's path only.** Put the tenant UUID
   into the path itself. A read-only example:

    ```hcl
    # meho-tenant-team-b: the secrets of the tenant team-b only.
    path "secret/data/tenants/<tenant-uuid>/*" {
      capabilities = ["read"]
    }
    path "secret/metadata/tenants/<tenant-uuid>/*" {
      capabilities = ["read", "list"]
    }
    # privileged/ holds the admin-only secrets. The two rules above
    # cover it too, so refuse it here. The more exact path wins.
    path "secret/data/tenants/<tenant-uuid>/privileged/*" {
      capabilities = ["deny"]
    }
    path "secret/metadata/tenants/<tenant-uuid>/privileged/*" {
      capabilities = ["deny"]
    }
    ```

    The login role of item 2 accepts every role group of the tenant, so
    every member gets the same rights from this policy. Keep it at the
    lowest level the tenant needs. Add the write rights the tenant
    needs in the same way. The rights per role, and the `privileged/`
    rule, are in the
    [per-tenant Vault policies runbook](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-tenant-policy.md),
    section 1. Use the fixed UUID where that runbook has the
    `{{identity.entity.metadata.tenant_id}}` template.

2. **Create the login role and tie it to the tenant.** `bound_claims`
   with the glob type requires an entry `<tenant-uuid>:<anything>` in the
   person's `meho_tenants` list. Vault accepts the login when any entry
   of the list matches.

    ```bash
    vault write auth/jwt/role/meho-tenant-team-b - <<'JSON'
    {
      "role_type": "jwt",
      "user_claim": "sub",
      "bound_audiences": "<keycloak-audience>",
      "bound_claims_type": "glob",
      "bound_claims": {"meho_tenants": "<tenant-uuid>:*"},
      "policies": "meho-mcp,meho-tenant-team-b",
      "token_ttl": "1h"
    }
    JSON
    ```

    `meho-mcp` is the base policy from
    [Vault provisioning](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md),
    section 3. Use your own mount path in place of `jwt`, and your
    `KEYCLOAK_AUDIENCE` in `bound_audiences`.

    Do not bind this role on the `tenant_id` claim. For a person in two
    tenants, `tenant_id` holds the first tenant, not the current one.

3. **Tell MEHO which role belongs to which tenant.** Add
   `<tenant-uuid>=<vault-role>` to `VAULT_OIDC_ROLE_BY_TENANT`, through
   `extraEnv` in your Helm values. Separate the entries with commas.
   Then run `helm upgrade`.

    ```yaml
    extraEnv:
      - name: VAULT_OIDC_ROLE_BY_TENANT
        value: "<first-tenant-uuid>=meho-tenant-main,<tenant-uuid>=meho-tenant-team-b"
    ```

    MEHO picks the role from the **current** tenant. A wrong entry stops
    MEHO from starting, so check the value before you roll it out. More
    about this setting is in
    [deploying.md](https://github.com/evoila/meho/blob/main/docs/deploying.md#version-specific-upgrade-notes)
    (row v0.35.13).

4. **Take the person out of the Vault groups of Step 6.** The roles
   above log in with `user_claim: sub` on the same auth mount as the
   shared role. So every login of the person, with any role, lands on
   the same Vault identity. Vault adds the policies of that identity's
   groups to each login. A person who is still in a group
   `meho-tenant-<first-tenant-uuid>-<role>` can then read the first
   tenant's secrets through the `team-b` role too, also after you take
   them out of the first tenant's Keycloak group. So give every tenant
   the person works in its own role (item 3), and take the person out
   of every group of Step 6. Read the group, then write it again with
   the other members only. The member list replaces the old one.

    ```bash
    meho operation call vault-1.x vault.identity.group.read --target vault-main \
      --params '{"name": "meho-tenant-<first-tenant-uuid>-read_only"}'
    meho operation call vault-1.x vault.identity.group.write --target vault-main \
      --params '{"name": "meho-tenant-<first-tenant-uuid>-read_only", "policies": ["meho-tenant-read-only"], "member_entity_ids": ["<entity-id of each other member>"]}'
    ```

    Each write waits for the second person. The `tenant_id` metadata
    on the identity may stay: without the groups, no policy uses it.

5. **Check that each role stops at its own tenant.** Get the person's
   Keycloak token as in the
   [connector Vault policy runbook](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-vault-policy.md),
   section 4. Log in to Vault with it against each role, and ask Vault
   what the token may do. `deny` means no access.

    ```bash
    TOKEN_B=$(vault write -field=token auth/jwt/login role=meho-tenant-team-b jwt="$PERSON_JWT")
    vault token capabilities "$TOKEN_B" "secret/data/tenants/<tenant-uuid>/<target>"             # expect: read
    vault token capabilities "$TOKEN_B" "secret/data/tenants/<first-tenant-uuid>/<target>"       # expect: deny
    vault token capabilities "$TOKEN_B" "secret/data/tenants/<tenant-uuid>/privileged/<target>"  # expect: deny
    ```

    Then do the same with `meho-tenant-main`, with the two tenants
    swapped. A `read` on the other tenant's path means the person is
    still in a Vault group of that tenant (item 4), or the policy has
    the wrong UUID (item 1).

Agent, service and runner logins have no `meho_tenants` list, so Vault
refuses them for a role that is bound only on `meho_tenants`. Plan their
Vault access before you add the tenant to `VAULT_OIDC_ROLE_BY_TENANT`.

## Step 3: Put a person in two tenants

Put the person in **one role group per tenant**. For example, they read
in your first tenant and operate in `team-b`:

```bash
meho keycloak group member add --target keycloak-main \
  --username alice@example.com --group-name MEHO-Readers
meho keycloak group member add --target keycloak-main \
  --username alice@example.com --group-name MEHO-team-b-Operators
```

Each command waits for approval. The second person approves it:

```bash
meho approvals list
meho approvals approve <approval-id>
```

The rules:

- One role group per tenant. Two groups of the **same** tenant: MEHO
  refuses the login with `401 multiple_role_groups` and names the groups.
- Groups of two **different** tenants are fine.
- A person may also be only in the groups of an extra tenant, for
  example only in `MEHO-team-b-Admins`. MEHO reads their tenant from the
  list.

The person logs in again (or waits for the next token refresh). Then
they check their tenants:

```bash
meho tenants use
```

The command lists each tenant with the role in it, and marks the
current one.

## Step 4: Switch tenants

The person picks the current tenant. No new login is needed: one token
holds all of their tenants.

**Web interface.** After the login, a person with several tenants sees
**Choose a tenant** first. A person with one tenant goes straight on. For
a person with several tenants, and for a platform admin, the tenant box
in the sidebar shows **Switch** and opens the same page (`/ui/tenant`). The account page lists the
person's tenants with the role in each. A switch is stored in the audit
log ("tenant switched from A to B") and opens the dashboard. A page that
was open in another browser tab before the switch refuses to send:
"The tenant changed. Reload the page."

**Command line.**

```bash
meho tenants use team-b      # a tenant name or ID
meho status                  # shows the current tenant
```

The choice is stored in `config.json`, next to the backplane address.
Every command then sends it. The variable `MEHO_TENANT` wins over the
stored choice for one shell:

```bash
MEHO_TENANT=team-b meho targets list
```

**MCP (Claude Code, Claude Desktop).** One MCP session works in one
tenant from start to end. Set the tenant before the session starts:

- Claude Code plugin: `MEHO_TENANT="team-b"` in your shell profile or in
  the plugin config file
  ([Claude Code](../clients/claude-code.md#work-in-one-tenant)).
- Claude Desktop bundle: the **Tenant** field in the extension settings
  ([Claude Desktop](../clients/claude-desktop.md)).

To switch, change the setting and start a new session. The first session
in a tenant opens a browser login once more. Without a tenant, a session
of a person in several tenants may only call `meho_status`, which lists
their tenants. No MCP tool switches the tenant inside a session. More:
[Which tenant a session works in](../clients/mcp-surface-and-scopes.md#which-tenant-a-session-works-in).

## The platform admin role

The group `MEHO-Platform-Admins` gives rights across all tenants. It is
not a role group: it holds no tenant and no role. So a platform admin is
also in at least one role group.

A platform admin **may**:

- work in any tenant that exists in MEHO, also without a group there.
  MEHO knows a tenant after the first request of a person in it. In the
  web interface, the tenant picker lists every tenant. On the command
  line, use `MEHO_TENANT=<tenant-uuid>`: `meho tenants use` accepts only
  the tenants of the person's own groups.
- act there as `tenant_admin`. When the person has a role group in that
  tenant, the role of that group applies instead.
- keep the cross-tenant actions of before, for example `--tenant` on the
  commands that have it.

A platform admin **may not**:

- approve their own changes. Only the `SA-Operators` group of the
  current tenant allows that.
- get the `approver` flag from the platform admin group. It comes only
  from an `Approvers` group of the current tenant.
- pick a tenant that does not exist. MEHO answers
  `403 tenant_not_found` and never makes a tenant from a typo.
- move a machine login. Agent, service and runner logins always stay in
  their own tenant.

Every audit row of a request where the role came from the platform admin
group carries `tenant_role_source: platform_admin`.

## When a change takes effect

Keycloak builds the tenant list again at each token refresh. So:

- **A new group** shows at the next token refresh, at the latest at the
  next login.
- **A removed group** ends at the next token refresh, at the latest at
  the next login. Until then the person still works in that tenant.

What the person sees after a removal:

- Web interface: "You are no longer in the tenant you worked in." MEHO
  moves to the one tenant left, or shows the tenant picker.
- Command line: the stored tenant is refused with `403 tenant_not_member`.
  `meho status` explains it. Pick another tenant with `meho tenants use`.
- MCP: a new session in that tenant is refused with `tenant_not_member`.

## If something does not work

| What you see | Cause | Fix |
|---|---|---|
| `tenant_not_chosen` | The login is in several tenants and the request named none. | `meho tenants use <name>`, or set the tenant in the MCP settings. |
| `tenant_not_member` | The tenant you chose is not in your list, or you were taken out of it. | `meho tenants use` lists your tenants. Ask your admin for a role group in the tenant. |
| `multiple_role_groups … of the same tenant …` | Two role groups of one tenant. | Keep one group per tenant, then log in again. |
| `meho tenants use` lists no tenants, or only one | The realm has no `meho-tenants` mapper, or you did not log in again after the group change. | Step 1 (the mapper), then log in again. |
| Vault refuses the login in the new tenant | The Vault role is missing, its `bound_claims` do not match, or `VAULT_OIDC_ROLE_BY_TENANT` has no entry for the tenant. | Step 2. |
| A person reads the first tenant's secrets through the second tenant's Vault role | The person is still in a Vault group of the first tenant. Vault adds the group's rights to every login of the person. | Step 2, item 4. Then check with item 5. |

More answers are in
[Troubleshooting auth walls](../clients/troubleshooting.md#calls-answer-tenant_not_chosen-or-tenant_not_member).
