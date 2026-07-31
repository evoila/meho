# Register targets and secrets

A **target** is one system MEHO operates against — a Kubernetes API
server, a vCenter, a Vault, a firewall. Registration gives the
backplane three things: the coordinates to reach the system (`host`,
`port`, TLS trust), the `product` token that selects which connector
serves it, and a `secret_ref` — a *reference* into your credential
store. MEHO never stores the credential itself; it resolves the
reference at dispatch time, under an identity your store can audit.

Until at least one target is registered and its probe is green, an
agent session can discover operations but cannot act on anything —
which is why this is the first guide.

!!! note "Prerequisites and roles"

    - A running backplane ([Install & operate](../install/index.md))
      and an authenticated CLI session
      ([Connect clients](../clients/index.md)):
      `meho login https://meho.example.com`.
    - Registration verbs (`import`, create, edit, delete) require the
      **tenant_admin** role. Read verbs (`list`, `describe`, `probe`,
      `discover`) need **operator**. If a step below answers
      `403 insufficient_role`, that is your JWT's role, not a broken
      deploy.

## Choose a registration path

| Path | What it is | When to use it |
|---|---|---|
| **CLI — `meho targets import`** | Bulk-import from a `targets.yaml` file you keep in version control. | The canonical path. Your target registry becomes a reviewable, re-runnable file. |
| **Operator console — `/ui/connectors`** | Create/edit forms plus a paste-or-upload `targets.yaml` import with a preview table. | Console-first operators. Produces byte-identical writes to the CLI import. |
| **REST — `POST /api/v1/targets`** | The API the other two paths call. | Automation that already speaks REST. |

**There are no MCP tools for registering targets or staging secrets —
deliberately.** Registration is an operator-trust decision, so it
lives on the operator surfaces above; agents get the read-only
`list_targets` tool and consume whatever you registered. This is also
why the CLI is worth installing before your first agent session. If
that boundary ever moves, the change will be tracked on the
[feature-maturity index](../reference/maturity.md).

## Write a `targets.yaml`

The file is a `targets:` list. A minimal, realistic pair — a
Kubernetes cluster and a VMware appliance that is reached by IP:

```yaml
targets:
  - name: lab-rke2
    product: k8s                    # registered product token — NOT "kubernetes"
    host: rke2-api.lab.example.com
    port: 6443
    auth_model: shared_service_account
    notes: RKE2 lab cluster, evaluation estate.

  - name: lab-vcenter
    product: vmware
    host: 10.20.0.15                # reached by IP...
    tls_server_name: vc01.lab.example.com   # ...but the cert's SAN is the FQDN
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      ...internal CA, PEM...
      -----END CERTIFICATE-----
    notes: vCenter reached by IP; SNI + cert verification pinned to the SAN name.
```

Mapping rules the importer applies:

- **Recognised top-level keys** map 1:1 to real columns: `name`,
  `aliases`, `product`, `host`, `port`, `fqdn`, `secret_ref`,
  `auth_model`, `vpn_required`, `notes`, `preferred_impl_id`,
  `verify_tls`, `tls_ca_pin`, `tls_server_name`, `extras`.
- **`name`, `product`, and `host` are required** and validated
  locally before any HTTP request.
- **Unknown keys are not errors** — they spill into the target's
  `extras` JSON column, so you can carry your own annotations.
- **`fingerprint` is server-managed** and dropped with a warning; the
  probe verb is its only writer.
- `verify_tls: false` and `tls_ca_pin` are mutually exclusive (422):
  pin the CA *or* disable verification, never both.
- Set **`tls_server_name`** whenever you reach an appliance by IP (or
  by an alias) while its certificate only carries the FQDN — without
  it, dispatch fails TLS verification with an
  `connector_tls_verify_failed` / "IP address mismatch" class error.

You can leave `secret_ref` out entirely — see the next section.

## Stage the secret

Every target that needs credentials carries a `secret_ref`. Which
store it points at is decided by your deploy's credential backend
(`vault`, the default, or `gsm` — see the
[install trail](../install/index.md)).

### Vault (default backend)

