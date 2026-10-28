# Core bootstrap Job — the realm stage finishes the built-in Keycloak (#4043)

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
  `tenant_role`. The plan field is `Plan.tenants` (`TenantPlan`). A name
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
4. **CI login**, when on.
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
`meho` (from the realm file), which covers steps 3 to 5. Only the
first-start account can do step 6, and only it is needed there.

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
  `backend/tests/integration/test_core_bootstrap_keycloak.py`.

## References

- Core design, section 3 "Secrets at install" (internal design note):
  sub-decisions 3, 8 and 9.
- `docs/decisions/login-role-groups.md` — the five role groups.
- [core-secrets.md](core-secrets.md) — the Secrets, the hook account and
  its rights.
- Keycloak bootstrap admin accounts:
  https://www.keycloak.org/server/bootstrap-admin-recovery
- Keycloak Admin REST API: https://www.keycloak.org/docs-api/latest/rest-api/index.html
