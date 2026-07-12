<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# values-examples — sanitized chart-values templates

This directory ships **sanitized example values files** for the MEHO Helm
chart at [`deploy/charts/meho/`](../charts/meho/). Each file targets a
specific deployment shape (today: the RDC Hetzner dogfooding lab) and is
designed to be copied into a private deploy repo, the placeholders
substituted, and applied via `helm install` / `helm upgrade`.

The files here ship **no real secrets, no real CIDRs**. Every
site-specific field uses a `<REPLACE: ...>` placeholder that fails
`values.schema.json` validation at install time, so an operator who
forgets to substitute one fails-loud at `helm install` rather than
silently connecting to the wrong system or CrashLoopBackOff'ing at first
request.

## Files

| File | Targets | Backed by |
| --- | --- | --- |
| [`values-rdc-example.yaml`](./values-rdc-example.yaml) | The RDC Hetzner lab (`*.evba.lab` hosts, rke2-infra ingress-nginx, cluster-internal Postgres + Vault + Keycloak). | The actual, private file lives in [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)'s `manifests/meho/values-rdc.yaml`; this file is the sanitized template for other Vault-+-Keycloak-+-Postgres-shaped labs. |
| [`values-gsm-example.yaml`](./values-gsm-example.yaml) | A **Vault-free, GCP-native** install (Initiative #2227) — credentials + the `/api/v1/health` federation proof resolve through GCP Secret Manager (`config.credentialBackend: gsm`) instead of Vault. `vault.address` is left blank; the schema requires it only when `credentialBackend: vault`. | The GSM SA-direct backend (#2230). Per-operator GCP token exchange (Workload Identity Federation) is Phase 2 (#2232); the `gsm.workloadIdentityFederation.*` keys are inert stubs until then. |

## Using `values-rdc-example.yaml`

1. **Copy** the file into your private deploy repo. Do not put real
   CIDRs / hostnames / image tags into a public repo.

2. **Substitute** the `<REPLACE: ...>` placeholders. The minimum required
   set:

    | Placeholder | What to put |
    | --- | --- |
    | `image.tag` | An immutable tag from the G2.4 image pipeline. Production: `sha-<40-char-git-sha>` from a green CI run. Pre-prod / lab: `v0.1.0` once the release tag exists. **`:latest` and `:main` are forbidden by Goal #11 deploy discipline.** |
    | `config.keycloakIssuerUrl` / `keycloak.issuer` realm | Your Keycloak realm name. Both fields must agree — the ConfigMap-sourced env mirrors the values block (`config.keycloakIssuerUrl` is what the backplane process reads at startup). |
    | `config.keycloakCliClientId` | The client_id of the **public** Keycloak client `meho login` uses for device-code flow. Pre-create the client in the realm above per the [auth onramp recipe](#auth-onramp-recipe-cli--mcp) (`meho-cli` is the suggested default). Leaving this empty keeps v0.3.1 behaviour: the backplane endpoint serves an empty value and `meho login` surfaces an actionable public-client error. |
    | `networkPolicy.postgresCIDR` | The IPv4 CIDR of your Postgres Service. Recover via `kubectl get endpoints <pg-svc> -n <ns> -o jsonpath='{.subsets[].addresses[].ip}'` and widen to the controlling subnet. |
    | `networkPolicy.vaultCIDR` | Same, for Vault. |
    | `networkPolicy.keycloakCIDR` | Same, for Keycloak. |

3. **Provision** the Kubernetes Secrets named in the values file. The
   chart references `postgres.credentialsSecret` by name; the Pod will
   not start until that Secret exists with a `url` key holding the
   `DATABASE_URL`. The recommended sync mechanism is **External Secrets
   Operator (ESO)** — see [§ ESO sync patterns](#eso-sync-patterns) below.

4. **Install** (after ESO has populated the target Secret — see install
   flow below):

    ```bash
    helm upgrade --install meho ./deploy/charts/meho/ \
      --namespace meho --create-namespace \
      -f values-rdc.yaml
    ```

5. **Verify** the release came up clean:

    ```bash
    kubectl -n meho rollout status deploy/meho
    kubectl -n meho get pods -l app.kubernetes.io/name=meho
    kubectl -n meho logs deploy/meho --tail=50
    ```

## ESO sync patterns

The MEHO chart references operator-provisioned Kubernetes Secrets *by
name* — it does not embed credentials in `values.yaml`, and it does not
ship a `Secret` template that consumers `--set` values into. Instead, the
chart expects the consumer to sync secrets from a backing store (Vault,
1Password Connect, AWS Secrets Manager, …) into Kubernetes via
[**External Secrets Operator (ESO)**](https://external-secrets.io/).

The RDC lab uses ESO with **HashiCorp Vault** as the backend
([provider docs](https://external-secrets.io/latest/provider/hashicorp-vault/)).
Two resources combine to materialise a Secret the chart can consume:

1. **`ClusterSecretStore`** — cluster-scoped pointer at the upstream
   store. Carries the Vault address, auth method, and (for JWT/Kubernetes
   auth) the SA token. Created once, by the platform team. **Lives
   outside this chart by design**: it outlives any given release and
   carries the cluster's Vault credentials, so it belongs in the
   consumer's GitOps repo, not the application chart.

    ```yaml
    apiVersion: external-secrets.io/v1beta1
    kind: ClusterSecretStore
    metadata:
      name: vault-store
    spec:
      provider:
        vault:
          server: https://vault.evba.lab
          path: secret          # KV v2 mount
          version: v2
          auth:
            kubernetes:
              mountPath: kubernetes
              role: external-secrets
              serviceAccountRef:
                name: external-secrets
                namespace: external-secrets
    ```

2. **`ExternalSecret`** — namespaced resource that pulls one or more
   keys out of the upstream store and projects them into a Kubernetes
   Secret in the same namespace. **Two options for who owns this**:

    - **Default (consumer-managed):** the consumer's GitOps repo applies
      ExternalSecret manifests alongside (or before) the chart. The
      chart references the resulting Secret by name. This is the RDC
      lab's convention and stays out of the chart entirely.

      ```yaml
      apiVersion: external-secrets.io/v1beta1
      kind: ExternalSecret
      metadata:
        name: meho-postgres
        namespace: meho
      spec:
        refreshInterval: 1h
        secretStoreRef:
          name: vault-store
          kind: ClusterSecretStore
        target:
          # MUST match `.Values.postgres.credentialsSecret` in the chart.
          name: meho-postgres
          creationPolicy: Owner
        data:
          - secretKey: url       # the Secret's data key — MUST be `url`
            remoteRef:
              key: secret/meho/postgres   # Vault KV path
              property: url               # the JSON property holding DATABASE_URL
      ```

      The chart's Deployment env reads `DATABASE_URL` from this Secret's
      `url` key — see [`deploy/charts/meho/templates/deployment.yaml`](../charts/meho/templates/deployment.yaml).

    - **Opt-in (chart-managed):** set `eso.enabled: true` in your
      values file and the chart renders the ExternalSecret itself. Use
      this when you'd rather keep one source of truth (`helm template`
      shows everything) and you're not running a sibling GitOps
      operator that already owns the ExternalSecrets.

      ```yaml
      eso:
        enabled: true
        secretStore:
          name: vault-store
          kind: ClusterSecretStore
        refreshInterval: 1h
        postgres:
          remoteKey: secret/meho/postgres
          remoteProperty: url
        keycloak:
          enabled: false        # v0.1 does not yet consume this in env
      ```

      The schema enforces `secretStore.name` + `secretStore.kind` when
      `eso.enabled: true`, so a misconfigured opt-in fails at install
      time. With `eso.enabled: false` (the default) the chart never
      renders ExternalSecret resources — verify with
      `helm template ... | grep -c ExternalSecret` → `0`.

### Vault paths the chart expects

| Vault KV path | What's stored | Consumed by |
| --- | --- | --- |
| `secret/meho/postgres` (property `url`) | The full `DATABASE_URL`: `postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>` | The Deployment env `DATABASE_URL` via `postgres.credentialsSecret` |
| `secret/meho/keycloak/client_secret` (property `client_secret`) | The Keycloak OAuth client secret backing `keycloak.audience` | v0.2 federation wiring (rendered optionally today for end-to-end sync verification) |
| `secret/meho/agent` (property `api_key`) | The Anthropic API key the G11.1 agent LLM loop authenticates with | The Deployment env `ANTHROPIC_API_KEY` via `agent.secretName` (resolved through `meho.agentSecretName`) when `agent.enabled: true` |
| `secret/meho/keycloak/admin_client_secret` (property `client_secret`) | The G11.2 Keycloak Admin client secret gating agent-principal registration | The Deployment env `KEYCLOAK_ADMIN_CLIENT_SECRET` via `keycloakAdmin.clientSecret.secretName` (resolved through `meho.keycloakAdminSecretName`) when `keycloakAdmin.enabled: true` |

The `secret/meho` base is configurable via `vault.paths.kv` — adjust the
KV paths above accordingly if you remount Vault elsewhere.

## Agent-runtime credential wiring (G11.1 + G11.2)

The chart wires two G11 credential groups as first-class chart values
so an operator enables agent-runtime without hand-rolling Secrets +
`extraEnv` `valueFrom` (G0.18-T10 #1363):

| Group | Chart toggle | Env vars wired | Secret handling |
| --- | --- | --- | --- |
| **G11.1 agent LLM loop** | `agent.enabled: true` | `ANTHROPIC_API_KEY` | `secretKeyRef` only — never plaintext. |
| **G11.2 agent-principal registration** | `keycloakAdmin.enabled: true` | `KEYCLOAK_ADMIN_URL` + `KEYCLOAK_ADMIN_CLIENT_ID` (plain env) + `KEYCLOAK_ADMIN_CLIENT_SECRET` (`secretKeyRef`) | URL + clientId are plain config; only the client secret is `secretKeyRef`. |

Both default to `enabled: false` — the chart renders no env wiring and
the backplane's fail-closed surface keeps both features inoperative
(`/api/v1/agent-runs` 503 / "no credentials"; `POST /api/v1/agent-principals`
`503 keycloak_admin_not_configured`) — same posture as a chart that
doesn't ship these blocks at all.

The Secret-name resolution allows two equally first-class paths:

- **Bring-your-own Secret.** Set `agent.secretName` /
  `keycloakAdmin.clientSecret.secretName` to a Kubernetes Secret your
  consumer GitOps repo provisions. Leave `eso.agent.enabled` /
  `eso.keycloakAdmin.enabled` at `false`.

- **ESO-rendered.** Flip `eso.agent.enabled` / `eso.keycloakAdmin.enabled`
  to `true` and provide `eso.secretStore.name`. The chart renders an
  ExternalSecret targeted at `<release>-agent` / `<release>-keycloak-admin`
  (keys `api_key` / `client_secret`). Leave the `secretName` fields
  empty — the `meho.agentSecretName` / `meho.keycloakAdminSecretName`
  helpers automatically resolve the ESO target name.

A configuration that flips `enabled: true` but leaves both Secret-name
paths empty fails at `helm template` / `helm install` time with an
actionable message (the chart's `fail` gates in
`templates/_helpers.tpl`).

The CI `helm test <release>` Pod
(`templates/tests/test-agent-runtime-config.yaml`) asserts the four
env vars resolve when both opt-ins are enabled. The
`.github/workflows/chart.yml` PR build gate additionally renders the
chart with both opt-ins on and greps for the `secretKeyRef` shape so
a regression flipping `ANTHROPIC_API_KEY` to plaintext is rejected
before merge.

## Internal-CA trust bundle (`extraVolumes` / `extraEnv`)

The backplane connects to **Vault**, **Keycloak**, and **PostgreSQL**
over TLS. Any lab whose Vault / Keycloak / PG ingress is signed by an
**internal CA** (the realistic posture for every regulated lab) needs
to inject that CA into the backplane Pod, otherwise the readiness probes
fail with `SSLError` / `ConnectError` and `/ready` returns 503 even
though `/healthz` is green. The `--atomic --wait` install path rolls
back the release. This is Issue [#209](https://github.com/evoila/meho/issues/209).

The chart exposes three top-level knobs — `extraVolumes`,
`extraVolumeMounts`, `extraEnv` — that flow into both the backplane
Deployment AND the migration Job (Postgres' internal-CA-signed TLS is
the typical reason the migration Job needs the bundle too).

### Recommended pattern: trust-manager + `SSL_CERT_FILE`

In v0.1 the recommended path is [trust-manager](https://cert-manager.io/docs/trust/trust-manager/)
(jetstack/trust-manager). The lab admin creates one `Bundle` resource
cluster-wide; trust-manager distributes a `ConfigMap` containing
`ca.crt` into every namespace flagged via
`trust.cert-manager.io/include` (or a NamespaceSelector). The chart
mounts that ConfigMap and points Python's ssl module at it via
`SSL_CERT_FILE`:

```yaml
extraVolumes:
  - name: trust-bundle
    configMap:
      name: internal-ca-bundle       # the ConfigMap trust-manager renders
      optional: false                # fail-loud if it's missing

extraVolumeMounts:
  - name: trust-bundle
    mountPath: /etc/ssl/extra-certs
    readOnly: true

extraEnv:
  - name: SSL_CERT_FILE
    value: /etc/ssl/extra-certs/ca.crt
```

`SSL_CERT_FILE` is CPython's standard env var
([`ssl.get_default_verify_paths`](https://docs.python.org/3/library/ssl.html#ssl.get_default_verify_paths))
— httpx, hvac, asyncpg, and SQLAlchemy all honour it without code
changes. Mounting read-only keeps the discipline (the bundle is owned
by trust-manager, not by anything inside the Pod).

### Alternatives if trust-manager isn't deployed

- **Direct ConfigMap.** Skip trust-manager; create the ConfigMap by hand
  in the `meho` namespace. `extraVolumes[0].configMap.name` points at
  it. Rotation is now the operator's job.
- **Secret instead of ConfigMap.** Same `extraVolumes` shape with
  `secret:` instead of `configMap:`. Useful if the bundle itself is
  sensitive (uncommon — CA certificates are public-by-design).
- **Pre-baked image.** Add the CA to the runtime image's
  `/etc/ssl/certs/`. Rejected for v0.1 because it puts environment-
  specific state into a public OSS artefact; the chart-side approach
  keeps the image generic.

### Verification

After `helm install`:

```bash
# The mount is present
kubectl exec -n meho deployment/meho -- ls -l /etc/ssl/extra-certs/ca.crt
# SSL_CERT_FILE resolves to it
kubectl exec -n meho deployment/meho -- printenv SSL_CERT_FILE
# Python sees it
kubectl exec -n meho deployment/meho -- python -c "import ssl; print(ssl.get_default_verify_paths().cafile)"
# /ready turns green for vault + keycloak
kubectl exec -n meho deployment/meho -- wget -qO- http://localhost:8000/ready | jq '.checks'
```

If `/ready` still reports `ssl_error` after the mount lands, check the
**migration Job's** Pod logs — that Job uses the same bundle. A
common drift cause: a typo'd ConfigMap name (the mount succeeds but
the file is empty / wrong).

## Connector dispatch against self-signed / internal-CA targets

The trust-bundle section above covers the backplane's **own**
infrastructure dependencies — Vault, Keycloak, PostgreSQL. The same TLS
problem reappears one layer out, on the **connector dispatch** path: the
governed targets a `meho operation call` reaches (a vCenter, an NSX
manager, a vRLI / VCF Operations for Logs appliance, a Harbor registry)
routinely present **self-signed or internal-CA** certificates —
freshly-deployed appliances before cert replacement, nested labs, and
anything Fleet-managed with its own locker CA. The backplane's HTTP
connector verifies every dispatch against the same global trust store
(`SSL_CERT_FILE` / the chart trust-bundle — see above), so a target
whose certificate chain isn't in that bundle fails dispatch with a TLS
verification error.

When that happens, the dispatch result is the structured
`connector_tls_verify_failed` error (Initiative
[#1774](https://github.com/evoila/meho/issues/1774) T3,
[#1782](https://github.com/evoila/meho/issues/1782)). It names the
**host** and **both** remediations below, in preference order, so an
operator who hits `[SSL: CERTIFICATE_VERIFY_FAILED]` gets pointed at the
fix instead of an opaque `connector_error: ConnectError`. The two
remediations:

### 1. Secure path (preferred): trust the appliance's CA

Make the backplane **trust the target's certificate chain** — exactly
the trust-bundle mechanism documented above, extended to cover the
appliance's issuing CA. Add the internal-CA / self-signed-appliance cert
to the CA bundle `SSL_CERT_FILE` points at (or inject it into the chart
trust-bundle ConfigMap), and verification succeeds against the real
chain without weakening it. This is the **recommended** fix: TLS
verification stays on, so the dispatch channel — which forwards the
target's Vault-resolved credential — stays authenticated against a
man-in-the-middle.

> **Scope the bundle correctly (the [#572](https://github.com/evoila/meho/issues/572)
> footgun).** A naïve "mount only the internal CA at `SSL_CERT_FILE`"
> **clobbers** the public root store: `SSL_CERT_FILE` *replaces* the
> default CA set rather than appending to it, so a bundle containing
> only the internal CA breaks every public-CA TLS connection the Pod
> also makes (the HuggingFace model download in #572 was the first
> casualty). The bundle you mount must be the **union** of the public
> roots and your internal CA — the [trust-manager](https://cert-manager.io/docs/trust/trust-manager/)
> `Bundle` resource is built for exactly this (it can include
> `useDefaultCAs: true` alongside your CA), which is why it is the
> recommended source for the ConfigMap above.

### 2. Last resort (per-target, never global): `verify_tls=false`

When you genuinely cannot trust the chain yet — a Fleet-managed
appliance whose cert is wiped on the next lab rebuild, a nested lab with
no stable CA — meho exposes a **per-target** TLS-verification opt-out:
the `verify_tls` flag on a target
([#1774](https://github.com/evoila/meho/issues/1774) T1,
[#1780](https://github.com/evoila/meho/issues/1780)). It is
**default-secure** (`NOT NULL DEFAULT true`): a target verifies its
certificate chain unless an operator explicitly turns verification off
for **that target alone**.

> **MITM / credential-exposure caveat (load-bearing — read before
> setting this).** A target with `verify_tls=false` still forwards its
> **Vault-resolved credential** over the now-unverified channel on every
> dispatch. With verification off, meho can no longer tell the real
> appliance apart from a man-in-the-middle presenting any certificate —
> so the credential is exposed to interception. Use `verify_tls=false`
> **only** against a **trusted-network** appliance you cannot yet pin a
> CA for, and treat it as temporary until the secure path or the CA-pin
> follow-up (below) lands.

This deliberately mirrors the escape hatches operators already know,
and inherits their discipline — **never global, per-target, loud,
audited**:

- **`govc -k` / `GOVC_INSECURE`** ([govmomi](https://github.com/vmware/govmomi/tree/main/govc#usage))
  — vSphere CLI per-invocation insecure flag, not a daemon-wide setting.
- **`kubectl` per-cluster `insecure-skip-tls-verify`**
  ([kubeconfig reference](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/))
  — set on **one** cluster entry in a kubeconfig, never as a global
  client default.

`verify_tls=false` is the meho equivalent: scoped to a single target,
written through a `tenant_admin`-only API, and **audited** — flipping it
off writes a durable `audit_log` row (`tls_verification_disabled`, with
the before/after values) and emits a WARN log line, so the opt-out is
queryable after the fact rather than silent. (This closes the prior gap
where a target PATCH wrote an empty audit payload.)

> **Behaviour note.** This is fully shipped end to end. The `verify_tls`
> column, its API surface, and the audit/WARN trail
> ([#1780](https://github.com/evoila/meho/issues/1780)) and the **dispatch
> wiring** that makes the pooled HTTP connector actually honour the flag
> ([#1781](https://github.com/evoila/meho/issues/1781), Initiative T2) are
> both merged. Setting `verify_tls=false` now genuinely reaches a
> self-signed / internal-CA endpoint: the connector builds its
> `httpx.AsyncClient` with an insecure `SSLContext` (`check_hostname` off,
> `CERT_NONE`) for that target only, while a `verify_tls=true` target is
> built with no `verify=` argument so the global `SSL_CERT_FILE` /
> trust-bundle path is byte-identical to before. The opt-out is keyed into
> the per-target client pool (`(tenant_id, id, verify_tls)`), so a PATCH
> that flips the flag is not served a stale verifying/non-verifying client.

### The secure supersession (preferred per-target fix): CA-pin ([#1784](https://github.com/evoila/meho/issues/1784))

`verify_tls=false` trades away MITM protection. The follow-up that gives
back the security **without** requiring a CA in the global bundle is the
**per-target CA-pin** (Initiative
[#1774](https://github.com/evoila/meho/issues/1774) T5,
[#1784](https://github.com/evoila/meho/issues/1784)): pin the appliance's
expected CA / cert PEM on the target itself via the `tls_ca_pin` field.
At dispatch the connector builds
`ssl.create_default_context()` then
`ctx.load_verify_locations(cadata=<your PEM>)`, which **keeps**
`CERT_REQUIRED` **and** `check_hostname` **on** — so the certificate
chain and the hostname are both still verified, now additionally trusting
the pinned CA. This is the same shape as the govc **thumbprint** flow
(`govc -thumbprint`): trust *this specific* appliance's self-signed /
internal-CA certificate without trusting the wider world and without
disabling verification.

CA-pin is the **preferred** fix for a self-signed / internal-CA appliance
you can't add to the global bundle, because the channel stays
authenticated against a man-in-the-middle. `verify_tls=false` is the
genuine last resort it is framed as here.

> **Precedence + mutual exclusion.** A target's TLS trust resolves in
> this order: **(1)** `tls_ca_pin` set → trust the pinned CA, verification
> stays on (secure); **(2)** else `verify_tls=false` → verification off
> (insecure last resort); **(3)** else the global `SSL_CERT_FILE` bundle.
> Because the pin is the secure way to reach a self-signed endpoint,
> setting **both** `tls_ca_pin` and `verify_tls=false` on the same target
> is rejected with a `422` at create/update time — they are mutually
> exclusive. The pin is added *on top of* the global bundle, so a pinned
> target still trusts public CAs too.

```bash
# Create a target that trusts a specific appliance CA — verification
# stays ON (chain + hostname), against the pinned CA. PEM goes in the
# tls_ca_pin field; --rawfile keeps the multi-line cert intact as a JSON
# string.
jq -n --rawfile pin /path/to/appliance-ca.pem \
  '{name:"vcf-logs-lab", product:"vmware-rest", host:"vrli.nested.lab",
    auth_model:"shared_service_account", tls_ca_pin:$pin}' \
| curl -sf -X POST https://meho.example.com/api/v1/targets \
    -H "Authorization: Bearer $(meho status --print-token)" \
    -H 'Content-Type: application/json' -d @-

# Rotate the pin (e.g. the appliance cert was re-issued): PATCH the new
# PEM. The pooled client is rebuilt — the pin digest is part of the
# client-pool cache key, so the old client is never reused.
jq -n --rawfile pin /path/to/new-appliance-ca.pem '{tls_ca_pin:$pin}' \
| curl -sf -X PATCH https://meho.example.com/api/v1/targets/vcf-logs-lab \
    -H "Authorization: Bearer $(meho status --print-token)" \
    -H 'Content-Type: application/json' -d @-

# Clear the pin (the CA is now in the global bundle, say): send null.
curl -sf -X PATCH https://meho.example.com/api/v1/targets/vcf-logs-lab \
  -H "Authorization: Bearer $(meho status --print-token)" \
  -H 'Content-Type: application/json' \
  -d '{"tls_ca_pin": null}'
```

A malformed PEM is rejected with a `422` at create/update time (the field
is validated against the stdlib SSL loader before it is persisted), so a
bad pin surfaces immediately rather than as an opaque dispatch failure.
Setting / rotating / clearing the pin writes a durable `audit_log` row
(`tls_ca_pinned` + a digest of the before/after PEM — never the PEM body)
and a WARN log line, exactly like the `verify_tls` toggle.

### Setting `verify_tls` on a target

`verify_tls` is a first-class field on the targets API
([`/api/v1/targets`](../../backend/src/meho_backplane/api/v1/targets.py)),
settable on both create and update (the route is `tenant_admin`-only):

```bash
# Create a target with verification off (self-signed lab appliance).
curl -sf -X POST https://meho.example.com/api/v1/targets \
  -H "Authorization: Bearer $(meho status --print-token)" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "vcf-logs-lab",
        "product": "vmware-rest",
        "host": "vrli.nested.lab",
        "auth_model": "shared_service_account",
        "verify_tls": false
      }'

# Flip an existing target back to secure once its CA is in the bundle.
curl -sf -X PATCH https://meho.example.com/api/v1/targets/vcf-logs-lab \
  -H "Authorization: Bearer $(meho status --print-token)" \
  -H 'Content-Type: application/json' \
  -d '{"verify_tls": true}'
```

Omitting `verify_tls` on create lands the secure default (`true`). On
PATCH, the field follows standard partial-update semantics — a body that
does not mention `verify_tls` leaves it (and the TLS audit trail)
untouched; only an explicit `{"verify_tls": false}` / `{"verify_tls": true}`
flips the column and writes the audit row.

#### Setting `verify_tls` / `tls_ca_pin` from a `targets.yaml`

`meho targets import` **does** set `verify_tls` and `tls_ca_pin` as
first-class fields (#1793). The bulk-import path
([`cli/internal/cmd/targets/import.go`](../../cli/internal/cmd/targets/import.go))
maps a fixed set of known top-level YAML keys onto the create/update
body and spills every other key into the `extras` JSONB column; both
TLS-trust keys are in that known-key set, so a `verify_tls:` or
`tls_ca_pin:` line in the descriptor reaches the typed column (it does
**not** spill into `extras`). This covers both `meho targets import`
(create) and `meho targets import --update` (PATCH). There is still no
`meho targets create` / `meho targets update` verb — `import` is the
CLI's only write path (direct write verbs are out of scope for v0.2;
see [`cli/internal/cmd/targets/targets.go`](../../cli/internal/cmd/targets/targets.go))
— so the REST `POST` / `PATCH /api/v1/targets` calls above and the
descriptor below are the two supported ways to set these.

```yaml
# targets.yaml — imported with `meho targets import targets.yaml`
# (add --update to PATCH targets that already exist).
targets:
  # Secure path: pin the appliance CA. Verification stays ON (chain +
  # hostname) against the pinned CA. Prefer this over verify_tls: false.
  - name: vcf-logs-lab
    product: vmware-rest
    host: vrli.nested.lab
    auth_model: shared_service_account
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      MIIB...appliance CA PEM...
      -----END CERTIFICATE-----

  # Last resort: verification OFF for this one target. Read the MITM /
  # credential-exposure caveat above before setting this.
  - name: legacy-appliance
    product: vmware-rest
    host: appliance.nested.lab
    auth_model: shared_service_account
    verify_tls: false
```

> **Security framing carries over to import.** `verify_tls: false` is
> the same insecure last resort the section above describes — it
> disables chain + hostname verification for that target, so a forwarded
> credential is exposed to a man-in-the-middle. Prefer `tls_ca_pin`,
> which keeps verification on against the pinned CA. The server enforces
> the precedence and **mutual exclusion** noted above: a descriptor entry
> that sets **both** `verify_tls: false` and `tls_ca_pin` on the same
> target is rejected with a `422` (they are mutually exclusive), and a
> malformed PEM is likewise rejected with a `422` — the import surfaces
> the server's error rather than silently picking one or applying a bad
> pin. Omitting both keys lands the secure default (`verify_tls: true`,
> no pin), and on `--update` an omitted key follows the same sparse-PATCH
> semantics as the REST `PATCH` (the column and its TLS audit trail are
> left untouched).

### Coverage gap: two out-of-pool connectors do not honour `verify_tls`

`verify_tls` governs the **pooled HTTP connector** dispatch path only.
Two dispatch clients live **outside** that pool and are **not** affected
by the flag — named here so adopters don't assume coverage:

- **The Kubernetes reachability probe**
  ([`connectors/kubernetes/connector.py`](../../backend/src/meho_backplane/connectors/kubernetes/connector.py))
  already hardcodes `verify=False` for its kubeconfig-free reachability
  check, so `verify_tls` has nothing to toggle there.
- **The GitHub App token-exchange**
  ([`connectors/github/session.py`](../../backend/src/meho_backplane/connectors/github/session.py))
  authenticates against public-cert `api.github.com`, which is in the
  public root store — there is no self-signed case for it to handle.

Both are moot for the self-signed-appliance scenario this section
addresses.

## Auth onramp recipe (CLI + MCP)

> First-login auth-onramp for both `meho login` (CLI device-code) and
> the MCP-client surface (`/mcp` over RFC 9728 + OAuth 2.1 + PKCE).
> The 2026-05-21 RDC dogfood walked the realm from scratch and paid
> ~2.5 hours hitting four sequential walls — none of them documented
> in one place at the time; a follow-up 2026-05-22 dogfood added a
> fifth (W7, `offline_access` for Claude Code's MCP refresh-token
> request). This section is the consolidated 5-step recipe +
> five-wall symptom→cause→fix matrix that closes that gap.
> Companion to [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
> (MCP-client-side configuration) and
> [`docs/acceptance/install.md`](../../docs/acceptance/install.md)
> (cold-deploy acceptance contract).
>
> **Scope.** Both `meho login` and most MCP clients (Claude Desktop /
> Claude.ai, MCP Inspector, Claude Code's HTTP MCP, Cursor) need
> **public** OAuth clients pre-provisioned in the Keycloak realm
> before they can authenticate. MEHO is RFC-correct (the chassis
> emits the RFC 9728 metadata document, and the `/mcp` 401 carries
> `WWW-Authenticate: Bearer resource_metadata=…`) — but the implicit
> "follow the metadata trail and run dynamic client registration"
> path is closed by Keycloak's default Trusted Hosts policy on any
> realistic prod realm, and a public client can't appear by magic.
> The deployer creates two public clients (one for the CLI, one for
> MCP) with the right mappers + scopes; the consolidated automation
> verb that does this in one idempotent step ships under
> [#791](https://github.com/evoila/meho/issues/791) (G0.9.1-T11).
> This recipe is the manual path the verb encodes.

### 5-step realm recipe

The recipe assumes the confidential resource-server client
`meho-backplane` already exists (it's how the backplane validates
inbound tokens; created at install time alongside the realm). Steps
1–5 add the **public** clients + the user that approve and consume
those tokens.

#### Step 1 — Install the deployment's TLS CA on the operator workstation

`meho login` (Go) and most MCP clients verify TLS against the
operator's **OS trust store**, not the `SSL_CERT_FILE` env var.
On Linux: `update-ca-certificates` after dropping the CA in
`/usr/local/share/ca-certificates/`. On macOS: import to the system
keychain via `Keychain Access` or `security add-trusted-cert -d -r
trustRoot -k /Library/Keychains/System.keychain <ca>.pem`; Go on
macOS reads the system keychain via the Security framework and
**ignores `SSL_CERT_FILE`**, so the env-var trick that works for
Python on the backplane side doesn't carry over to the workstation
CLI. On Windows: import to `Trusted Root Certification Authorities`
via `certutil -addstore -f Root <ca>.pem`. Verify with
`curl -sf https://<backplane-host>/healthz` from a fresh shell.

If this step is skipped, `meho login` fails at the discovery probe
with an `x509: certificate signed by unknown authority` error and
the breadcrumb points at `--client-id`/`--issuer` overrides — which
doesn't fix TLS. The override surfaces a separate failure mode but
isn't the right recovery for an untrusted CA.

#### Step 2 — Create the public `meho-cli` device-code client

In the Keycloak realm that hosts `meho-backplane`, create a new
**public** client with these settings:

| Setting | Value | Why |
| --- | --- | --- |
| Client ID | `meho-cli` (suggested) | Matches the default in `values-rdc-example.yaml`. Any short identifier works — set `config.keycloakCliClientId` (`KEYCLOAK_CLI_CLIENT_ID`) to whatever you choose. |
| Client authentication | **Off** (public client) | The device grant cannot be completed by a confidential client; the CLI has nowhere to store a secret. Confidential client + device-grant → `401 unauthorized_client` from Keycloak's device endpoint (Wall #1). |
| Authentication flow → Standard flow | Off | The CLI doesn't run the authorization-code grant. |
| Authentication flow → Direct access grants | Off | Resource-owner password is explicitly out of scope. |
| Authentication flow → Implicit flow | Off | Deprecated by OAuth 2.1. |
| Authentication flow → Service accounts roles | Off | Public clients can't hold credentials. |
| Authentication flow → **OAuth 2.0 Device Authorization Grant** | **On** | Required for `meho login` ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)). |
| Valid redirect URIs | (none) | Device flow doesn't redirect. |

#### Step 3 — Clone all 5 protocol mappers from `meho-backplane` onto `meho-cli`

Tokens minted by `meho-cli` must carry the same claim shape the
backplane validates — otherwise the token decodes cleanly but is
rejected with `invalid_token` (Wall #2). The five mappers (verbatim
names from the dogfood reference, copy hardcoded values from the
`meho-backplane` client in the same realm):

| Mapper name | Type | Output claim | Notes |
| --- | --- | --- | --- |
| `audience-meho-backplane` | `oidc-audience-mapper` | `aud` adds `meho-backplane` | Without this, tokens carry `aud: meho-cli` and the backplane rejects them with `audience_not_configured` / `invalid_audience`. |
| `meho-mcp-audience` | `oidc-audience-mapper` | `aud` adds `<backplane-url>/mcp` | Required so a token minted via the CLI can also drive `/mcp` calls. Use **Included Custom Audience** (no client-mapper UI option exists for an arbitrary URI). Paste the URI **without** a trailing slash — MEHO normalises `MCP_RESOURCE_URI` server-side and the audience claim must match the no-trailing-slash form. |
| `tenant-id` | `oidc-hardcoded-claim` | `tenant_id` | Hardcoded to the tenant UUID the operator belongs to. Without this the backplane rejects with `missing_tenant_claim` (Wall #2). |
| `tenant-role` | `oidc-hardcoded-claim` | `tenant_role` | Hardcoded to one of `read_only` / `operator` / `tenant_admin`. Without this the backplane rejects with `missing_tenant_role_claim`. |
| `groups-claim` | `oidc-group-membership-mapper` | `groups` | Drives group-based RBAC (`meho-admins`, etc.). Without this group-gated tools report empty results rather than failing — a softer failure than the above, but still misleading. |

Hardcoded-claim mappers are intentional: in the dogfood lab the
realm doesn't model tenants/roles as group attributes, so hardcoded
values are the simplest path. A realm that already encodes
tenant/role on the user (group attribute, custom user-attribute,
identity-provider mapping) can swap these for `oidc-usermodel-*`
mappers — keep the **claim names** identical (`tenant_id`,
`tenant_role`) because that's what the backplane validates against.

Validator-side errors at the decode stage are made specific by
[#797](https://github.com/evoila/meho/issues/797) (G0.9.1-T12) and
[#1131](https://github.com/evoila/meho/issues/1131) (G0.13-T1):
`invalid_audience` / `missing_sub` / `token_expired` /
`signature_verification_failed` / `invalid_issuer` /
`token_not_yet_valid` / `malformed_jws` are distinguished in the
401 `detail` body instead of all collapsing to `invalid_token`. The
residual `invalid_token` is reserved for failures that aren't
`DecodeError` (`alg: none` via `UnsupportedAlgorithmError`, future
authlib `JoseError` subclasses, post-refresh kid miss).

#### Step 3a — Docs-corpus entitlement claim (`meho-docs:*`) is per-audience

> **Only relevant if you run the `meho-docs` add-on** (the federated
> vendor-document corpus behind `search_docs` / `ask_docs` /
> `/ui/corpus`). Skip this sub-step otherwise.

The docs-corpus surfaces gate on a **capability claim**, not a role or a
group: an operator may search collection `<key>` only when its token's
`capabilities` claim contains `meho-docs:<key>` (e.g. `meho-docs:vmware`).
The backplane reads that array from the claim named by
`JWT_CAPABILITIES_CLAIM_NAME` (default `capabilities`), and **all three
surfaces use the same `(tenant_id, capabilities)` contract** — so they
either all see the entitlement or none do, *for a given token*.

The catch is the word "token". MEHO validates each surface's token for a
**different audience**:

| Surface | Audience the token is validated for |
| --- | --- |
| REST `POST /api/v1/search_docs` | `KEYCLOAK_AUDIENCE` (`meho-backplane`) |
| `/ui/corpus` (the operator web console BFF) | `KEYCLOAK_AUDIENCE` (`meho-backplane`) — same as REST |
| MCP `search_docs` / `ask_docs` | `MCP_RESOURCE_URI` (`<backplane-url>/mcp`) |

If your realm mints **per-audience tokens with different claim sets** (the
common case once the `meho-mcp-audience` mapper from Step 3 is in play —
the MCP token and the REST/UI token are issued for different audiences),
the `meho-docs:*` capability can land on the MCP token but **not** the
REST/UI token. The symptom is the [#1802](https://github.com/evoila/meho/issues/1802)
dogfood report: `search_docs` over MCP returns cited chunks, while the same
collection over REST 403s (`{"error":"not_entitled", ...}`) and `/ui/corpus`
shows "Not entitled to the attached docs corpus". That is a **claim-mapper
gap, not a backplane bug** — the entitlement check is identical across
surfaces; the claim simply wasn't issued on every audience.

**Fix: emit the `capabilities` claim on every audience the operator uses.**
Add a `capabilities` mapper to the **realm-default client scope** every
client inherits (the same `meho-backplane-claims` default scope the CIMD
section recommends, or directly on each public client), so the claim rides
both the `meho-backplane` and `<backplane-url>/mcp` audiences:

| Mapper name | Type | Output claim | Notes |
| --- | --- | --- | --- |
| `meho-docs-capabilities` | `oidc-hardcoded-claim` (lab) or `oidc-usermodel-attribute-mapper` (realm with a `capabilities` user attribute) | `capabilities` (must match `JWT_CAPABILITIES_CLAIM_NAME`) | Claim **JSON type: `JSON`** and **Multivalued: On** so it serialises as a JSON array (`["meho-docs","meho-docs:vmware"]`), not a comma string. Include the base `meho-docs` add-on key **and** one `meho-docs:<collection_key>` per collection the identity may search. Add it to a **default** client scope (not per-client) so a CIMD-resolved or newly-added client inherits it too. |

For the hardcoded-claim (lab) form, set **Claim value** to the literal JSON
array, e.g. `["meho-docs", "meho-docs:vmware"]`.

> **Per-collection, not all-or-nothing.** `meho-docs` alone makes the
> add-on *visible* (the tool appears, the page loads); each
> `meho-docs:<collection_key>` is what authorises searching *that*
> collection. Granting `meho-docs` without any `meho-docs:<key>` is exactly
> the empty-but-diagnosable state `/ui/corpus` now names explicitly.

**Verify the claim is present on the audience that's failing.** Decode the
token each surface validates and confirm both the audience and the
capability. Using `meho status --print-token` (the CLI/REST audience):

```bash
# 1. The capabilities claim carries the per-collection key on the REST/UI
#    audience token. (jwt-cli `jwt decode`, or jq over the base64 payload.)
meho status --print-token \
  | jwt decode --json - \
  | jq '{aud: .payload.aud, capabilities: .payload.capabilities}'
# Expect aud to include "meho-backplane" AND capabilities to include
# "meho-docs:vmware". If capabilities is missing/empty here but the MCP
# token has it, that is the per-audience gap — add the mapper above.

# 2. Probe the REST surface directly: a 403 names the missing claim + the
#    identity it checked, so you can grant exactly that capability.
curl -s -X POST https://meho.example.com/api/v1/search_docs \
  -H "Authorization: Bearer $(meho status --print-token)" \
  -H 'Content-Type: application/json' \
  -d '{"query":"x","collection":"vmware"}' | jq .detail
# {
#   "error": "not_entitled",
#   "collection": "vmware",
#   "required_capability": "meho-docs:vmware",
#   "operator_sub": "<sub>",
#   "tenant_id": "<uuid>",
#   "message": "identity '<sub>' (tenant <uuid>) is not entitled to doc
#               collection 'vmware': missing capability 'meho-docs:vmware'"
# }
```

For the MCP-audience token (minted via the `meho-mcp-client` browser flow),
decode that token the same way and confirm `aud` includes
`<backplane-url>/mcp` **and** the same `capabilities` array — both audiences
must carry it for all three surfaces to agree.

#### Step 4 — Explicitly assign the 4 default client scopes

| Scope | Why | What breaks if it's missing |
| --- | --- | --- |
| `basic` | Carries the **Subject (sub) protocol mapper**. Keycloak 25 moved the `sub` claim out of the hardcoded token-generation path and into a protocol mapper inside this scope ([Keycloak 25 release notes](https://www.keycloak.org/2024/06/keycloak-2500-released) — the change persists in Keycloak 26+). RFC 9068 §2.2.1 makes `sub` REQUIRED on JWT access tokens, so a token without it is correctly rejected — the diagnostic is the opaque part. | Wall #3: every backplane call returns 401 `invalid_token` with no hint that `sub` is missing. This is the deepest wall — every other claim is present, the token *looks* well-formed, and the breakage moves with the realm regardless of how the client was created. |
| `roles` | Carries realm-roles + client-roles into the token. | Group-gated tools may report empty / unauthorised results. |
| `web-origins` | Allowed CORS origins for browser-driven flows. | Browser-driven MCP clients (Claude.ai Custom Connector) fail CORS pre-flight. |
| `acr` | Authentication Context Class Reference — drives step-up auth. | Step-up flows misbehave; not load-bearing for first login but cheap to ship. |

> **The `basic`/`sub` gotcha (load-bearing).** Clients **created via
> the Keycloak admin REST API do not auto-inherit the realm's
> default-default client scopes** the way the admin-console UI's
> "Create" button does. The console populates `defaultClientScopes`
> on the new client to the realm's "Default" set; `kcadm.sh` /
> direct admin-API `POST /clients` does not, unless the request body
> explicitly includes a `defaultClientScopes` array. Re-using a
> realm-default scope **name** in `optionalClientScopes` doesn't
> back-fill it as default either. The recovery is to set
> `defaultClientScopes: ["basic","roles","web-origins","acr"]`
> explicitly in the admin-API request body, or to re-add each scope
> via the admin console afterwards. The consumer's reference script
> at [`scripts/keycloak-bootstrap-meho-cli.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/670)
> sets all four explicitly to be safe.

#### Step 5 — Provision a user in `meho-admins` with a password

The realm ships with zero human users — prior lab work rode
`client_credentials` and never needed one. Device-code, by RFC 8628
§3.4, requires a real user to approve the verification URL.

Create at least one user (any username; the dogfood lab uses the
operator's email) with:

- A password (any policy-compliant value; the user is prompted to
  change it on first login if the realm's password policy says so).
- Group membership: `meho-admins` (so the `groups-claim` mapper at
  Step 3 emits `meho-admins` and the backplane's group-gated tools
  are reachable).

The user's `email_verified` flag does not need to be true for
device-code to complete; if your realm enforces verified email
elsewhere, set it true on this user.

### MCP onramp — public `meho-mcp-client` (Claude.ai / Claude Desktop / MCP Inspector)

Repeat Steps 2–4 for a second public client used by MCP clients
that **can** carry `client_id` in their config (Claude.ai Custom
Connector, MCP Inspector). Suggested client ID `meho-mcp-client`;
client-shape contrast with `meho-cli`:

| Setting | `meho-mcp-client` | Why differs from `meho-cli` |
| --- | --- | --- |
| Standard flow (authorization-code + PKCE) | **On** | MCP 2025-06-18 mandates OAuth 2.1 authorization-code + PKCE for the `/mcp` path; the device grant is a CLI-only convenience. |
| Device grant | Off | Not needed; MCP clients run the browser-redirect flow. |
| Valid redirect URIs | `https://claude.ai/api/mcp/auth_callback`, `http://localhost:*` | Covers the Claude.ai Custom Connector and any localhost MCP Inspector. |
| Web origins | `+` (or a tight allow-list) | CORS for the browser flow. |
| PKCE challenge method | `S256` | Spec-required for public-client PKCE. |
| **Optional client scopes** | **`offline_access`** | Claude Code's MCP client **always** requests `offline_access` in its scope parameter to obtain a refresh token; if the scope isn't assigned on the client, Keycloak rejects the authorization request with `invalid_scope` (Wall #7). Assign as **optional** rather than default — only flows that ask for a refresh token (browser MCP clients) should mint one. The CLI device-code client deliberately doesn't get this scope (RFC 8628 device-code clients re-run the device dance instead of holding a long-lived refresh token; a stolen device-code refresh token has worse blast-radius than a fresh dance prompt). |

The 5 protocol mappers + 4 default client scopes from Steps 3–4
apply identically. The MCP client additionally carries
`offline_access` as an **optional** client scope so Claude Code's
auth-code + PKCE flow — which always lists `offline_access` in its
scope parameter — can mint a refresh token; without it Keycloak
returns `invalid_scope` (Wall #7). The CLI device-code client does
**not** get `offline_access`: it re-runs the device dance instead of
holding a refresh token. The recipe at
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
documents the per-client configuration step for each MCP client.

> **`.mcp.json` `client_id` limitation (Claude Code + Cursor).**
> Claude Code's HTTP-MCP support and Cursor's MCP wire-up
> as of 2026-05 follow the RFC 9728 metadata trail to the
> Keycloak authorization server but **do not expose a
> `client_id` field in their `.mcp.json` shape**. They
> attempt dynamic client registration (RFC 7591) against the
> Keycloak `clients-registrations/openid-connect` endpoint
> and hit Keycloak's Trusted Hosts policy — which ships with
> an empty whitelist, so anonymous DCR is **de-facto
> disabled** ([Keycloak docs](https://www.keycloak.org/securing-apps/client-registration)).
> The deployer-side fix (registering a public client by hand)
> doesn't help these clients until they expose `client_id` in
> `.mcp.json`. Two workarounds today: (a) use Claude.ai
> Custom Connector or MCP Inspector for first-class wire-up
> against `meho-mcp-client`; (b) shim Claude Code / Cursor
> through `mcp-remote` (or an equivalent stdio→HTTP proxy)
> and bake the Bearer token into the wrapper. The right
> long-term fix is upstream MCP-client `client_id` support,
> not opening DCR on a prod realm. The **third path**,
> documented in [§ CIMD onramp](#cimd-onramp--no-pre-registered-client-keycloak--2660-experimental)
> below, dissolves this wall entirely for CIMD-capable
> clients — a CIMD-mode client_id is an HTTPS URL the
> authorization server fetches, so neither DCR nor a
> deployer-side pre-registered client is needed.

### CIMD onramp — no pre-registered client (Keycloak ≥ 26.6.0, experimental)

> **Optional alternative to the pre-registration path above.** This
> section configures the realm to accept **Client ID Metadata
> Documents (CIMD)** — an OAuth extension where the client presents
> an HTTPS URL as its `client_id` and the authorization server
> fetches the JSON metadata at that URL on the fly. A CIMD-capable
> MCP client (Claude Code's HTTP MCP as of MCP protocol version
> `2025-11-25`) consequently authenticates with **no pre-registered
> client and no DCR**, dissolving Wall #6 for those clients. The
> pre-registration recipe above (Steps 1–5 + the MCP onramp) is
> unchanged and remains the required path for older Keycloak realms
> and for MCP clients that don't yet implement CIMD.
>
> **Read the framing carefully.** In CIMD the *client* (Claude
> Code / Anthropic) hosts its own metadata document and brings
> the URL as `client_id`; MEHO does not host that document and is
> not the publisher. MEHO is the resource server + MCP server.
> The realm-side deliverable below is **enabling and validating
> CIMD on the MEHO-fronted Keycloak realm** so that CIMD-mode
> `client_id` URLs are accepted, not anything MEHO publishes.
> MEHO's RFC 9728 protected-resource metadata surface is
> unchanged.
>
> **Stability disclaimer (load-bearing).** CIMD shipped
> **experimental** in Keycloak 26.6.0 (2026-04;
> [release notes](https://www.keycloak.org/2026/04/keycloak-2660-released))
> and is off by default. The Keycloak project documents it may
> introduce breaking changes in a future release
> ([CIMD config guide](https://www.keycloak.org/securing-apps/mcp-authz-server),
> tracking [keycloak#45106](https://github.com/keycloak/keycloak/issues/45106) /
> [keycloak#45284](https://github.com/keycloak/keycloak/issues/45284) /
> discussion [#44711](https://github.com/keycloak/keycloak/discussions/44711)).
> Treat this section's recipe as a moving target until CIMD goes
> GA; pin your Keycloak version. Realms on Keycloak < 26.6.0 must
> use the pre-registration path above.

#### When to use CIMD

| You should use CIMD if … | You should stay on the pre-registration path if … |
| --- | --- |
| Your Keycloak runs ≥ 26.6.0 and you accept the **experimental** stability label. | Your Keycloak is < 26.6.0 (CIMD is not present at all). |
| Your MCP client supports CIMD (Claude Code on MCP `2025-11-25+`). | Your MCP client doesn't carry a `client_id` field *and* doesn't implement CIMD — you're stuck on the `mcp-remote` workaround until the upstream client lands one or the other. |
| You'd rather not maintain a per-deployer public client + chart wiring for every MCP client variant. | You want a stable, GA-supported Keycloak surface area and don't mind the one-time `meho admin keycloak bootstrap-clients` (#791) run per realm. |

CIMD is **not** a replacement for `meho-cli` — the device-code
flow continues to need a pre-registered public client because
`meho login` is the CLI, not an MCP client, and the CIMD spec
binds metadata-resolution to OAuth authorization-code + PKCE
clients. Keep the `meho-cli` public client provisioned per
Steps 2–4 above regardless.

#### Step C1 — Enable the `cimd` feature flag on the Keycloak server

CIMD is gated behind a server-side feature flag; the realm-level
client-policy configuration in the following steps is a no-op
until the flag is on. Start (or re-start) Keycloak with:

```bash
bin/kc.sh start --features=cimd
# or, on a container deployment, set the env var:
#   KC_FEATURES=cimd
```

Verify the flag took effect by reading the OpenID Connect
authorization-server metadata document for the realm — once
`cimd` is enabled, the document carries
`"client_id_metadata_document_supported": true`:

```bash
curl -sf https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration \
  | jq .client_id_metadata_document_supported
# true
```

If the field is missing or `false`, the flag did not propagate —
re-check the server startup environment and the realm name.

#### Step C2 — Create the three MCP capability scopes (Optional type)

MCP `2025-11-25` introduces three protocol-level scopes the
authorization server uses to bind tokens to specific MCP
capabilities. Create each as a realm-level **client scope** with
type **Optional** (not Default — the spec leaves them opt-in per
session) and, on each scope, an Audience mapper whose **Included
Custom Audience** is the MCP server URL (`<backplane-url>/mcp`,
no trailing slash — same normalisation rule as the
`meho-mcp-audience` mapper in Step 3 above):

| Scope name | Type | Audience mapper (Included Custom Audience) |
| --- | --- | --- |
| `mcp:tools` | Optional | `<backplane-url>/mcp` |
| `mcp:prompts` | Optional | `<backplane-url>/mcp` |
| `mcp:resources` | Optional | `<backplane-url>/mcp` |

> **Source-of-truth note.** Earlier internal references named
> these scopes `mcp:read` / `mcp:execute`. That naming did not
> match the published Keycloak guide — the canonical names from
> [Keycloak's MCP authorization-server guide](https://www.keycloak.org/securing-apps/mcp-authz-server)
> are the three above (`mcp:tools` / `mcp:prompts` /
> `mcp:resources`), and that's what a CIMD-capable client
> requests at the `/authorize` step.

These scopes coexist with the `meho-mcp-audience` mapper +
4 default scopes from Steps 3–4 — a CIMD client still needs the
same downstream claim shape (`sub`, `aud`, `tenant_id`,
`tenant_role`, `groups`) the backplane validator enforces. The
shared claim-shape requirement is **not optional**: a CIMD
client whose token reaches the backplane without `tenant_id` /
`tenant_role` is rejected at the decode stage (Wall #2 / Wall
#3) with `invalid_audience` / `missing_tenant_claim` /
opaque `invalid_token`, the same failure modes the
pre-registration recipe's Step 3 mappers exist to prevent.
>
> **Mechanism note (load-bearing).** The pre-registration
> recipe at Step 3 above attaches the five claim mappers
> (`audience-meho-backplane`, `meho-mcp-audience`, `tenant-id`,
> `tenant-role`, `groups-claim`) to each client **directly**
> (per-client protocol mappers cloned from `meho-backplane`
> onto `meho-cli` / `meho-mcp-client`). That mechanism does
> **not** carry forward to a CIMD-resolved client — there is
> no per-client mapper-cloning step in CIMD because the client
> isn't pre-registered. A CIMD-capable client picks up its
> claim shape through one of two surfaces, and the deployer
> must choose one explicitly:
>
> - **(Preferred) Attach the equivalent mappers to a realm-
>   level *default* client scope every client inherits.**
>   Create a new realm client scope (e.g. `meho-backplane-
>   claims`), assign it the five mappers Step 3 lists,
>   mark it **Default** in **Realm Settings → Client
>   Scopes → Default Client Scopes**, and every newly-
>   resolved client — pre-registered *and* CIMD — gets the
>   same claim shape automatically. This is the simpler
>   deployer posture and the one the rest of this recipe
>   assumes.
> - **(Alternative) Carry the claims in the CIMD client's
>   metadata document.** The `draft-ietf-oauth-client-id-
>   metadata-document` shape allows clients to declare
>   `client_metadata` fields the AS forwards into tokens;
>   for a CIMD-only deployment posture this avoids the
>   realm-default-scope edit. The trade-off is that
>   the operator no longer owns the claim values — they
>   live in whatever the CIMD client publishes — which is
>   why the realm-default-scope form is recommended for
>   MEHO's tenant-claim shape.
>
> Do **not** rely on the per-client mappers Step 3 attaches
> to `meho-cli` / `meho-mcp-client` to reach a CIMD-resolved
> client. They won't — and the failure presents as the same
> `invalid_audience` / `missing_tenant_claim` wall a deployer
> running the pre-registration recipe without Step 3 would
> hit.

#### Step C3 — Create the `cimd-profile` client-policy profile

In **Realm Settings → Client Policies → Profiles**, create a
profile named `cimd-profile` (the name is conventional; any
identifier works as long as Step C4's policy references the
same string) and attach a single executor:

| Executor | Setting | Value | Why |
| --- | --- | --- | --- |
| `client-id-metadata-document` | **Allow http scheme** | **Off** (production); On only for a local dev realm | Per Keycloak's guide: production realms must reject `http://` `client_id` URLs and any `http://` URLs referenced in the metadata document (`logo_uri`, `policy_uri`, `tos_uri`, `jwks_uri`, …). |
| `client-id-metadata-document` | **Trusted domains** | Wildcard list of domains you accept as `client_id` URLs (e.g. `*.anthropic.com`, `*.claude.ai`) | An empty list denies all domains, so this field is **required** to be non-empty for the policy to allow any CIMD client at all. List the MCP clients your operator population uses. |
| `client-id-metadata-document` | **Only Allow Confidential Client** | **Off** | MCP clients (Claude Code) are public OAuth 2.1 + PKCE clients per the MCP spec; flipping this on rejects them at the executor. |
| `client-id-metadata-document` | **Restrict same domain** | **On** (default) | Forces the `client_id` URL and any metadata-referenced URLs to share the same host, which closes a phishing surface where a metadata document references logos / redirect URIs on an attacker's host. |
| `client-id-metadata-document` | **Required properties** | Leave default unless your operator policy demands specific metadata fields | Tightens metadata validation; the default set covers `redirect_uris` + `client_name`. |

#### Step C4 — Create the `cimd-policy` client policy

In **Realm Settings → Client Policies → Policies**, create a
policy named `cimd-policy` and configure it to apply the
`cimd-profile` profile to any client whose `client_id`
matches a CIMD-shaped URL:

- **Conditions → `client-id-uri`**:
  - **URI scheme**: `https` (production) — the same posture as
    the executor's `Allow http scheme: Off`.
  - **Trusted domains**: must mirror Step C3's trusted-domains
    list (the executor + condition both enforce the same allow-
    list; mismatches surface as "policy passed but executor
    denied" 400s that are tedious to diagnose).
- **Associated client profiles**: add `cimd-profile`.
- Save the policy.

Once the policy is enabled, a CIMD-mode authorization request
arriving at `/realms/<realm>/protocol/openid-connect/auth?client_id=https://…`
triggers the `client-id-uri` condition (the `client_id` is a
URL), the policy applies `cimd-profile`, and the executor
fetches + validates the metadata document at that URL before
proceeding.

#### Step C5 — Verify against a CIMD-capable MCP client

For the dogfood walkthrough, install Claude Code on a host
that has the deployment's TLS CA in its OS trust store (Step 1
above), point an `.mcp.json` at `https://<backplane-host>/mcp`
**without** a `client_id` field, and run a `tools/list`. The
OAuth flow should:

1. `meho` returns `401 + WWW-Authenticate: Bearer
   resource_metadata=…/.well-known/oauth-protected-resource`.
2. Claude Code fetches the RFC 9728 metadata, reads the
   `authorization_servers` URL, and constructs an authorization
   request whose `client_id` is the URL of its own metadata
   document (no DCR call is made).
3. Keycloak's `cimd-policy` fires on the URL-shaped
   `client_id`, the `client-id-metadata-document` executor
   fetches the URL, validates it against the trusted-domains
   list, and proceeds with the PKCE flow.
4. The issued access token carries `sub`, `aud` (including
   `<backplane-host>/mcp`), `tenant_id`, `tenant_role`,
   `groups` — the same claim shape the backplane validator
   requires of the pre-registered MCP client. `tools/list`
   succeeds.

> **The shared claim-shape requirement is not optional.** A
> CIMD client that authenticates successfully but is missing
> `tenant_id` / `tenant_role` / `sub` will still hit Walls
> #2 / #3 from the 4-wall matrix below. CIMD removes the
> *registration* step; it does not remove the *audience and
> claim-mapper* requirement. The realm-level default client
> scopes from Step 4 above are what make those claims appear
> on CIMD-issued tokens too.

If `tools/list` fails, the diagnostic path mirrors the
4-wall matrix below — the only CIMD-specific failure modes
are at the policy gate, and they're prefixed in the Keycloak
event log with `CLIENT_REGISTER_ERROR` /
`client_registration_policy_failed` referencing the
`client-id-uri` condition or the
`client-id-metadata-document` executor by name.

#### Out of scope for CIMD onramp

- **Anthropic's side of CIMD.** Whether and how Claude Code
  hosts its CIMD metadata document is upstream tooling; this
  recipe assumes the client implements CIMD per the IETF draft.
- **Per-MCP-client CIMD enablement.** Each MCP client either
  supports CIMD or doesn't (Claude Code's HTTP MCP on protocol
  `2025-11-25+` does; older clients and Cursor as of 2026-05
  don't). The realm-side recipe is the same regardless of which
  CIMD-capable clients connect to it.
- **Automated provisioning of `cimd-profile` / `cimd-policy`
  via `meho admin keycloak bootstrap-clients`.** The bootstrap
  helper (#791) provisions the pre-registration path today.
  Extending it with a `--enable-cimd` step is a small follow-up
  but out of scope for this Task — documented as the issue's
  optional sub-deliverable (#911 Fix shape item 2).
- **Helm chart values exposing CIMD knobs.** None today —
  the realm-side configuration is per-realm rather than
  per-MEHO-deployment, and the chart's auth-related values
  (`config.keycloakIssuerUrl`, `config.keycloakCliClientId`)
  are unaffected by enabling CIMD.

### Wire it into Helm

Set the chart value to the CLI client_id you created at Step 2:

```yaml
config:
  keycloakCliClientId: meho-cli   # or whatever you chose
```

The backplane's `/api/v1/auth-config` endpoint surfaces this value
as the `cli_client_id` JSON field, which `meho login`'s discovery
parser maps to the OAuth `client_id`. The CLI also accepts
`--client-id <id>` as a per-invocation override, useful when a
deployer publishes multiple public clients (e.g. `meho-cli-prod`,
`meho-cli-staging`) and the chart value pins one default.

The MCP public client (`meho-mcp-client`) is not chart-wired today
— Claude.ai's Custom Connector and MCP Inspector both ask the
operator to paste a client_id at config time. Auto-discovery via
RFC 9728 advertises the authorization server but not the
`client_id` (the metadata document defines that as an out-of-band
parameter, intentionally).

### Verify

After `helm install` / `helm upgrade` and the realm steps above:

```bash
# 1. Auth-config endpoint carries cli_client_id.
curl -sf https://meho.evba.lab/api/v1/auth-config | jq .
# {
#   "keycloak_issuer": "https://keycloak.evba.lab/realms/evba",
#   "audience": "meho-backplane",
#   "cli_client_id": "meho-cli"
# }

# 2. CLI device-code flow completes.
meho login https://meho.evba.lab
# Logged in to https://meho.evba.lab; token stored in keyring.

# 3. Authenticated REST call succeeds.
curl -sf -H "Authorization: Bearer $(meho status --print-token)" \
  https://meho.evba.lab/api/v1/health
# {"status": "ok"}

# 4. MCP RFC 9728 metadata document is reachable.
curl -sf https://meho.evba.lab/.well-known/oauth-protected-resource | jq .
# {
#   "resource": "https://meho.evba.lab/mcp",
#   "authorization_servers": ["https://keycloak.evba.lab/realms/evba"],
#   ...
# }
```

### Five-wall symptom → cause → fix matrix

The deployer's first-login walk hits these walls in sequence; each
surfaces with a different symptom but the cause-and-fix chain is
bounded. W1–W4 came out of the 2026-05-21 RDC dogfood; **W7** was
added 2026-05-22 when the same operator's MCP client (Claude Code)
hit `invalid_scope` after the W1–W4 fixes shipped. Walls are
numbered for cross-reference with the originating dogfood reports.

| Wall | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| **W1** | Device-code initiation 401s with `{"error":"unauthorized_client","error_description":"Invalid client or Invalid client credentials"}`. Or DCR 403s with `{"error":"insufficient_scope","error_description":"Policy 'Trusted Hosts' rejected request to client-registration service. Details: Host not trusted."}`. | The CLI / MCP client tried to drive the device or authorization-code grant against a **confidential** client (typically `meho-backplane`), or anonymous DCR against a realm whose Trusted Hosts policy ships with an empty whitelist. Confidential clients require a `client_secret` the CLI can't carry; DCR on a prod realm is closed by design ([Keycloak docs](https://www.keycloak.org/securing-apps/client-registration)). | Pre-create a **public** client per Step 2 (and Step 2-MCP for the MCP path). Set `config.keycloakCliClientId` to the CLI client's ID so `/api/v1/auth-config` surfaces it. Operators on older backplanes can pass `--client-id <id>` per-invocation. Don't open DCR — the right fix is a deployer-side public client. |
| **W2** | Token issuance succeeds; every backend call 401s with a structured `detail` code (post-[#797](https://github.com/evoila/meho/issues/797) + [#1131](https://github.com/evoila/meho/issues/1131): `invalid_audience` / `missing_tenant_claim` / `missing_tenant_role_claim` / `malformed_jws` if the `Authorization` header carries a non-JWT value at all). | The public client mints tokens with a different claim shape than `meho-backplane` validates against — missing the `audience-meho-backplane` mapper (wrong `aud`), the `meho-mcp-audience` mapper (no MCP audience), the `tenant-id` mapper, or the `tenant-role` mapper. A `malformed_jws` instead means the `Authorization` header is not a JWT (typo, copy/paste truncation, or a probe like `Bearer not-a-real-jwt`). | Clone all 5 mappers from the `meho-backplane` client onto the public client per Step 3. After the fix, decode the issued token (`jwt.io` or `kcadm.sh evaluate-protocol-mappers`) and confirm `aud` is an array containing both `meho-backplane` and `<backplane-url>/mcp`, plus `tenant_id`, `tenant_role`, `groups` are present. For `malformed_jws`: paste the bearer into `jwt.io` — if it doesn't decode, the header was sent with the wrong value, not the issued token. |
| **W3** | Token issuance succeeds; every backend call 401s with `{"detail":"invalid_token"}` even after Wall #2 is closed; decoded token has `aud`, `tenant_id`, `tenant_role`, `groups` but **no `sub` claim**. | The `basic` client scope wasn't assigned. Keycloak 25 moved `sub` into the Subject (sub) protocol mapper inside the `basic` scope; clients created via the admin REST API don't auto-inherit realm default-default scopes the way the admin-console UI populates them. RFC 9068 §2.2.1 makes `sub` REQUIRED on JWT access tokens, so rejection is spec-correct — the diagnostic is the opaque part. | Add `basic`, `roles`, `web-origins`, `acr` to the public client's **default** client scopes per Step 4. Re-issue (logout and re-login; existing tokens are stale) and confirm `sub` is now present. After [#797](https://github.com/evoila/meho/issues/797) lands the symptom is `{"detail":"missing_sub"}` instead of the opaque form. |
| **W4** | `meho login` fails with `meho: token exchange failed: context deadline exceeded`, often before the human has a chance to approve the verification URL. | Not the device-code TTL (which is already 10 minutes per `cli/internal/auth/devicecode.go:355`'s `PollTimeout = 10 * time.Minute` — longer than Keycloak's default 600 s `expires_in`). The real cause is an **ambient parent deadline** on `cmd.Context()` (CI step timeout, `claude` bash-tool timeout, IDE-task wrapper) that truncates the approval wait far below the device-code lifetime. | (Until [#798](https://github.com/evoila/meho/issues/798) lands) run `meho login` in a real interactive terminal without a short wrapper deadline; or raise the wrapper timeout to ≥ `expires_in` + headroom. After #798 lands the message distinguishes "parent deadline fired" from "device code expired" and the device-flow approval wait detaches from a too-short parent context. |
| **W7** | Browser-flow MCP client (Claude Code, MCP Inspector) authorization request 400s with `{"error":"invalid_scope","error_description":"Invalid scopes: openid profile email offline_access"}` (or just `offline_access` in the failed-scopes list). Token endpoint never reached; the user never sees a Keycloak login page. | The MCP client requested `offline_access` to obtain a refresh token (Claude Code **always** includes it; OIDC Core §11 makes it the spec-defined signal for a refresh token), but the `meho-mcp-client` public client doesn't have `offline_access` in either its default or optional client-scope list. Keycloak rejects unknown / unassigned scopes per OAuth 2.0 [RFC 6749 §5.2](https://www.rfc-editor.org/rfc/rfc6749#section-5.2). The realm-built-in `offline_access` scope exists; it just isn't attached to the public MCP client. | Assign the realm's built-in `offline_access` client scope to `meho-mcp-client` as an **optional** scope (not default — only flows that ask for a refresh token should mint one). In the Keycloak admin console: `meho-mcp-client` → Client scopes → Add client scope → pick `offline_access` → **Optional**. `meho admin keycloak bootstrap-clients` does this automatically from the next release after v0.6.0 ([#912](https://github.com/evoila/meho/issues/912)). The CLI device-code client (`meho-cli`) deliberately is **not** given `offline_access` — device-code clients re-run the device dance rather than hold a long-lived refresh token; a stolen device-code refresh token has worse blast-radius than re-prompting the operator. |

### Out of scope for this recipe

- **The broader Deploying-MEHO guide.** This recipe is one entry in
  the consolidated deployment guide tracked under
  [#559](https://github.com/evoila/meho/issues/559) (deployment-
  friction umbrella). When #559 lands, this recipe moves there
  verbatim; until then `deploy/values-examples/README.md` is the
  authoritative deployer doc for the chart values + the auth-onramp
  recipe.
- **Automation of the recipe.** [#791](https://github.com/evoila/meho/issues/791)
  (G0.9.1-T11) ships `meho admin keycloak bootstrap-clients` — an
  idempotent verb that creates the two public clients + 5 mappers +
  4 default scopes from one invocation, plus (from the next release
  after v0.6.0 via [#912](https://github.com/evoila/meho/issues/912))
  the `offline_access` optional scope on the MCP client (W7). The recipe
  above is the manual path; the helper is the supported automation.
- **Confidential `meho-backplane` client.** Out of scope — it's
  created at realm-install time alongside the Keycloak realm itself
  and isn't touched by this recipe. Rotating its client secret is
  also out of scope.
- **RFC 7591 Dynamic Client Registration support in MEHO.** A real
  feature with its own design tradeoffs (which clients are allowed
  to self-register; how operators audit unknown registrations);
  tracked separately, not a v0.3.2 onramp fix. `docs/cross-repo/mcp-client-setup.md`
  notes it as future work.
- **The CLI device-code client provisioning + auth-config endpoint
  completion.** [#789](https://github.com/evoila/meho/issues/789)
  (G0.9.1-T9) — already landed in v0.3.2.
- **Token-validator error specificity at the decode stage.**
  [#797](https://github.com/evoila/meho/issues/797) (G0.9.1-T12)
  makes Wall #2 / Wall #3 symptoms specific (`invalid_audience` /
  `missing_sub` / …) instead of opaque `invalid_token`.
- **The `meho login` context-deadline fix.**
  [#798](https://github.com/evoila/meho/issues/798) (G0.9.1-T13)
  closes Wall #4 in code.

## pgvector extension prerequisite

The chart's pre-install migration Job runs Alembic revision `0003`
([`backend/alembic/versions/0003_create_documents_with_pgvector.py`](../../backend/alembic/versions/0003_create_documents_with_pgvector.py)),
which executes:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

`CREATE EXTENSION` requires a **superuser** in stock PostgreSQL. A
normally-provisioned cluster gives MEHO a **least-privilege app role**
(the DSN user in `postgres.credentialsSecret`), which is *not* a
superuser — so a **cold** install fails at this step with:

```
permission denied to create extension "vector"
HINT:  Must be superuser to create this extension.
```

Warm/managed setups often hide this because the extension is already
present (a pre-existing extension makes `IF NOT EXISTS` a NOTICE-and-skip,
with no ownership check) or because migrations run as a superuser. A
first-time adopter following this chart with a least-privilege role hits
it immediately.

**Satisfy one of the two options below before `helm install`.** Option A
(pre-create the extension once, keep the app role least-privilege) is the
recommended, minimal posture — it keeps the running DSN unprivileged and
adds no chart surface. The chart intentionally does **not** ship a
separate superuser migration DSN; that decision and its rationale are
recorded in
[`docs/decisions/pgvector-superuser-prerequisite.md`](../../docs/decisions/pgvector-superuser-prerequisite.md).

### Option A (recommended): pre-create the extension as a superuser

Create the extension once, as a superuser, against the target database
**before** installing. It is idempotent — safe to re-run.

```bash
psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**CloudNativePG (CNPG):** exec into the primary Pod's `postgres`
container (which runs as the bootstrap superuser) and create it there.
CNPG's Postgres 18.1 image ships pgvector 0.8.1; the extension just needs
a superuser to enable it:

```bash
kubectl exec <cluster>-1 -n <ns> -c postgres -- \
  psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

To make it survive a cluster re-bootstrap, declare it at init time on the
CNPG `Cluster` — `postInitSQL` runs as the bootstrap superuser:

```yaml
spec:
  bootstrap:
    initdb:
      postInitSQL:
        - "CREATE EXTENSION IF NOT EXISTS vector;"
```

### Option B: run the first migration under a superuser-capable role

Point `postgres.credentialsSecret`'s `DATABASE_URL` at a
superuser-capable role for the first migration. This is simpler to state
but keeps a privileged credential on the running DSN unless you rotate it
back down afterwards, so Option A is preferred.

## End-to-end install flow

The order matters because the chart's migration Job runs as a
`pre-install,pre-upgrade` hook and refuses to start without
`DATABASE_URL`.

```bash
# 0. Once per cluster: install External Secrets Operator.
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace

# 1. Once per cluster: create the ClusterSecretStore pointing at Vault.
#    (Owned by your GitOps repo, not this chart — see above.)
kubectl apply -f cluster-secret-store-vault.yaml

# 2. Per release: provision the ExternalSecret(s) the chart references.
#    Skip this step if you set `eso.enabled: true` in values — the chart
#    renders them itself.
kubectl apply -n meho -f externalsecret-meho-postgres.yaml

# 3. Wait for the target Secret to materialise. The chart's pre-install
#    migration Job mounts it; if it isn't ready, the Job fails fast.
kubectl -n meho wait --for=create secret/meho-postgres --timeout=60s
kubectl -n meho get externalsecret meho-postgres -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# → "True"

# 3b. Pre-create the pgvector extension as a superuser (see § pgvector
#     extension prerequisite). Skip only if the migration DSN's role is
#     already superuser-capable. Idempotent.
kubectl exec meho-db-1 -n meho -c postgres -- \
  psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 4. Apply the chart.
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  -f values-rdc.yaml
```

## Out of scope for v0.1

- The actual `values-rdc.yaml` (private, lives in the consumer's repo).
- The `ClusterSecretStore` / `SecretStore` manifest itself — consumer-owned.
- Multi-environment overlays (staging, prod) — v0.1 ships only the lab shape.
- Helm `--values=secret://` plugin integration — v0.2 if the dual-source
  ExternalSecret pattern proves friction-heavy.
- ApplicationSet (ArgoCD) — v0.2.

## References

- Parent Goal: [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
- Parent Initiative: [#36 — G2.5 Helm chart](https://github.com/evoila-bosnia/meho-internal/issues/36)
- This task: [#40 — values-rdc-example.yaml + ESO sync patterns documented](https://github.com/evoila-bosnia/meho-internal/issues/40)
- External Secrets Operator: <https://external-secrets.io/>
- ESO Vault provider: <https://external-secrets.io/latest/provider/hashicorp-vault/>
- ESO ExternalSecret API: <https://external-secrets.io/latest/api/externalsecret/>
- Chart documentation: [`docs/codebase/devops.md`](../../docs/codebase/devops.md)