The `secret_ref` is the **logical KV-v2 path relative to the `secret`
mount** — never include the mount or the `/data/` segment. The
canonical, enforced layout is per-tenant:

```
tenants/<tenant-id>/<target-name>
```

If you omit `secret_ref` at registration, **the server derives exactly
that path for you**. The reliable recipe is therefore: register first,
read the derived reference back, then stage the secret at it.

```bash
# 1. After import, read the derived reference:
meho targets describe lab-vcenter
#   ...
#   secret_ref:        tenants/4f6f6f6f-.../lab-vcenter

# 2. Stage the credential there with your own Vault tooling.
#    The Vault CLI addresses the *wire* path, which prepends the mount:
vault kv put secret/tenants/4f6f6f6f-.../lab-vcenter \
  username=svc-meho password='...'
```

The two path notations trip everyone once, so:

| Context | Path shape | Example |
|---|---|---|
| MEHO `secret_ref` | logical, no mount | `tenants/<tenant-id>/lab-vcenter` |
| Vault CLI / API | mount-prefixed | `secret/tenants/<tenant-id>/lab-vcenter` |

Rules the backplane enforces:

- An explicitly supplied Vault `secret_ref` must stay **inside your
  tenant subtree** (`tenants/<tenant-id>/…`). Anything else is
  rejected at registration with a structured 422
  (`secret_ref_outside_tenant_scope`) that names the expected path —
  because your operator Vault identity could never read it at
  dispatch anyway.
- A mount- or API-shaped ref (`secret/data/…`, `kv/data/…`,
  `data/…`) is rejected at read time: Vault inserts the `/data/`
  segment itself, so a prefixed value double-resolves to a 404.
