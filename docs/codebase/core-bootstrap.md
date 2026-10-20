# Core bootstrap Job — the realm stage finishes Keycloak after the install (#4043)

## Overview

When the built-in Keycloak is on (`coreKeycloak.enabled`), the Helm chart
runs a `bootstrap` Job after every install and every upgrade. It has one
stage, the realm stage. It does what the realm file cannot do, and what a
newer chart adds later. Keycloak imports the realm file only at its first
start (`--import-realm` skips a realm that exists).

What is true after the Job ran:

- Every role group in `coreKeycloak.groups` exists, with the attributes
  `tenant_id` (= `coreKeycloak.firstTenantId`), `tenant_role` and, for
  MEHO-Approvers, `approver`. A mapper copies them into each login's token.
- Every client and every client mapper of the realm file exists. The
  `meho-admin`, `meho-web` and `meho-check-runner` secrets in Keycloak equal
  their Kubernetes Secrets (made by the secrets Job, `core-secrets.md`).
- At the first install, two people exist: the first admin (default `admin`,
  in MEHO-Admins, with the realm role `platform_admin`) and the second person
  (default `approver`, in MEHO-SA-Operators). The second person can approve
  a change that waits for approval from day one. Their passwords are
  temporary, and Keycloak asks for a new one at the first login.
- Keycloak's temporary bootstrap account is gone.

A second run with the same values changes nothing. The Job's output names
objects and `created`, `kept`, `secret set` or `deleted`. It never holds a
value.

The first login: the install notes print the command that reads the first
admin's password from `<release>-core-first-login`, then
`meho login https://<host>`.

## Key types and files

| File | What it holds |
|---|---|
| `backend/src/meho_backplane/core/bootstrap.py` | The realm stage: `python -m meho_backplane.core.bootstrap keycloak`. |
| `backend/src/meho_backplane/auth/keycloak_admin.py` | `KeycloakAdminClient`: the `client_credentials` login and `request()`, one Admin REST call. |
| `backend/src/meho_backplane/core/secrets.py` | `KubernetesSecretStore.read_data` and `annotate`, for the first-login Secret. |
| `deploy/charts/meho/templates/core-bootstrap-job.yaml` | The hook Job. |
| `deploy/charts/meho/templates/_core-keycloak.tpl` | `meho.core.bootstrapPlan` (the Job's plan) and the check that each person's group is in the list. |
| `deploy/charts/meho/charts/keycloak/values.yaml` | `groups` and `firstLogin`, the defaults. |
| `deploy/charts/meho/charts/keycloak/files/meho-realm.json` | The realm file: clients, mappers, client scopes, the `platform_admin` role. No groups and no secret. |

The plan (`MEHO_CORE_BOOTSTRAP_PLAN`) holds the Keycloak Service URL, the
first tenant id, the groups and the two people. Secrets reach the Job only
through `secretKeyRef`: the bootstrap account (`KC_BOOTSTRAP_ADMIN_CLIENT_*`,
the same Secret the Keycloak pod reads) and the three client secrets
(`MEHO_REALM_*_CLIENT_SECRET`, also the realm file's placeholders). The Job
mounts the realm ConfigMap and fills its `${...}` placeholders from its own
environment, as Keycloak does at import.

## Control flow

1. **Wait** up to 10 minutes for the realm's discovery document at
   `http://<release>-keycloak:8080/auth/realms/meho`. A plain `helm install`
   without `--wait` starts the hook while Keycloak still starts.
2. **Log in.** Try the bootstrap account in the master realm. While it
   exists, check that `meho-admin` logs in with its Kubernetes Secret. If
   Keycloak refuses it, set the client's secret through the bootstrap account
   first. Every call below runs as `meho-admin` (`manage-clients`,
   `manage-users` on realm `meho`).
3. **Groups.** Create each missing top-level group of `coreKeycloak.groups`.
   A group that exists is never changed.
4. **Clients.** Create each missing client of the realm file, with its
   mappers and secret. On an existing client, add each missing mapper (by
   name), and set the secret only when Keycloak's differs from the file's.
5. **First login.** Read `<release>-core-first-login`.
   - Annotation `meho.evoila.com/first-login-users: made` → skip. A person
     deleted after the first install stays gone.
   - No Secret → generate two 32-character passwords (`[A-Za-z0-9]`) and
     create the Secret first, so no password exists only in Keycloak.
   - Then create each missing person: one group, the temporary password,
     `UPDATE_PASSWORD`; the first admin also gets `platform_admin`. If a
     step after the create fails, the half-made person is deleted before the
     Job stops, and the retry makes it again with the stored password.
   - Annotate the Secret.
6. **Delete the bootstrap account**, last. If any step fails, the account
   stays, and the next run (the Job's retry or the next upgrade) uses it.

### Helm lifecycle

| Weight | Object | Phase |
|---|---|---|
| -40 | hook ServiceAccount, Role, RoleBinding `<release>-core-bootstrap` (#4041) | pre- and post-install, pre- and post-upgrade |
| -30 | `secrets` Job | pre-install, pre-upgrade |
| 0 | Keycloak, the backplane and the rest | release |
| +10 | `bootstrap` Job `<release>-core-bootstrap` | post-install, post-upgrade |

The Job writes only `<release>-core-first-login`: `create` (any name) and
`get` and `patch` on that name, all from the hook Role of #4041. The Job pod
carries `app.kubernetes.io/component: core-bootstrap`; the Keycloak
subchart's NetworkPolicy admits that label on port 8080.

### platform_admin

The first admin gets the realm role `platform_admin`. The chart sets
`JWT_PLATFORM_ADMIN_ROLE_NAME=platform_admin` on the backplane when
`coreKeycloak.enabled`, so the backplane reads the flag from the token's
realm roles. A user attribute would not work: Keycloak 24 and newer drop
attributes the realm's user profile does not declare.

## Dependencies

- Keycloak 26 Admin REST API (users with `groups` and `credentials` on
  create; realm role mappings; client secrets; protocol mappers).
- `httpx` through `KeycloakAdminClient`; `kubernetes-asyncio` for the
  Secret.
- The secrets Job (#4041) makes every Secret the Job reads before the
  install starts.

## Known issues and limits

- The group names may change with the company-login design (#3959). They
  live in one values list, `coreKeycloak.groups`; `firstLogin.*.group` must
  name a group from it (the render fails otherwise).
- shortcut: the Job only adds. It never changes or deletes a group, a
  client setting, a mapper's config, a client-scope assignment or a realm
  role. A new client scope or realm role in a newer realm file reaches only
  new installs. Upgrade this to a full reconcile when a chart change needs
  one.
- A crash of the pod between creating a person and giving them their role
  (not a failed call, which rolls back) leaves that person without
  `platform_admin`; the next run sees the person and keeps them.
- The CI-only login and the second CI tenant (core design §8) are not here.
  They are off by default and wait for the #3959 decision.
- The backplane trusts the built-in Keycloak only after #4591. Until then
  the kind install can log in to Keycloak, but MEHO's own login check needs
  that wiring.

## References

- Core design, section 3 "Secrets at install", sub-decisions 3, 8 and 9,
  and Table 3e (internal design note).
- `docs/codebase/core-secrets.md` — the secrets Job and the hook account.
- `docs/codebase/approvals.md` — who may approve a change that waits.
- Keycloak bootstrap admin accounts:
  https://www.keycloak.org/server/bootstrap-admin-recovery
- Keycloak Admin REST API: https://www.keycloak.org/docs-api/latest/rest-api/index.html
- Helm chart hooks: https://helm.sh/docs/topics/charts_hooks/
