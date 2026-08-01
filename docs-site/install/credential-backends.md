# Credential backends: Vault or Google Secret Manager

When an agent (or you) runs an operation against a credentialed target
— a vCenter, a firewall, an appliance — MEHO fetches that target's
credential from a secret store *at dispatch time*, scoped to the
calling tenant, and never hands it to the caller. That store is the
**credential backend**, selected once per deployment with
`config.credentialBackend`.

Two backends are supported. Both use the same chart and the same
target model; they differ in what you must run, how background work
authenticates, and maturity.

| | **Vault** (default) | **Google Secret Manager** |
|---|---|---|
| You run | A HashiCorp Vault (KV v2) reachable from the cluster | Nothing extra — GSM is a Google Cloud API |
| MEHO authenticates via | A Vault JWT role bound to your Keycloak issuer | The Pod's ambient Google identity (Workload Identity on GKE); no service-account key files |
| Secret references look like | `targets/<id>` or `vault:targets/<id>` | `gsm:<project>/<secret-name>` |
| Fits when | You are on-prem, or Vault is already your secret store | Your platform is GCP-native and you would rather not operate Vault |
| Maturity | Core path — carries the GA read plane | **Beta**, targeting v1.0.0 — see the [maturity index](../reference/maturity.md) |

## What each backend requires

### Vault

- `config.credentialBackend: vault` (the default — omitting it selects
  Vault).
