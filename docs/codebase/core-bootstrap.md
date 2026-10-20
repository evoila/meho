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
- When `coreKeycloak.ci.enabled` is on, it also makes a login for automated
  install proofs.

What is true after the Job ran:

- The first admin can log in with `meho login https://<host>` or in the
  browser. Keycloak asks for a new password first. The token then carries
  the first tenant's id, the role `tenant_admin` and `platform_admin`.
- The second person is in `MEHO-SA-Operators`, the group that may approve
  changes that wait for a second person.
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

**One role group per person.** A person is in exactly one of the five role
groups (`docs/decisions/login-role-groups.md`). `MEHO-Platform-Admins` is
not a role group: it carries only `platform_admin: true` and no tenant or
role, so it does not change which group gives the tenant. The realm file
puts the `platform-admin` User Attribute mapper on the three clients people
log in with (`meho-cli`, `meho-mcp`, `meho-web`), the same way as the
`approver` mapper. The backplane reads the boolean claim `platform_admin`.

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
    the Keycloak Service URL, the realm file path, the Secret names and the
    user names. Never a value.
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
3. **Client scopes, groups, clients** from the realm file: each is created
   when missing (by name). An existing client gets the mappers and the
   scope assignments it lacks, and its secret is set when the file names
   one. Nothing is deleted, and an existing group, mapper or scope is not
   changed.
4. **CI login**, when on.
5. **First login.** Read `<release>-core-first-login`; make it when missing
   (create only; "already exists" means another run won). Make each person
   that does not exist and put them in their groups.
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
- Rotation of a client secret: write the new value into the
  `<release>-core-…` Secret and run `helm upgrade`; the Job sets it on the
  client. Restart the backplane pods that read it.
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
