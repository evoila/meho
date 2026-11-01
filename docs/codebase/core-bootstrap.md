# Core bootstrap Job — the realm stage finishes the built-in Keycloak (#4043, #4592)

## Overview

When the built-in Keycloak is on (`coreKeycloak.enabled`), the Helm chart
runs a `bootstrap` Job after every install and every upgrade. Its one stage
today, the realm stage, finishes the Keycloak realm `meho`:

- It sets the secrets of the confidential clients `meho-admin`, `meho-web`
  and `meho-check-runner` from the `<release>-core-…` Secrets the
  [secrets Job](core-secrets.md) made. Keycloak and the backplane then always
  use the same value.
- It adds what a newer realm file brings: client scopes, groups, clients,
  the mappers on a client and the scopes assigned to a client. Keycloak
  reads the realm file only at its first start, so this is the only way an
  upgrade reaches a running realm.
- It makes two people with temporary passwords: the first admin and a
  second person who can approve. The passwords go into the Secret
  `<release>-core-first-login`.
- On the first run it logs in with Keycloak's temporary first-start account,
  checks that `meho-admin` can log in, and then removes the first-start
  account. Every later run logs in as `meho-admin`.
- It makes the five role groups of every extra tenant in
  `coreKeycloak.tenants` (see [Several tenants](#several-tenants)).
- When `coreKeycloak.ci.enabled` is on, it also makes a login for automated
  install proofs.
- When `coreKeycloak.companyLogin.enabled` is on, it puts the company login
  system in front of Keycloak (see "The company login" below).

What is true after the Job ran:

- The first admin can log in with `meho login https://<host>` or in the
  browser. Keycloak asks for a new password first. The token then carries
  the first tenant's id, the role `tenant_admin` and `platform_admin`.
- The second person is in `MEHO-SA-Operators`, the group that may approve
  changes that wait for a second person. The first admin cannot approve
  their own request.
- A second run with the same values changes nothing.
- The Job's output names objects and says `created`, `kept` or `removed`. It
  never holds a password or a secret.

## The people

| Person | Values key (user name) | Groups | What the login carries |
|---|---|---|---|
| First admin | `coreKeycloak.firstLogin.adminUsername` (default `admin`) | `MEHO-Admins`, `MEHO-Platform-Admins` | `tenant_role: tenant_admin`, `platform_admin: true` |
| Second person | `coreKeycloak.firstLogin.approverUsername` (default `approver`) | `MEHO-SA-Operators` | `tenant_role: operator` |

Both get a 32-character password from the one alphabet `[A-Za-z0-9]`, stored
as a temporary credential with the required action `UPDATE_PASSWORD`, so
Keycloak forces a change at the first login. The realm's user profile also
wants a first name, a last name and an email, so the first browser login
asks for those too. Until both are done, a login without a browser (for
example the password grant) is refused with "Account is not fully set up".

The Secret `<release>-core-first-login` holds `admin_username`,
`admin_password`, `approver_username` and `approver_password`. The install
notes print the `kubectl get secret … -o jsonpath` command; they never print
a value. The Secret is the source on later runs: if a person is missing but
the Secret exists, the Job makes the person again with the stored password.
Changing a user name in values after the first install has no effect.

The two names must be two different Keycloak users. Keycloak ignores case
and surrounding spaces in a user name, so `Admin` and `admin` are the same
person. The Job refuses such a pair before it makes the Secret, so a wrong
pair never leaves one person with a stored password that does not work.

**One role group per person.** A person is in exactly one of the five role
groups (`docs/decisions/login-role-groups.md`). `MEHO-Platform-Admins` is
not a role group: it carries only `platform_admin: true` and no tenant or
role, so it does not change which group gives the tenant. The realm file
puts the `platform-admin` User Attribute mapper on the three clients people
log in with (`meho-cli`, `meho-mcp`, `meho-web`), the same way as the
`approver` mapper. The backplane reads the boolean claim `platform_admin`.

## Several tenants

Design: `docs/decisions/2026-10-28-several-tenants-per-person.md`.

- The five role groups carry `meho_tenant` =
  `<tenant-uuid>:<tenant>:<Role>`, from the placeholders
  `${MEHO_REALM_TENANT_ID}` and `${MEHO_REALM_TENANT_NAME}`. The name comes
  from `coreKeycloak.tenantName` (default `main`).
- The `meho-tenants` mapper on `meho-cli`, `meho-mcp` and `meho-web` lists
  the `meho_tenant` values of all the person's groups in the claim
  `meho_tenants` (Multivalued and Aggregate on).
- `coreKeycloak.tenants` is a list of `{name, id}`. For each entry the Job
  makes `MEHO-<name>-Readers`, `-Operators`, `-SA-Operators`, `-Admins` and
  `-Approvers`. They carry only `meho_tenant`, never `tenant_id` or
  `tenant_role`. When such a group exists already, the Job removes
  `tenant_id`, `tenant_role` and `approver` from it
  (`OLD_ACCESS_ATTRIBUTES`) and keeps every other attribute. The plan
  field is `Plan.tenants` (`TenantPlan`). A name
  must be lowercase letters, digits and dashes, and not `sa`, `platform`
  or `ci` (those would clash with shipped groups). Names and ids must
  differ from each other and from the first tenant; the Job stops before
  any call otherwise.
- No person is put into these groups by the Job. Doing it through MEHO's
  Keycloak operations still needs a second person's approval.
- A site with one tenant: the upgrade adds `meho_tenant` to the five groups
  and the mapper to the three clients. The token then lists one tenant, and
  every other claim is the same.

## The CI login (off by default)

`coreKeycloak.ci.enabled: true` with `coreKeycloak.ci.tenantId: <uuid>` makes:

- the confidential service client `meho-ci`, a `tenant_admin` of the first
  tenant through fixed claims (`client_credentials`, no browser). Its secret
  is made by the Job and stored in `<release>-core-ci-login` (keys
  `client_id`, `client_secret`);
- the group `MEHO-CI-Admins` with `tenant_id` = `ci.tenantId` and
  `tenant_role: tenant_admin`, a second tenant for cross-tenant proofs.

Turn it on only on a test install: it is a login with fixed admin claims.

## The company login (off by default)

A site connects its company login system (an OpenID Connect server such as
authentik, Entra ID, Okta or ADFS) with the values in
`coreKeycloak.companyLogin`. People then log in on the company login page,
with its own second login step, and come back with a MEHO token from
Keycloak. MEHO still trusts only Keycloak, so the backplane needs no change.
Service accounts stay in Keycloak.

Keycloak's identity broker does the work. The company login sends people
back to `https://<host>/auth/realms/meho/broker/<alias>/endpoint`; the
install notes print this address. On every install and upgrade the realm
stage (`core/company_login.py`, `ensure_company_login`) does this, in order:

1. **Groups.** Every MEHO group named in `groupMap` must exist; otherwise
   the Job stops before it changes anything.
2. **Provider.** Keycloak reads the discovery document itself
   (`POST identity-provider/import-config` with `fromUrl`), so it uses its
   own trusted CAs, including `caBundle`. The stage refuses a company login
   that publishes no signing keys (`jwks_uri`). It then creates or updates
   one OpenID Connect provider: PKCE S256, signature check with the
   published keys, sync mode FORCE, "trust email" off, the first-login flow
   `first broker login` (Keycloak's default, so a company user is never
   linked to a local account without proof), the scopes from `scopes`, and
   the client secret in `client_secret_post` form. Keycloak never returns
   the secret, so the stage sends it on every run; a new value in the
   Secret reaches Keycloak at the next `helm upgrade`. If the discovery
   document cannot be read but the provider exists with the same discovery
   address, the saved addresses stay, so an upgrade that turns
   `autoRedirect` off works while the company login is down. "Cannot be
   read" covers an error reply from Keycloak and a request to Keycloak that
   times out while Keycloak waits for the company login.
3. **Group rules.** One "Advanced Claim to Group" mapper
   (`oidc-advanced-group-idp-mapper`, sync mode FORCE) per `groupMap` line,
   named `meho-group-<company group>`. It puts the person into the MEHO
   group when the `groupsClaim` field holds the company group. With FORCE,
   Keycloak checks again at every company login and takes the person out of
   a group that no longer matches. A changed line updates its mapper; a
   mapper with the `meho-group-` prefix that is no longer listed is
   removed. Other mappers stay. FORCE fixes a membership only through a
   mapper that still exists, so when a line goes away or points at another
   MEHO group, the Job takes the people who came in through the company
   login out of the old group. Local accounts and other memberships stay. A
   person whom another line still maps to that group gets it back at the
   next login. Before it changes any mapper, the Job writes the old groups
   onto the provider (config key `meho.groupCleanup`) and removes that
   note when the people are out. So a run that stops in the middle of the
   cleanup, for example on a timeout, finishes it at the next run and
   nobody keeps an old group.
4. **Auto-redirect.** With `autoRedirect: true`, the "Identity Provider
   Redirector" step of the realm's browser login flow gets the company
   login as its default (config alias `meho-company-login-redirect`), so
   Keycloak never shows its own login page. With `false`, that default is
   removed and the Keycloak login form shows again: the way in with the
   local accounts when the company login is down.

What is not done:

- Turning `companyLogin.enabled` off does not remove the provider. The
  stage then does not touch the company login at all.
- A removal from a company group takes effect at the person's next company
  login. A running login, and an offline login, keep working until they
  end or an admin signs the person out in Keycloak.
- A person in two role groups gets an unpredictable role; keep one company
  group per person.

The client secret comes into the Job as `MEHO_COMPANY_LOGIN_CLIENT_SECRET`
by `secretKeyRef` from `companyLogin.clientSecret`. It is never in the plan,
the chart or the output. `companyLogin.caBundle` mounts a ConfigMap into the
Keycloak pod and sets `KC_TRUSTSTORE_PATHS`; Keycloak keeps trusting the
usual public CAs too. The Keycloak NetworkPolicy limits only incoming
traffic, so Keycloak can call the company login.

**Rights.** Setting up the provider needs `manage-identity-providers`; the
redirect step needs `manage-realm`. With the company login on, the realm
ConfigMap of the Keycloak subchart adds both to `meho-admin`'s service
account; with it off, the rendered realm is the realm file and nothing
else. Keycloak applies the realm at the first start only, and it does not
let an account give itself a role it does not have. So an install whose
first start had the company login off lacks the two roles; when the company
login is turned on later, its Job stops with a sentence that names them.
Add them once to the service account of `meho-admin` (realm-management
client roles), with a Keycloak admin login or a temporary one from
Keycloak's `bootstrap-admin` recovery command, then run `helm upgrade`
again. With the two roles, the backplane, which uses `meho-admin`, can
also change login settings of realm `meho`; it could already change people
and groups with `manage-users`.

## Key types and files

- `backend/src/meho_backplane/core/bootstrap.py` — the entry point,
  `python -m meho_backplane.core.bootstrap <stage>`. Exit code `0` on
  success, `1` on failure with one sentence on stderr as
  `core_bootstrap_failed: <sentence>`, `2` for a wrong command line.
- `backend/src/meho_backplane/core/keycloak.py` — the realm stage:
  - `Plan` — read from `MEHO_CORE_KEYCLOAK_PLAN`, rendered by the chart:
    the Keycloak Service URL, the realm file path, the Secret names, the
    user names and the extra tenants (`TenantPlan`). Never a value.
  - `tenant_groups(tenant)` — the five groups of an extra tenant.
  - `load_realm_file(path, environ)` — reads the realm file and fills its
    `${...}` placeholders from the Job's environment (the same variables
    the Keycloak pod gets).
  - `finish_realm(plan, environ, admin, store)` — the steps below.
- `backend/src/meho_backplane/core/keycloak_api.py` — `HttpAdmin`, the
  Keycloak Admin REST calls over `httpx` against the Service
  `http://<release>-keycloak:8080/auth`; `Admin`, the narrow interface the
  stage uses (tests run it against a fake); `RealmStageError`.
  - `KubernetesSecretStore.read_data` (in `core/secrets.py`) — reads the
    values of a `<release>-core-*` Secret.
- `backend/src/meho_backplane/core/company_login.py` — the company login:
  `CompanyLoginPlan` (the `company_login` part of the plan, only rendered
  when on) and `ensure_company_login`.
- `deploy/charts/meho/templates/core-bootstrap-job.yaml` — the Job.
- `deploy/charts/meho/templates/_core-bootstrap.tpl` — the plan.
- `deploy/charts/meho/charts/keycloak/files/meho-realm.json` — the realm
  file; mounted into the Job from the subchart's ConfigMap.

## Control flow

Helm order (the rest of the core's hooks are in
[core-secrets.md](core-secrets.md)):

| Weight | Object | Phases |
|---|---|---|
| -40 | ServiceAccount, Role, RoleBinding `<release>-core-bootstrap` | all four |
| 0 | the Keycloak StatefulSet imports the realm file at its first start | install, upgrade |
| 10 | Job `<release>-core-bootstrap` (`keycloak` stage) | post-install, post-upgrade |

Steps of the realm stage, in order:

1. **Wait** until Keycloak serves the realm's discovery document (up to 5
   minutes; the Job's `backoffLimit` retries after that).
2. **Log in.** First with the first-start account (`client_credentials` in
   realm `master`, from `<release>-core-keycloak-bootstrap`), else as
   `meho-admin` (`client_credentials` in realm `meho`, secret from
   `<release>-core-keycloak-admin`). Neither works: the Job stops.
3. **Client scopes, groups, clients** from the realm file, then the groups
   of the extra tenants: each is created when missing (by name). An
   existing group gets the attributes the file (or the tenant) names, when
   a value differs; its other attributes stay (output: `kept (attributes
   set)`). An existing client gets the mappers and the scope assignments
   it lacks, and its secret is set when the file names one. Nothing is
   deleted, and an existing mapper or scope is not changed.
   Before the clients come the **login flows** of the realm file
   (`core/keycloak_flows.py`, #5515). Today that is one flow, `meho
   approval sign-in`: the password form, then the one-time code when the
   person has set one up. A missing flow is made with its steps; an
   existing flow is kept as it is. A client that names a flow in
   `authenticationFlowBindingOverrides` (only `meho-cli-approve`) is bound
   to it (output: `kept (login flow set)`). Reading and making login flows
   needs `manage-realm`. Without it the step prints `not checked (no right
   to read login flows)` and the Job goes on; the client is then made
   without its own flow. A fresh install gets the flow and the binding from
   the realm import, so this matters only for a realm made before #5515.
4. **CI login**, when on.
   Then the **company login**, when on (see "The company login").
5. **First login.** Read `<release>-core-first-login`; make it when missing
   (create only; "already exists" means another run won). Make each person
   that does not exist. The person and their group memberships are one
   request, so Keycloak makes both or neither; a run that fails here
   leaves no person without groups, and the next run makes the person
   again.
6. **Swap.** Only when step 2 used the first-start account: check that
   `meho-admin` logs in with its secret, then delete the first-start
   account's client in realm `master`. If `meho-admin` cannot log in, the
   first-start account stays and the Job stops.

Rights: `meho-admin` has `manage-clients` and `manage-users` on realm
`meho` (from the realm file), which covers steps 3 to 5 except the login
flows; with the company login on, the chart adds `manage-identity-providers`
and `manage-realm` (see "Rights" under the company login), and then the
Job can make the login flows too. Only the first-start account can do
step 6, and only it is needed there.

### Network rules

The Job pod carries the labels `app.kubernetes.io/instance: <release>` and
`app.kubernetes.io/component: core-bootstrap`, not the backplane's selector
labels, because the backplane NetworkPolicy allows no egress to the
Kubernetes API. The Keycloak subchart's own NetworkPolicy
(`coreKeycloak.networkPolicy.enabled`) admits port 8080 from those labels,
so the Job reaches Keycloak with the policy on.

### GitOps and other deploy paths

Same rules as the secrets Job: no `argocd.argoproj.io` annotation; the Job
is a PostSync hook under Argo CD and runs on every sync; it changes nothing
when nothing is missing. The Secrets it makes carry only the label
`meho.evoila.com/managed-by: core-bootstrap`.

## Dependencies

- `httpx` for the Keycloak Admin REST API, `kubernetes_asyncio` for the two
  Secrets, `pydantic` for the plan. All are in the backplane image.
- The Keycloak subchart (#4040): the realm file, the ConfigMap and the
  Service name `<release>-keycloak`.
- The secrets Job (#4041): the client secrets and the first-start account.

## Known issues and limits

- The stage adds, it never removes or rewrites. A changed group attribute,
  a changed mapper setting or a renamed client in a newer realm file does
  not reach a running realm; an operator changes it in Keycloak.
- The `users` entry of the realm file (the roles of `meho-admin`'s service
  account) is applied at the first start only.
- A person who exists is never touched: not their groups, not their
  password. To start over for one person, delete the person in Keycloak;
  the Job makes them again from the Secret.
- Rotation of the secret of `meho-web` or `meho-check-runner`: write the
  new value into its `<release>-core-…` Secret and run `helm upgrade`; the
  Job sets it on the client. Then restart the pods that read it.
- Rotation of the secret of `meho-admin` is manual. The Job logs in as
  `meho-admin` with the value in `<release>-core-keycloak-admin`, so a new
  value in the Secret alone locks the Job out: it can no longer log in to
  set the new value on the client, and every later run fails. Instead:
  1. In Keycloak, set the new secret on the client `meho-admin` (realm
     `meho`, Clients, Credentials). This needs a Keycloak admin login; the
     first-start account is gone after the first run, so use a Keycloak
     admin you made, or a temporary one from Keycloak's
     `bootstrap-admin` recovery command (see the link below).
  2. Write the same value into the Secret `<release>-core-keycloak-admin`
     (key `client_secret`).
  3. Restart the pods that read it, or run `helm upgrade`. The Job then
     logs in with the new value and reports `client meho-admin: kept`.
  Between steps 1 and 2 the backplane cannot use `meho-admin`.
- A fresh install on kind that proves `meho login` end to end is the CI
  job of #4051, which does not exist yet. The realm stage is tested
  against a real Keycloak in
  `backend/tests/integration/test_core_bootstrap_keycloak.py`. The company
  login is tested there too, in
  `backend/tests/integration/test_core_company_login_keycloak.py`: a second
  realm in the same Keycloak plays the company login, and a browser login
  through it gives the right token fields.

## References

- Core design, section 3 "Secrets at install" (internal design note):
  sub-decisions 3, 8 and 9.
- `docs/decisions/login-role-groups.md` — the five role groups.
- [core-secrets.md](core-secrets.md) — the Secrets, the hook account and
  its rights.
- Keycloak bootstrap admin accounts:
  https://www.keycloak.org/server/bootstrap-admin-recovery
- Keycloak Admin REST API: https://www.keycloak.org/docs-api/latest/rest-api/index.html
- Keycloak identity brokering, sync mode, Identity Provider Redirector:
  https://www.keycloak.org/docs/latest/server_admin/index.html
- Keycloak trusted CAs (`truststore-paths`):
  https://www.keycloak.org/server/keycloak-truststore