- A reachable Vault with a **KV v2** mount and a **JWT auth role**
  (conventionally named `meho-mcp`) bound to your Keycloak issuer and
  audience, so MEHO can exchange a caller's Keycloak token for a Vault
  token. The role + policy provisioning walk-through is
  [`docs/cross-repo/vault-provisioning.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md).
- The `vault.address`, `vault.role`, `vault.paths.kv` values (plus the
  mirrored `config.vaultAddr`) — shown on
  [the install trail, Step 6](index.md#step-6-write-your-values-file).

One behaviour to know about up front: target secret references are
**tenant-scoped by default**. A schemeless reference like
`targets/<id>` resolves under a per-tenant prefix
(`secret/tenants/<tenant-id>/…`), so one tenant can never read
another's credentials. This matters when you write secrets into Vault
for MEHO to find — the
[Vault tenant-scope guide](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault-tenant-scope.md)
has the path shapes.

### Google Secret Manager

- `config.credentialBackend: gsm`, plus `gsm.enabled: true` and
  **both** `gsm.project` and `config.gsmProject` set to your project.
  They are different keys: the schema validates `gsm.project`, but the
  backplane reads `GSM_PROJECT`, which the chart renders from
  `config.gsmProject`. Set only the first and the install validates and
  then fails every credential read against an empty project.
  `vault.address` stays **blank** — the chart's schema requires it only
  for the Vault backend.
- An ambient Google identity for the Pod: on GKE, Workload Identity
  binding the Kubernetes ServiceAccount to a Google service account
  with `roles/secretmanager.secretAccessor` on MEHO's secrets. There
  are no service-account JSON keys anywhere in this design.
- Optionally `config.gsmImpersonateSa` to impersonate a dedicated
  reader service account.

The full annotated example is
[`values-gsm-example.yaml`](https://github.com/evoila/meho/blob/main/deploy/values-examples/values-gsm-example.yaml).

**Per-operator reads (optional).** Setting
`gsm.workloadIdentityFederation.audience` switches credential reads
onto the *calling operator's* identity: MEHO exchanges the operator's
Keycloak token with Google's Security Token Service, so GCP's own
audit log names the human, not MEHO's service account. This is the
strongest attribution story — and it is exactly what complicates
background work, below.

## Helm values per backend

Vault (default):

```yaml
config:
  # credentialBackend: vault   # the default; may be omitted
  vaultAddr: https://vault.example.com
vault:
  address: https://vault.example.com
  authMethod: oidc
  role: meho-mcp
  paths:
    kv: secret/meho
```

Google Secret Manager:

```yaml
config:
  credentialBackend: gsm
  gsmProject: <your-gcp-project>   # rendered as GSM_PROJECT; the backplane reads this one
  # gsmImpersonateSa: meho-reader@<project>.iam.gserviceaccount.com  # optional
gsm:
  enabled: true
  project: <your-gcp-project>      # same value; this is the key the schema validates
  # workloadIdentityFederation:
  #   audience: //iam.googleapis.com/projects/.../providers/...   # optional, see above
```

## The honest support matrix

Interactive operations — an operator or agent calling with a live
token — work identically on both backends. Where the backends differ
today is **background execution**: sensor evaluations and scheduled
work run on a timer with *no calling operator*, so there is no token
to federate. This is an area under active hardening (sensors and the
scheduler are **Beta**, tracked toward v1.0.0 in
[#2668](https://github.com/evoila/meho/issues/2668)); the current
state, honestly:

| Deployment shape | Interactive operations | Background (sensors / scheduler) on credentialed targets |
|---|---|---|
| **Vault** | ✅ Works | ⚠️ Requires the check-runner principal (below). Works once configured — but read the privilege caveat. A durable machine-credential identity (including the Vault token-lifecycle root cause) is the open hardening item, [#2668](https://github.com/evoila/meho/issues/2668). |
| **GSM, service-account reads** (no per-operator WIF) | ✅ Works | ✅ Works — background reads fall back to the Pod's own Google identity; no extra configuration. |
| **GSM, per-operator WIF, on GKE** | ✅ Works | ⚠️ Background *dispatch* falls back to the Pod identity and works. But connector *probes* of credentialed targets take the per-operator path with a placeholder token, fail the exchange harmlessly, and report `reachable=false` / `auth_failed` — a documented current limitation. |
| **GSM, per-operator WIF, no Pod identity** (non-GKE) | ✅ Works | ❌ Fails closed **unless** the check-runner principal is configured — there is no identity to read with. Credentialed sensors read `unknown` forever. |

Targets whose credentials MEHO does not fetch per read (for example,
unauthenticated network diagnostics) are unaffected by all of this.

### The check-runner principal

The `checkRunner.*` chart values give background work a real identity:
a confidential Keycloak client whose `client_credentials` token the
runner presents on scheduled reads.

```yaml
checkRunner:
  enabled: true
  clientId: meho-check-runner
  clientSecret:
    secretName: meho-check-runner   # a Secret you provision
    secretKey: client_secret
```

Realm-side, create the confidential client with the
`client_credentials` grant and the audience mapper — same shape as the
other service principals
([`docs/cross-repo/keycloak-agent-client.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/keycloak-agent-client.md)).

!!! warning "On Vault, the check-runner is a privilege decision — not a no-op"

    The conventionally-provisioned `meho-mcp` Vault role accepts *any*
    principal carrying the backplane audience, and its policy reads
    the whole MEHO secret tree. Enable the check-runner against that
    role unchanged, and **every scheduled evaluation can read any
    target credential** — the "background work cannot read
    operator-context secrets" carve-out is gone. That may be exactly
    what you want (it is what makes credentialed sensors work on
    Vault), but decide it consciously: give the runner its own,
    narrower Vault role, or bind the existing role to operator-only
    claims. Both recipes, with verification commands, are in
    [`docs/cross-repo/vault-provisioning.md` § Bounding the check-runner principal](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md).
    The chart prints this same warning at install time when
    `checkRunner.enabled: true` meets the Vault backend.

## Can I switch later?

The backend is a deployment-level choice, and target secret
references are written in the backend's reference shape — so
switching means re-pointing every credentialed target's
`secret_ref` and re-provisioning secrets in the new store. It is not
a values-flip. Choose with your platform direction in mind; when in
doubt and on-prem, choose Vault.

## Back to the trail

Return to [the install trail, Step 3](index.md#step-3-provision-the-database-credentials-secret).