- Most connectors expect the fields **`username`** and
  **`password`**. Some want a different shape — the Kubernetes
  connector reads a single field named **`kubeconfig`** holding the
  kubeconfig YAML. The per-connector field contract is listed in each
  connector's onboarding notes
  ([`docs/cross-repo/`](https://github.com/evoila/meho/tree/main/docs/cross-repo)).

```bash
# Kubernetes example — one field, whole kubeconfig as the value:
vault kv put secret/tenants/<tenant-id>/lab-rke2 \
  kubeconfig=@rke2-kubeconfig.yaml
```

### Google Secret Manager (`credentialBackend: gsm`)

The ref grammar is:

```
gsm:<project-id>/<secret-name>[/versions/<version>][#<field>]
```

- `gsm:my-project/lab-vcenter-creds` — latest version, whole payload.
- `gsm:my-project/lab-vcenter-creds/versions/3` — pinned version.
- `gsm:my-project/lab-vcenter-creds#password` — one field only.

The secret's payload **must be a JSON object of named fields**, e.g.
`{"username": "svc-meho", "password": "..."}` — the same field names
the connector expects on Vault. A kubeconfig ref looks like
`gsm:my-project/lab-rke2-kubeconfig#kubeconfig`.

On a GSM deploy, set `secret_ref` explicitly in the YAML (the
server-derived default is the Vault layout).

## Import and verify

```bash
# 1. Preview — classifies every entry CREATE / UPDATE / SKIP, writes nothing:
meho targets import targets.yaml --dry-run

# 2. Apply. Default mode aborts before any write if a name already exists:
meho targets import targets.yaml
#   Applied: 2 created, 0 updated.

# 3. Iterate later with --update: PATCHes existing names, creates new ones.
#    The PATCH is sparse — fields your YAML omits are left untouched.
meho targets import targets.yaml --update
```

Then probe. The probe invokes the connector matched to the target's
`product`, live, and persists the resulting fingerprint:

```bash
meho targets probe lab-rke2
```

```text
vendor:        kubernetes
product:       rke2
version:       v1.31.4+rke2r1
reachable:     true
probed_at:     2026-07-31T09:14:22Z
probe_method:  GET /version
```

**Probe-green means `reachable: true` with the product/version
identified.** (The fingerprint's `product` is what the connector
*observed* — here the RKE2 distribution — which can be more specific
than the `product` token you registered the target under.) The fingerprint is cached on the target row —
`meho targets describe lab-rke2` shows it from then on without
re-probing, and the connector resolver uses it to pick the right
implementation version at dispatch time.

Two useful companions:

```bash
meho targets list --product k8s     # everything registered, filterable
meho targets discover k8s           # candidate systems a connector can see
                                    # from an already-registered seed target
```

## What can go wrong here

Real failure modes, with the errors they actually produce:

| Symptom | What it means | Fix |
|---|---|---|
| 422 at registration: *"target destination is not a public address; refusing it as a server-side request forgery risk…"* | The SSRF guard rejects private/internal addresses by default. The number-one surprise on on-prem first installs. | Add your internal ranges or hostnames to the chart's `config.targetSsrfAllowlist` (env `MEHO_TARGET_SSRF_ALLOWLIST`) and upgrade the release. |
| 422 `unknown_product`, listing `valid_products` | The `product` token isn't one a registered connector advertises. Product tokens are exact — `k8s`, not `kubernetes`. | Pick a token from the error's `valid_products` list. |
| 422 `secret_ref_outside_tenant_scope` | You supplied a Vault ref outside `tenants/<tenant-id>/…`. It would fail with "permission denied" at every dispatch. | Use the `expected_secret_ref` the error names — or omit `secret_ref` and take the server default. |
| Probe exits with 501 — *"no connector registered for product=…"* | The target row is fine; no connector serves that product on this backplane version. Any cached fingerprint is left untouched. | Check the [connector catalog](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-catalog.md) for your version; for spec-ingested (generic) connectors, ingest one first. |
| Dispatch fails, `connector_error: VaultCredentialsReadError` — *"…has a KV-v2 API-path-shaped secret_ref…"* | Your ref embeds the mount or a `data/` segment (`secret/data/…`). | Store the logical path only: `tenants/<tenant-id>/<name>`. |
| Dispatch fails, `connector_error: VaultCredentialsReadError` — *"…is missing required field '…'"* | The secret exists but doesn't carry the field the connector needs (`username`/`password`, or `kubeconfig` for Kubernetes). | Re-stage the secret with the connector's field names. |
| Dispatch fails, `connector_vault_forbidden` — Vault answered *permission denied* | Nine times out of ten the secret is staged at the wrong path, **not** a missing grant: the error reads like a policy problem but the ref simply doesn't match where you wrote the secret. The error names the expected path. | `vault kv put` the credential at the expected path. Do **not** widen the backplane's Vault policy — it is deploy-owned and re-applied on upgrade. |
| Dispatch fails, `connector_auth_failed` (HTTP 401 at session establish) | The credential resolved but the vendor rejected it — usually a password rotated upstream while the store copy lagged. | Re-stage the current credential at the same path, then retry. |
| Dispatch fails TLS: `connector_tls_verify_failed`, *"IP address mismatch"* | You reach the appliance by IP but its certificate only names the FQDN. | Set `tls_server_name: <cert-fqdn>` on the target and re-import with `--update`. |
| Probe shows `reachable: false` / `auth_failed` on a GSM deploy with per-operator Workload Identity Federation | Probes run under a system placeholder identity that cannot complete the per-operator token exchange, so credentialed-target probes cannot read the secret on that configuration. Reachability of *uncredentialed* endpoints still probes fine. | Known limitation — documented in [`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md); verify credentialed dispatch with a real operation call instead. |
| Registration accepted, but *every* dispatch fails with a `no_connector` error naming the wrong thing | A `product` no connector implementation actually serves can slip through registration in some cases; the dispatch-time error then misattributes the cause. | Known issue, tracked in [evoila/meho#2701](https://github.com/evoila/meho/issues/2701). Re-check the target's `product` against the connector catalog. |

!!! tip "Prove the secret is readable — without printing it"

    If Vault itself is registered as a MEHO target, dispatch the
    metadata-only `vault.kv.versions` operation against the
    credential's path instead of a read: it proves the path resolves
    and the policy allows access while returning version numbers,
    never values.

**Next:** [Run your first operations](first-operations.md).
