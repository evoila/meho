# One person in several tenants: one login lists the tenants, MEHO uses one at a time (decision)

**Status:** decided — product owner, 2026-10-28
**Issue:** [#3978](https://github.com/evoila/meho/issues/3978) — switch tenants without logging out, and a role for people who work across all tenants
**Builds on:** [login-role-groups.md](login-role-groups.md) (the five groups, [#3959](https://github.com/evoila/meho/issues/3959)), the approval rules of [#3325](https://github.com/evoila/meho/issues/3325)

## In short

- A person can be in role groups of several tenants. A tenant is a separate space in MEHO, with its own systems and passwords.
- The login token lists all of the person's tenants, with the role in each.
- MEHO always works in **one current tenant**. The person picks it in the web interface, on the command line, or in the MCP settings. No new login is needed.
- Every check that exists today keeps reading the current tenant only. A site with one tenant sees no change.

## 1. How one login carries several tenants

Code paths without a folder name are under `backend/src/meho_backplane/`.

- Every role group gets one new group attribute, `meho_tenant`. Its value is `<tenant-uuid>:<tenant>:<Role>`, for example `3f0c…:lab-a:Operators`. `<Role>` is one of `Readers`, `Operators`, `SA-Operators`, `Admins`, `Approvers`.
- One new Keycloak mapper copies the values of all the person's groups into the token claim `meho_tenants` (a list of strings).
- MEHO turns each entry into a tenant, a role and the flags, with the table in [login-role-groups.md](login-role-groups.md). The table lives in one place in MEHO's code.
- The fields `Operator.tenant_id`, `tenant_role` and `approver` (`backend/src/meho_backplane/auth/operator.py:218-233`) and the self-approval flag of [#3327](https://github.com/evoila/meho/issues/3327) get the values of the **current** tenant's entry. Values of another tenant never apply.
- So the role checks (`auth/rbac.py:79`, `:183`), the cross-tenant check (`auth/rbac.py:234`), the Vault role choice (`auth/vault.py:264-296`), the Vault path guard (`connectors/vault/tenant_scope.py:150`) and the audit tenant (`middleware.py:443`) all keep working unchanged.
- **No `meho_tenants` claim in the token: today's path, exactly** (`auth/jwt.py:1383-1470`).
- **One role group per tenant.** Two entries for the same tenant: MEHO refuses the login (`401 multiple_role_groups`, the same answer as today, `auth/jwt.py:846`). Two groups of two different tenants are fine.

Other ways, and why not:

- Read the tenant from the group names in the `groups` claim: the tenant UUID is not in the name, so MEHO would need a second list that maps names to UUIDs.
- One token per tenant (a Keycloak client scope per tenant): every switch needs a new Keycloak login, and every tenant adds scopes and mappers on three clients.
- A JSON value in the attribute: with one typo in one group, Keycloak 26.0 issues no token to any member of that group ("cannot map type for token claim"), and Keycloak 26.8 drops the entry without a word. A plain string lets MEHO refuse only the bad entry, with a clear message.

## 2. How switching works

**On the wire.** A request names its tenant in the header `Meho-Tenant` (UUID or tenant name). This is the same style as the `Meho-Work-Ref` header (`cli/internal/api/workref.go:14`). MEHO checks the header against the token's list on every request:

| The token lists | No header | Header names a tenant in the list | Header names another tenant |
| --- | --- | --- | --- |
| no list (today) | the token's tenant | allowed if it is the token's tenant | `403 tenant_not_member` (a platform admin: see §3) |
| one tenant | that tenant | that tenant | `403 tenant_not_member` (a platform admin: see §3) |
| several tenants | `400 tenant_not_chosen` (names the tenants and the command to pick one) | that tenant | `403 tenant_not_member` (a platform admin: see §3) |

`meho status` and the MCP tool `meho_status` answer without a chosen tenant. They list the person's tenants and roles.

**Web interface.**

- The web session row already stores a tenant (`db/models.py:3310`, set at login in `ui/auth/routes.py:527-530`). It becomes the current tenant.
- After a login with several tenants, MEHO shows a tenant picker first. The sidebar tenant box (`ui/templates/base.html:284-297`) becomes a switch. The account page lists the person's tenants (the slot at `ui/templates/account/index.html:86`; today `ui/routes/account.py:45` leaves switching out).
- A switch checks the freshly checked token, updates the session row and opens the dashboard.
- Every page remembers the tenant it was made for. A form sent from an old browser tab after a switch is refused: "The tenant changed. Reload the page."
- The check that the token and the session agree (`ui/routes/agents/operator.py:89`) compares the session tenant with the token's list.

**Command line.**

- `meho tenants use <name>` stores the choice next to the backplane address in `config.json` (`cli/internal/auth/config.go:26`). `meho tenants use` without a name lists the person's tenants and marks the current one. It goes into the existing `meho tenants` group (`cli/internal/cmd/tenants/tenants.go:55`).
- The CLI sends the header on every call (the client sets headers at `cli/internal/api/client.go:154`). The variable `MEHO_TENANT` overrides the stored choice for one shell.
- The stored login does not change. One token holds all tenants, so a switch needs no new login.
- No new global `--tenant` flag: several commands already use `--tenant` for "a platform admin acts on another tenant".

**MCP (Claude and other agents).**

- The two connection helpers (`clients/claude-code-plugin/bin/meho-mcp-remote`, `clients/claude-desktop-mcpb/`) read `MEHO_TENANT` (the `.mcpb` gets a `tenant` setting) and send the header.
- One MCP session works in one tenant, fixed at the start. To switch, change the setting and start a new session.
- No new MCP tool. The working surface stays at its 26 tools. Another way, and why not: a switch tool would let an agent move between tenants inside one session.

**Records.** Every audit row already stores the tenant (`db/models.py:517`). It is the tenant bound per request (`middleware.py:443`), so it is the current tenant. The tenant must also **show** in: the console audit list and its detail drawer, the approvals page, `meho audit` output, `meho status` and `meho_status`. The page header already shows it.

## 3. The platform admin role

- Group **`MEHO-Platform-Admins`** (name confirmed). It already ships in the built-in Keycloak (`deploy/charts/meho/charts/keycloak/files/meho-realm.json:104`), and the first admin is in it (`backend/src/meho_backplane/core/keycloak.py:94`). The bring-your-own setup (`cli/internal/cmd/admin/keycloak/bootstrap.go:234-240`) does not create it yet.
- The group sets `platform_admin: true`. MEHO reads it into the existing flag (`auth/jwt.py:1245`, `auth/operator.py:232`). It is not a role group: it holds no tenant and no role.
- **A platform admin may:**
  - pick any tenant that exists in MEHO as the current tenant, also without a group in that tenant;
  - act there as a tenant admin, unless they are in a role group of that tenant (then that group's role applies);
  - keep the cross-tenant actions of today: `--tenant` reads and writes (`auth/rbac.py:234`), built-in connectors, shared doc collections.
- **A platform admin may not:**
  - approve their own change. Self-approval comes only from an SA-Operators group of the current tenant;
  - approve in a way that #3325 does not allow. Approval decisions stay per person, one tenant at a time;
  - get the `approver` flag from the platform group;
  - pick a tenant that does not exist. A typo never creates a new tenant;
  - be a machine. Agent, service and runner logins stay in their one tenant, and the header cannot move them.
- The audit row notes when the role came from the platform admin group and not from a role group.
- Another way, and why not: read-only in tenants without a group. That is safer, but #3978 asks for rights in all tenants, and the platform admin would need groups in every tenant anyway.

## 4. Keycloak side

- **Groups per extra tenant:** `MEHO-<tenant>-Readers`, `MEHO-<tenant>-Operators`, `MEHO-<tenant>-SA-Operators`, `MEHO-<tenant>-Admins`, `MEHO-<tenant>-Approvers`. `<tenant>` is a short name: lowercase letters, digits and dashes. #3978's text wrote `<tenant>-<Role>`. We keep the `MEHO-` prefix of the shipped groups (as `login-role-groups.md:105-112` planned).
- Each of these groups carries only `meho_tenant`. It carries no `tenant_id` or `tenant_role`, so an older MEHO refuses a person who is only in tenant groups.
- The five groups of the first tenant keep their attributes and also get `meho_tenant`.
- **The mapper**, on `meho-cli`, `meho-mcp` and `meho-web`: stock User Attribute mapper, attribute `meho_tenant`, claim `meho_tenants`, type String, **Multivalued on, Aggregate attribute values on**, access token and userinfo on. With these settings Keycloak collects the value of every group of the person ([KeycloakModelUtils.java L776-L797, 26.8.1](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi-private/src/main/java/org/keycloak/models/utils/KeycloakModelUtils.java#L776-L797); [OIDCAttributeMapperHelper.java L162-L183](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/mappers/OIDCAttributeMapperHelper.java#L162-L183)). No scripts.
- **The standard setups create all of this, safe to run twice:**
  - bring-your-own: `meho admin keycloak bootstrap-clients` names the first tenant and adds a tenant's five groups on request. It also creates `MEHO-Platform-Admins` and its mapper;
  - built-in Keycloak: the realm stage (`core/keycloak.py`) reads a list of extra tenants from the Helm values.
- **A site with one tenant:** running the setup again adds the attribute and the mapper. The token then lists one tenant, and MEHO behaves exactly as before. A site that does not run it again has no list and stays on today's path.
- **Changing tenant groups** (create a group, put a person in it) stays at the dangerous risk level and needs a second person, until the #3325 rules land. [#3872](https://github.com/evoila/meho/issues/3872) tracks the tenant setup runbook.
- **Same rule as today:** nobody sets `meho_tenant` on a person. With Aggregate on, a value on the person is added to the list (`KeycloakModelUtils.java` L783). People must not edit their own attributes (see `login-role-groups.md` rule 4).

## 5. What must be true before coding

- **Vault (decided 2026-10-28).** For the lab test, one Vault login role per tenant counts as "separate passwords". The full Vault separation (#4044, #4047) is **not** a dependency of #3978.
- **Vault sees one tenant per login.** MEHO picks the Vault login role from the current tenant (`VAULT_OIDC_ROLE_BY_TENANT`, `auth/vault.py:264-296`). That role's policy names its own tenant's path. Its `bound_claims` (with the glob type, so `*` matches any text) require `<tenant-uuid>:*` in `meho_tenants`. Vault matches any element of a list claim ([claims.go L114-L169](https://github.com/hashicorp/vault-plugin-auth-jwt/blob/v0.21.0/claims.go#L114-L169)).
- #4044 and #4047 copy one `tenant_id` from the token into the Vault login. That is right for a person in one tenant, which covers every site today. For a person in two tenants, the token's `tenant_id` is the first tenant's, not the current one. So a per-tenant Vault role must name its tenant itself and must not read it from that claim.
- **#3959:** the five groups and the one-group check are done (#4590, #4594). Nothing open blocks this work.
- **#3325:** #3327 (the SA-Operators flag) follows the token Task, because both change `auth/jwt.py` and `auth/operator.py`.
- **Can start now:** the token Task, the Keycloak setup Task, the Task that shows the tenant in the audit views, and the lab setup of two test tenants.
- **Then:** authorization (after the token Task); web interface, command line and MCP (after authorization); docs with the one-tenant guarantee test, and the lab proof, last.

## 6. Risks, and what to prove in the lab

Risks:

- **An old browser tab** sends a form to the new tenant after a switch. Answer: every page carries its tenant, and MEHO refuses a mismatch.
- **Anything kept per person only** (result links, MCP sessions, web sessions) could mix tenants. Answer: key such things on person and tenant. A test reads a result link from tenant A while working in tenant B, and is refused.
- **Removal is not instant.** Keycloak rebuilds the list at each token refresh (minutes), at the latest at the next login.
- **The platform admin is powerful.** Only people get it, and the audit row says when it was used.
- **`capabilities` stays per login, not per tenant.** The standard setup does not set it today. Per-tenant capabilities are out of scope.

To prove in our lab, with two test tenants, each with its own Vault login role:

- [ ] One test person, in `Readers` of tenant A and `Operators` of tenant B, logs in once. They switch tenants in the web interface and on the command line, without logging out. They see only the systems of the current tenant.
- [ ] Their role follows the tenant: reads work in A, a change is refused in A, and the same change runs (or waits for approval) in B.
- [ ] In B they read B's test secret and cannot read A's.
- [ ] Every action record shows the right tenant.
- [ ] Taken out of B's group, they lose B at the next token refresh, at the latest at the next login.
- [ ] A platform admin sees both tenants and can switch into each.
- [ ] A person in one tenant, and a realm without the new mapper, work exactly as before.

## Out of scope

- A command to create or list all tenants of a site. Moving systems or passwords between tenants.
- An MCP tool to switch tenants. Per-tenant `capabilities`.
