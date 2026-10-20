# Core secrets Job — the install makes the Secrets it needs (#4041)

## Overview

When a built-in part of the core is on (`corePostgres.enabled` or
`coreKeycloak.enabled`; later also `coreVault.enabled`), the Helm chart runs a
`secrets` Job before every install and every upgrade. The Job makes each
password and key the core needs, as a Kubernetes Secret named
`<release>-core-…`, **if the Secret is missing**. Nobody creates these
Secrets by hand.

What is true after the Job ran:

- Every Secret in the table below exists (or the operator's own Secret is
  used instead).
- An existing Secret is never changed. A second run, a `helm upgrade` or a
  `helm template | kubectl apply` keeps every value.
- No value appears in the Job's output, in the rendered chart or in the
  release record. The output holds Secret names and `created`, `kept` or
  `checked`.

With every built-in part off (bring-your-own), the chart renders none of the
new objects, so existing installs render byte for byte as before.

Pointing MEHO's own pods at these Secrets (the backplane, Valkey, the
Ingress) and moving the database migration into the backplane pod are the
work of the wiring Task (#4047) and the PostgreSQL Task (#4039). Until then
an install can point at a made Secret by name, for example
`approvalHandoff.secretName: <release>-core-approval-handoff` (see
"Use your own Secret" below).

## The Secrets

Names are `<release>-core-…`. "alnum 32" means 32 characters from
`[A-Za-z0-9]`. Fernet keys come from `cryptography`'s
`Fernet.generate_key()`.

| Secret | Keys | Format | Made when | Values key for your own Secret |
|---|---|---|---|---|
| `-core-postgres-superuser` | `password` | alnum 32 | `corePostgres.enabled` | `corePostgres.existingSecrets.superuser` |
| `-core-postgres-meho` | `password`, `url` | alnum 32; `postgresql+asyncpg://meho:<password>@<release>-postgres:5432/meho` | `corePostgres.enabled` | `postgres.credentialsSecret` |
| `-core-postgres-keycloak` | `password` | alnum 32 | `corePostgres.enabled` | `corePostgres.existingSecrets.keycloak` (not while `coreKeycloak.enabled` is on, see below) |
| `-core-keycloak-bootstrap` | `client_id`, `client_secret` | alnum 32 | `coreKeycloak.enabled` | `coreKeycloak.existingSecrets.bootstrap` |
| `-core-keycloak-admin` | `client_secret` | alnum 32 | `coreKeycloak.enabled` | `keycloakAdmin.clientSecret.secretName` (not while `coreKeycloak.enabled` is on, see below) |
| `-core-ui-auth` | `session_encryption_key`, `client_secret` | Fernet; alnum 32 | `coreKeycloak.enabled` | `uiConsole.secretName` (not while `coreKeycloak.enabled` is on, see below) |
| `-core-check-runner` | `client_secret` | alnum 32 | `coreKeycloak.enabled` | `checkRunner.clientSecret.secretName` (not while `coreKeycloak.enabled` is on, see below) |
| `-core-approval-handoff` | `approval_handoff_encryption_key` | Fernet | any built-in part | `approvalHandoff.secretName` |
| `-core-broadcast-auth` | `requirepass` | alnum 32 | any built-in part, with `broadcast.enabled` and `broadcast.auth.enabled` | `broadcast.auth.existingSecret` |
| `-core-tls` | `tls.crt`, `tls.key`, `ca.crt` | type `kubernetes.io/tls`; a CA and a certificate for `ingress.host` | any built-in part, with the Ingress and TLS on | `ingress.tls.secretName` |

One alphabet for every password avoids three kinds of bugs: Keycloak
expanding `$` in its settings, broken JSON, and URL escaping.

The first certificate (`-core-tls`) is an ECDSA P-256 certificate for
`ingress.host`, signed by a small CA made at the same time. The CA's private
key is thrown away after signing, so the CA can never sign a second
certificate. Both are valid for 825 days, the longest lifetime Apple
platforms accept for a TLS server certificate. The customer replaces the
pair with their own certificate by setting `ingress.tls.secretName`.

**Not made by this Job:**

- `-core-first-login` (the first admin's temporary passwords): the realm
  stage of the `bootstrap` Job (#4043, `docs/codebase/core-bootstrap.md`).
- The `meho-backplane` client secret: Keycloak makes it at realm import, and
  it never leaves Keycloak.
- `-core-vault-unseal`, `-core-vault-init`, `-core-vault-snapshot`,
  `-core-vault-scheduler`: the `vault-init` Job (#4044).
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MAIL_SMTP_PASSWORD`,
  `METRICS_AUTH_TOKEN`, `CORPUS_SERVICE_TOKEN`, `VCF_PAIF_OIDC_CLIENT_SECRET`:
  the operator gives them; they are never generated.

## Use your own Secret

Put your Secret's name in the values key of the table. The Job then makes
nothing for that row. It only reads your Secret and checks that it holds
every key MEHO reads from it (for `uiConsole.secretName`, the keys named in
`uiConsole.clientSecretKey` and `uiConsole.sessionEncryptionKey`; but see
"With the built-in Keycloak" below). If a key is missing, or the Secret
does not exist, the install stops with one sentence that names the Secret
and the key, for example:

```text
core_secrets_failed: Secret "my-handoff" has no key "approval_handoff_encryption_key"; add the key, or clear approvalHandoff.secretName so the install makes its own Secret.
```

An empty values key, or one that holds the `<release>-core-…` name itself,
lets the install make the Secret. `ingress.tls.secretName` is never checked,
because cert-manager may create that Secret after the install.

**With the built-in Keycloak.** The Keycloak subchart reads its Secrets by
their `<release>-core-…` names only; it cannot read these parent values
keys. While `coreKeycloak.enabled` is on,
`corePostgres.existingSecrets.keycloak`,
`keycloakAdmin.clientSecret.secretName`, `uiConsole.secretName` and
`checkRunner.clientSecret.secretName` must stay empty or name the
`<release>-core-…` Secret. A custom name there makes the Keycloak pod wait
forever for a Secret that is never created. The render-time check for this
lands with #4591. To use your own value for one of these Secrets, create it
with the `<release>-core-…` name before the install: the Job keeps a Secret
that exists. Only `coreKeycloak.existingSecrets.bootstrap` may name another
Secret, because the Keycloak subchart reads that key too.

## Key types and files

- `backend/src/meho_backplane/core/secrets.py` — the Job's program,
  `python -m meho_backplane.core.secrets`:
  - `Plan` (`make` and `check` entries) — read from `MEHO_CORE_SECRETS_PLAN`,
    which the chart renders. It holds names, kinds and hosts, never a value.
  - `build_data(entry)` — fresh values for one kind. The kind fixes the keys
    and their format.
  - `reconcile(plan, store)` — the rules below.
  - `KubernetesSecretStore` — `get` and `create` on Secrets through
    `kubernetes_asyncio`. Made Secrets carry only the label
    `meho.evoila.com/managed-by: core-bootstrap`.
- `backend/src/meho_backplane/core/tls.py` — `self_signed_tls(host)`.
- `deploy/charts/meho/templates/_core-secrets.tpl` — `meho.core.enabled`,
  the hook account name, the list of `<release>-core-*` names, and the plan.
- `deploy/charts/meho/templates/core-secrets-rbac.yaml` — the hook
  ServiceAccount, Role and RoleBinding.
- `deploy/charts/meho/templates/core-secrets-job.yaml` — the Job.

## Control flow

Helm order on `helm install` and `helm upgrade`:

| Weight | Object | Phases |
|---|---|---|
| -40 | ServiceAccount, Role, RoleBinding `<release>-core-bootstrap` | pre-install, pre-upgrade, post-install, post-upgrade |
| -30 | Job `<release>-core-secrets` | pre-install, pre-upgrade |
| -20 | the backplane ConfigMap (unchanged) | pre-install, pre-upgrade |
| -10 | migration Job (unchanged) | pre-install, pre-upgrade |
| 0 | release resources (Deployment, Services, …) | install, upgrade |

Rules of `reconcile`:

1. **Read everything first.** For each named Secret and each core Secret:
   one `get`. Any error other than "not found" stops the Job before anything
   is created. A read error is never treated as "absent".
2. **Check keys.** A named Secret that does not exist, or any Secret (named
   or an existing core Secret) that lacks a key MEHO reads, stops the Job
   with one sentence. Nothing is created.
3. **Make what is missing.** One `create` per missing core Secret. HTTP 409
   ("already exists") means kept. Any other create error stops the Job.

The Job uses `restartPolicy: Never` (the migration Job uses `OnFailure`):
with `OnFailure`, Kubernetes removes the pod once `backoffLimit` is reached,
and its log is the place that says what to fix. The container also sets
`terminationMessagePolicy: FallbackToLogsOnError`, so the sentence shows in
`kubectl describe pod`. `backoffLimit`, `ttlSecondsAfterFinished` and
`resources` come from `migrationJob.*`; the delete policy is the migration
Job's (`before-hook-creation,hook-succeeded`), so a failed Job stays for
`kubectl logs job/<release>-core-secrets`.

### Rights of the hook account

One namespaced Role, nothing cluster-wide:

- `create` on Secrets. Kubernetes cannot limit `create` to names; creating a
  name that exists fails with 409 and changes nothing, so an operator's
  Secret is never overwritten.
- `get` and `patch` only on the fifteen `<release>-core-*` names (the ten
  above plus `-core-first-login` and the four `-core-vault-*` Secrets).
- `get` only on the Secrets the operator named in values, to check their
  keys.
- `delete` only on `<release>-core-vault-init` (the `vault-init` Job of #4044
  deletes its short-lived Secret).
- No `list`, no `watch`, no access to the chart's own ServiceAccount.

The ServiceAccount mounts no token by default. Only the Job pods set
`automountServiceAccountToken: true`. The `bootstrap` (#4043) and
`vault-init` (#4044) Jobs use the same account.

The Job pod does not carry the backplane's selector labels, because the
backplane NetworkPolicy allows no egress to the Kubernetes API.

### GitOps and other deploy paths

- No Helm template function (`randAlphaNum`, `lookup`) makes a value, so
  `helm template`, Argo CD and klipper-helm see the same render every time.
- No `argocd.argoproj.io` annotation. Argo CD maps the Helm hooks to PreSync
  and PostSync; the Job runs on every sync and changes nothing when nothing
  is missing.
- Made Secrets carry no chart labels, so Argo CD's label tracking cannot
  adopt, diff or prune them.
- Under `helm template | kubectl apply` the Job runs as a plain Job. Pods
  that read a made Secret wait in `CreateContainerConfigError` until it
  exists, then start.

### Uninstall and reinstall

Hook objects and Job-made Secrets are not part of the release, so
`helm uninstall` leaves them (the hook account too). A reinstall into the
same namespace keeps every value (409). Delete the Secrets and the data
volumes together, or neither: new passwords do not match old data.

## Dependencies

- `kubernetes_asyncio` (already in the backplane image) for the two API
  calls, `cryptography` for Fernet keys and the certificate, `pydantic` for
  the plan.
- The backplane image: the Job runs the same image as the Deployment
  (digest pin included), so the offline image list does not grow.

## Known issues and limits

- Rotation is out of scope. The layout does not block it: one Secret per
  user, one key per value. To rotate, write the new value and restart the
  pods that read it.
- `-core-broadcast-auth` is made, but Valkey and the backplane keep reading
  the subchart's own `<release>-broadcast-auth` until #4047 wires them. A
  subchart cannot read the parent's `core*` values; only `global` or the
  `broadcast:` block reach it.
- The PostgreSQL subchart (#4039) must name its Service `<release>-postgres`;
  the made database URL points there.
- A kept `-core-tls` keeps the host it was made for. After a change of
  `ingress.host`, delete `<release>-core-tls` and run `helm upgrade`; the Job
  makes a new certificate (and a new CA, so give the CLI the new `ca.crt`).
- The kind CI job that installs the full core and checks every made Secret
  (#4051) does not exist yet.

## References

- Core design, section 3 "Secrets at install" (internal design note).
- `docs/codebase/core-bootstrap.md` — the `bootstrap` Job that finishes the
  Keycloak realm after the install.
- `docs/codebase/approvals.md` — the approval hand-off key.
- `docs/codebase/devops.md` — chart layout and the migration Job.
- Kubernetes RBAC, "Referring to resources" (no `resourceNames` on
  `create`): https://kubernetes.io/docs/reference/access-authn-authz/rbac/
- Helm chart hooks (default delete policy, weights, tracking):
  https://helm.sh/docs/topics/charts_hooks/
- Apple's TLS server certificate rules (825 days):
  https://support.apple.com/en-us/103769
