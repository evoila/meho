# OpenBao setup Job — `vault-init` sets up the built-in OpenBao (#4044)

## Overview

With the built-in OpenBao on (`coreVault.enabled: true`), the chart runs
one Job that sets OpenBao up. When the Job is done:

- OpenBao is started ("initialised") with one unseal key, and it is
  unsealed.
- The unseal key is in the Secret `<release>-core-vault-unseal`. The
  `unseal` container of the OpenBao pod uses it after every restart (#4046).
- Users log in with their Keycloak token. Each user reads and writes only
  the secrets of their own tenant, from the first login. No hand step.
- No root token exists. Later runs of the Job log in with the Job's own
  service account.

The Job is `<release>-vault-init-<hash>`. It is a normal release object,
not a Helm hook, so `helm install --wait --wait-for-jobs`, klipper-helm and
Argo CD all wait for it. The name ends with 8 characters of a hash of the
Job's pod template and the policy files. A changed setup (a new MEHO image,
other addresses, other policies) gives a new name, so a new Job runs. An
unchanged setup keeps the name, and nothing runs again. The Job's pod
template cannot be changed in place, which is a second reason for the hash.

The chart refuses `coreVault.enabled` without `coreKeycloak.enabled`:
OpenBao trusts logins from the built-in Keycloak only.

A second run changes nothing. The Job's output names objects and says
`made`, `kept` or `set`. It never prints the unseal key or a token.

## Key types and files

| File | What it holds |
| --- | --- |
| `backend/src/meho_backplane/core/vault_init.py` | The Job. `set_up()` runs the steps; `Plan` is what the chart renders into `MEHO_CORE_VAULT_PLAN`; `HttpBao` calls OpenBao; `KubernetesCluster` reads the cluster's token issuer and signing keys; `pem_keys()` turns those keys into PEM text. |
| `backend/src/meho_backplane/core/secrets.py` | `KubernetesSecretStore`, shared with the `secrets` and `bootstrap` Jobs. The Job uses `read_data`, `put` (create, or replace the values) and `delete`. |
| `deploy/charts/meho/templates/core-vault-init.yaml` | The Job, its ServiceAccount, Role and RoleBinding, and the ConfigMap `<release>-vault-init-policies`. |
| `deploy/charts/meho/templates/_core-vault-init.tpl` | The name, the plan and the pod template (the hash covers the pod template). |
| `deploy/charts/meho/files/openbao/policies/*.hcl` | The 8 policies. `JWT_ACCESSOR` in the tenant policies is replaced by the Job with the accessor of the `jwt` login. |
| `backend/tests/test_core_vault_init.py` | Unit tests against a fake OpenBao. The fake checks every call of a later run against the policies, so it proves `meho-configurator` is enough. |
| `backend/tests/integration/test_core_vault_init_openbao.py` | The Job against a real OpenBao and a real Keycloak: two tenants, the admin login, the scheduler token, the second run. |
| `backend/tests/test_chart_core_openbao.py` | Chart renders: the objects, the Role, the plan, the name hash, nothing cluster-wide. |

What the Job sets up in OpenBao:

| Object | Shape |
| --- | --- |
| `secret/` | key-value store, version 2 |
| login `jwt/` | `jwks_url` = Keycloak's keys through its Service (`http://<release>-keycloak:8080/auth/realms/meho/protocol/openid-connect/certs`); `bound_issuer` = the public issuer (`https://<host>/auth/realms/meho`) |
| role `meho-mcp` on `jwt/` | for every tenant: audience `meho-backplane`; `claim_mappings` copies `tenant_id` and `tenant_role` into the login's metadata; `groups_claim: tenant_role`; policy `meho-mcp`; 1 hour |
| groups `meho-tenant-read-only`, `-operator`, `-admin` | external groups, one per `tenant_role` value (`read_only`, `operator`, `tenant_admin`, from `TENANT_POLICY_FOR_ROLE`); each gives its policy |
| role `meho-admin` on `jwt/` | only for tokens whose `groups` claim holds `MEHO-Platform-Admins` (the realm puts the first admin there); policy `meho-admin` (everything); 1 hour |
| login `kubernetes-sa/` | type `jwt`, with the cluster's signing keys as PEM text and the cluster's issuer |
| role `vault-init` on `kubernetes-sa/` | only the Job's service account (`bound_subject`), only tokens for the audience `openbao`; policy `meho-configurator`; 15 minutes |
| token role `meho-periodic` | orphan tokens with a period of 768 hours, policies `meho-scheduler` or `meho-snapshot` |
| `secret/meho/test/federation` | the test secret that `GET /api/v1/health` reads |

The policies:

| Policy | Rights |
| --- | --- |
| `meho-mcp` | read `secret/meho/*` (docs/cross-repo/vault-provisioning.md §3) |
| `meho-tenant-read-only`, `-operator`, `-admin` | the bodies of docs/cross-repo/connector-vault-tenant-policy.md §2, but the tenant comes from `identity.entity.aliases.<jwt accessor>.metadata.tenant_id` (the login's metadata), not from entity metadata. Only `-admin` reaches the break-glass path `tenants/<id>/privileged/`; the other two deny it |
| `meho-admin` | everything, with `sudo` |
| `meho-scheduler` | docs/cross-repo/vault-provisioning.md §6 |
| `meho-snapshot` | read and `sudo` on `sys/storage/raft/snapshot` (#3960) |
| `meho-configurator` | exactly what a later run of the Job changes. It can write policies, so it is an admin login in practice; only the Job's service account gets it |

The Secrets:

| Secret | Key | Lifetime |
| --- | --- | --- |
| `<release>-core-vault-unseal` | `key` | stays; delete it for "keyholder at boot" (#4046) |
| `<release>-core-vault-init` | `token` (the root token) | only while the first run runs; deleted at the end |
| `<release>-core-vault-scheduler` | `token` | stays; the backplane renews it (wiring: #4047) |
| `<release>-core-vault-snapshot` | `token` | stays; the backup job renews it (#3960) |

## Control flow

1. Read the 8 policy files. A missing file stops the Job before any call.
2. Wait until OpenBao answers (up to 10 minutes): a new install starts the
   Job and the OpenBao pod at the same time. Then `GET sys/init`.
   - **New server:** `init` with one key share and threshold 1. Store the
     unseal key first, then the root token, then unseal. The key goes first:
     without it the server can never be unsealed again.
   - **Server set up before:** wait until it is unsealed (up to 10 minutes;
     in keyholder mode a person unseals it). If `<release>-core-vault-init`
     holds a root token that still works, an earlier run stopped halfway:
     go on with it. Else log in with the `kubernetes-sa` login. A left-over
     Secret with a revoked root token is deleted. If that login fails, the
     Job stops and says how to give it a root token (see "When the Job
     cannot log in" below).
3. Wait until Keycloak serves realm `meho` (up to 10 minutes).
4. Set up the objects of the tables above. Mounts, groups, the test secret
   and the two tokens are read first and made only when missing; a group is
   written only when its type or policy differs (a real OpenBao stamps a
   rewritten group's alias with a new time). Policies, roles and login
   configs are written every time with the same values, which changes
   nothing.
5. Tokens: on a new server the Job always makes both tokens, so Secrets
   left from an earlier server are replaced. On a later run it keeps a token
   that still works (`auth/token/lookup-self` with that token) and makes a
   new one for a dead token.
6. If the Job used the root token: revoke it, then delete
   `<release>-core-vault-init`.

### The Job's login on later runs

Kubernetes' own login method checks tokens with the TokenReview API, which
needs a cluster-wide binding. The Job uses a second `jwt` login instead:

- The Job reads the cluster's issuer (`/.well-known/openid-configuration`)
  and keys (`/openid/v1/jwks`) with its normal token. Every service account
  may read them (the default `system:service-account-issuer-discovery`
  binding).
- OpenBao gets the keys as PEM text, not as a JWKS address: the CIS profile
  turns off anonymous access to the API server, and OpenBao sends no token
  when it fetches keys.
- The pod has a second, projected token for the audience `openbao`. Its
  normal API token has the API server's audience, so OpenBao refuses that
  one, and the API server refuses the `openbao` one.

Checked on RKE2 v1.37.1 with the CIS profile and namespace rights only:
the issuer and keys are read, and the Secret calls work.

### When the Job cannot log in

The `kubernetes-sa` login fails when OpenBao was set up some other way (by
hand, before this Job existed), when the cluster's signing key changed, or
when the first run stopped before it stored the root token. The fix is the
same each time:

1. Make a root token with `bao operator generate-root` and the unseal key
   (from `<release>-core-vault-unseal`, or from the keyholder). See the
   OpenBao docs, `commands/operator/generate-root`.
2. Put it into the Secret `<release>-core-vault-init`, key `token`.
3. Run the Job again (below). It goes on with that token, sets everything
   up, then revokes the token and deletes the Secret.

### Run the Job again

A failed Job stays failed: its name only changes when its setup changes.
Delete it (`kubectl delete job -l
app.kubernetes.io/instance=<release>,app.kubernetes.io/component=vault-init`)
and run `helm upgrade` again with the same values; Helm makes the missing
Job again. Argo CD does the same on its next sync.

### Kubernetes rights

The Job has its own ServiceAccount `<release>-vault-init` with a Role:

- `create` on Secrets (Kubernetes cannot limit `create` to names);
- `get` and `patch` only on the four `<release>-core-vault-*` Secrets;
- `delete` only on `<release>-core-vault-init`.

It does not use the shared hook account `<release>-core-bootstrap`: that
account is a Helm hook, deleted and made again in the post-install phase,
which can run while this Job still runs (Helm without `--wait`), and a pod
of a deleted account loses its API token.

### Network rules

The Job's pod carries `app.kubernetes.io/instance: <release>` and
`app.kubernetes.io/component: vault-init`, not the backplane's selector
labels (the backplane NetworkPolicy allows no egress to the Kubernetes
API). With the subcharts' NetworkPolicies on:

- OpenBao's policy admits port 8200 from the Job;
- Keycloak's policy admits port 8080 from the Job (it waits for the realm)
  and from the OpenBao pod (it fetches the realm's keys to check logins).

## Dependencies

- OpenBao subchart `charts/openbao/` and its `unseal` container (#4046).
- Keycloak subchart `charts/keycloak/` and its realm file: the `meho-cli`
  client's `tenant_id`, `tenant_role` and `groups` claims, the
  `MEHO-Platform-Admins` group.
- `httpx`, `kubernetes_asyncio` (`WellKnownApi`, `OpenidApi`, `CoreV1Api`),
  PyJWT (`PyJWK`, for RSA and EC keys), `cryptography`.

## Known issues and limits

- **The backplane is not pointed at the built-in OpenBao yet.** That is
  #4047 (with `coreVault` on by default). Until then, set `vault.address`
  and the other `vault.*` values by hand to use it.
- **The check runner cannot log in.** Its token has only the
  `meho-check-runner` audience and no tenant claims, so `meho-mcp` refuses
  it. Its own role belongs to the task that turns the runner on (#4766).
- **The cluster's signing key is a copy.** If an admin rotates the
  cluster's service-account key, later runs cannot log in. Fix: "When the
  Job cannot log in" above.
- **A first run that stops between `init` and storing the key** leaves a
  server nobody can unseal. The window is two API calls. Fix: delete the
  OpenBao data disk (PVC `data-<release>-vault-0`) and the pod, then run
  the Job again.
- **Helm runs the Job only once per setup.** The install still needs two
  Helm runs today (the first with `--no-hooks`, #4047). If the second run
  comes after the Job gave up waiting for Keycloak (4 tries of 10 minutes),
  run the Job again (above).
- **Vault image (`coreVault.binary: vault`, tests only):** the Job does not
  turn on an audit device for it.

## References

- Ticket: #4044.
- OpenBao API (v2.7.1): `website/content/docs/api/system/init.mdx`,
  `api/auth/jwt.mdx` (`claim_mappings`, `groups_claim`, `bound_claims`,
  `jwt_validation_pubkeys`), `api/secret/identity/group-alias.mdx`,
  `api/auth/token.mdx` (token roles: `orphan`, `token_period`).
- docs/codebase/core-secrets.md (the shared Secret code),
  docs/codebase/devops.md (the OpenBao subchart).
